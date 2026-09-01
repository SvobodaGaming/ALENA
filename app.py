"""
АЛЁНА – Автоматический Ловец Ёрничества, Небрежности и Аутентичности.

Flask web application for autonomous student report checking.

Accounts come in two roles. A teacher is isolated by default: own checks, own
fingerprint base, borrowing searched only inside that base. An administrator
manages accounts and system settings and may be allowed to see everything.

Групп преподавателей (checker/teams.py) раздвигает ровно одну эту стену: у
участников группы база отпечатков общая, а проверки и история остаются
личными. Отсюда две области видимости – `_scope()` для личного и
`_base_scope()` для базы.

Run locally:
    python app.py

Run with Docker:
    docker compose up --build

Run on server (production):
    gunicorn --workers=1 --threads=8 --timeout=300 -b 0.0.0.0:5000 app:app

Один рабочий процесс – не экономия, а требование: частичные загрузки и состояние
идущих проверок лежат в памяти процесса. Со вторым воркером куски одной партии
попадают в разные процессы и загрузка обрывается «Загрузка не найдена».
Параллелизм даёт --threads. То же значение стоит в Dockerfile.
"""

import csv
import errno
import hashlib
import io
import json
import os
import re
import secrets
import time
import uuid
import zipfile
import tempfile
import shutil
import threading
from functools import wraps
from pathlib import Path
from datetime import datetime, timedelta

from flask import (Flask, Blueprint, request, jsonify, render_template,
                   send_file, abort, send_from_directory, session,
                   redirect, url_for, g, flash)
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from weasyprint import HTML as WeasyHTML
    WEASYPRINT_OK = True
except Exception:
    WEASYPRINT_OK = False

from checker import (accounts, branding, convert, db, grading, job_store,
                     sqlmigrate, summary as summary_mod, teams)
from checker.extractor        import extract_report
from checker.gost             import check_gost, GOST_CHECKS, ALL_CODES, FLAW_TEXT
from checker.text_plagiarism  import check_text_plagiarism
from checker.image_plagiarism import check_image_plagiarism
from checker.reporter         import generate_html_report


APP_TITLE = branding.APP_TITLE
APP_FULL_NAME = branding.APP_FULL_NAME


def _trusted_proxy_count() -> int:
    raw = os.environ.get('AU_TRUSTED_PROXY_COUNT', '').strip() or '0'
    try:
        count = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            'AU_TRUSTED_PROXY_COUNT must be a non-negative integer'
        ) from exc
    if count < 0:
        raise RuntimeError(
            'AU_TRUSTED_PROXY_COUNT must be a non-negative integer'
        )
    return count


TRUSTED_PROXY_COUNT = _trusted_proxy_count()
app = Flask(__name__)
app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=TRUSTED_PROXY_COUNT,
    x_proto=0,
    x_host=0,
    x_port=0,
    x_prefix=0,
)

# Предел одного запроса. Партия отчётов приходит кусками, поэтому её общий
# объём этим не ограничен – см. UPLOAD_MAX_TOTAL.
app.config['MAX_CONTENT_LENGTH'] = 600 * 1024 * 1024

# Без SECRET_KEY из окружения ключ рождается случайным на каждый запуск: сессии
# переживут только этот процесс, зато подделать их нельзя. Прежнее значение по
# умолчанию было одинаковым у всех установок – с ним чужая cookie принималась
# как своя.
_secret = os.environ.get('SECRET_KEY', '').strip()
if not _secret or _secret in ('dev-secret-change-in-production',
                              'change-this-to-a-random-string'):
    _secret = secrets.token_hex(32)
    print('  ВНИМАНИЕ: SECRET_KEY не задан – сессии сбросятся при перезапуске. '
          'Задайте его в .env для рабочей установки.')
app.config['SECRET_KEY'] = _secret

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,       # cookie недоступна скриптам страницы
    SESSION_COOKIE_SAMESITE='Lax',      # не уходит по запросам с чужих сайтов
    # За TLS поставьте AU_HTTPS=1 в окружении процесса.
    SESSION_COOKIE_SECURE=os.environ.get('AU_HTTPS', '').strip() in ('1', 'true', 'yes'),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_FORM_MEMORY_SIZE=2 * 1024 * 1024,
)

REPORTS_DIR = Path(__file__).parent / 'reports'
REPORTS_DIR.mkdir(exist_ok=True)

JOB_ID_RE = re.compile(r'^[0-9a-f]{6,40}$')     # id проверки – только hex
# Форматы работ. DOCX, ODT и DOC перед разбором приводятся к PDF, поэтому
# дальше первого шага проверки разница между ними не доходит.
DOC_EXTS = ('.pdf',) + convert.SOURCE_EXTS
UPLOAD_EXTS = DOC_EXTS + ('.zip',)
UPLOAD_EXTS_TEXT = 'PDF, DOCX, ODT, DOC и ZIP'


def _mb_text(mb: int) -> str:
    """«800 МБ» или «5 ГБ» – как удобнее читать преподавателю."""
    if mb < 1024:
        return f'{mb} МБ'
    return f"{f'{mb / 1024:.1f}'.rstrip('0').rstrip('.')} ГБ"


def _mb_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, '').strip() or default))
    except ValueError:
        return default


# Курс целиком – это гигабайты. Один запрос столько не тянет: nginx, таймауты и
# память рвутся раньше, поэтому браузер шлёт партию кусками, а здесь ограничен
# её суммарный объём.
UPLOAD_MAX_MB = _mb_env('AU_MAX_UPLOAD_MB', 5120)   # 5 ГБ
UPLOAD_MAX_TOTAL = UPLOAD_MAX_MB * 1024 * 1024
UPLOAD_CHUNK = 16 * 1024 * 1024      # рекомендуемый браузеру размер куска
UPLOAD_TTL = 6 * 3600                # брошенная загрузка живёт не дольше

# Где держать принятые файлы и распакованные PDF. По умолчанию системный temp,
# но на многих серверах это tmpfs в оперативной памяти – партия на гигабайты
# положит машину. AU_TMP_DIR переводит её на обычный диск.
TMP_ROOT = os.environ.get('AU_TMP_DIR', '').strip() or None
if TMP_ROOT:
    Path(TMP_ROOT).mkdir(parents=True, exist_ok=True)

ZIP_MAX_TOTAL = UPLOAD_MAX_TOTAL * 2  # предел распаковки: архив-бомба не съест диск
ZIP_MAX_FILES = 5000

# Приставки временных каталогов. Собственные, а не общие up_/rc_: уборка чужого
# мусора из системного /tmp – не наше дело, а прежние приставки могли совпасть
# с чужими.
TMP_PREFIX_UPLOAD = 'alena_up_'
TMP_PREFIX_JOB    = 'alena_rc_'

jobs: dict = {}
jobs_lock = threading.Lock()

# Идущая проверка отмечается в хранилище каждые JOB_BEAT_EVERY секунд. Молчание
# дольше JOB_STALE_AFTER значит, что процесс с её потоком не жив.
JOB_BEAT_EVERY = 15
JOB_STALE_AFTER = 90

# Частичные загрузки: id → каталог, владелец, объём. Ключ выдаётся сервером и
# проверяется по владельцу – чужую партию не дополнить.
uploads: dict = {}
uploads_lock = threading.Lock()


# ────────────────────  Ссылки на статику  ────────────────────

def _stamp(path: Path) -> str:
    """Короткая метка редакции файла – время правки и размер."""
    try:
        st = path.stat()
    except OSError:
        return '0'
    return f'{int(st.st_mtime):x}{st.st_size:x}'


def _asset(filename: str) -> str:
    """Ссылка на файл из static/ с меткой его редакции.

    Метка меняется вместе с файлом, поэтому каждый выпуск программы получает
    свой адрес стилей и скрипта. Это и позволяет отдавать их с годовым кэшем
    (см. _security_headers): браузер не переспрашивает про них на каждом
    переходе и при этом никогда не покажет вчерашнее оформление.
    """
    return url_for('static', filename=filename,
                   v=_stamp(Path(app.static_folder) / filename))


def _logo_url() -> str:
    """То же самое для логотипа – он лежит рядом с app.py, не в static/."""
    return url_for('logo', v=_stamp(Path(app.root_path) / 'au_logo.png'))


@app.context_processor
def inject_globals():
    """Every template gets the current account and the branding."""
    return {
        'user':          getattr(g, 'user', None),
        'app_title':     APP_TITLE,
        'app_full_name': APP_FULL_NAME,
        'brand':         branding,
        'csrf_token':    _csrf_token,
        'ROLES':         accounts.ROLES,
        'STATES':        accounts.STATES,
        'can':           accounts.can,
        'asset':         _asset,
        'logo_url':      _logo_url,
        'checks_count':  _checks_count,
    }


def _mark_if_stale(job_id: str, data: dict, _locked: bool = False) -> dict:
    """Пометить ошибкой проверку, которую никто не ведёт.

    Признак – не факт перезапуска, а молчание: поток проверки отмечается в
    хранилище каждые JOB_BEAT_EVERY секунд, и если отметки нет дольше
    JOB_STALE_AFTER, процесс с этим потоком умер (кончилась память, сервер
    перезапустили, случился сбой). Раньше все идущие проверки списывались при
    старте процесса – и второй рабочий процесс gunicorn, поднявшись, гасил
    живую проверку соседа сообщением о перезагрузке, которой не было.
    """
    if data.get('status') != 'processing':
        return data
    try:
        quiet = time.time() - float(data.get('beat') or 0)
    except (TypeError, ValueError):
        quiet = JOB_STALE_AFTER + 1
    if quiet < JOB_STALE_AFTER:
        return data

    original = data
    data = dict(
        original,
        status='error',
        step=('Проверка оборвалась: процесс проверки остановлен. Чаще всего '
              'серверу не хватило памяти на большой партии – попробуйте '
              'разделить её на части. Последний шаг: '
              f'«{data.get("step") or "неизвестен"}».'),
        error='job thread gone: no heartbeat',
    )
    try:
        if _locked:
            job_store.save(job_id, data)
        else:
            # Bulk clear uses the same lock around delete. A delayed stale-job
            # save must not recreate a record after that delete completes.
            with jobs_lock:
                latest = job_store.get(job_id)
                if latest is None:
                    return data
                if latest != original:
                    return latest
                job_store.save(job_id, data)
    except Exception:
        pass
    return data


def _recover_stale_jobs():
    """Проверки, чей поток не пережил остановку процесса, при старте помечаются
    ошибкой – иначе они висят «выполняется» вечно и их нельзя даже удалить.

    Молчащие меньше JOB_STALE_AFTER не трогаем: их мог вести соседний процесс.
    """
    try:
        for jid, data in job_store.load_all().items():
            _mark_if_stale(jid, data)
    except Exception:
        pass  # storage not reachable yet; stale rows stay until the next boot


def _purge_expired():
    """Drop checks older than the retention period set by the administrator."""
    try:
        days = int(accounts.get_settings().get('retention_days', 0))
        for jid in job_store.expired(days):
            job_store.delete(jid)
            (REPORTS_DIR / f'{jid}.html').unlink(missing_ok=True)
    except Exception:
        pass


def _migrate_legacy_ownership():
    """Data written before per-teacher isolation has no owner. Hand it to the
    first administrator, rewriting fingerprint keys into the owner-scoped form,
    so an existing installation keeps its base and history after the upgrade."""
    from checker.memory_store import load_store, save_store, delete_entry

    admin = next((u for u in accounts.load_users().values()
                  if u.get('role') == 'admin'), None)
    if admin is None:
        return
    login = admin['login']

    store = load_store()
    legacy = {k: v for k, v in store.items() if not v.get('owner')}
    if legacy:
        migrated = {}
        for entry in legacy.values():
            entry['owner'] = login
            entry['key_base'] = f"{login}|{entry.get('key_base', '')}"
            migrated[f"{entry['key_base']}|v{entry.get('version', 1)}"] = entry
        save_store(migrated)
        for key in legacy:
            delete_entry(key)

    for jid, data in job_store.load_all().items():
        if not data.get('owner'):
            data['owner'] = login
            data['owner_fio'] = admin.get('fio', login)
            job_store.save(jid, data)


GOST_SCHEMA = 4   # bumped whenever the meaning of the check codes changes


def _migrate_settings():
    """The check codes were re-cut when the criteria list changed, so a saved
    default set from the previous schema now selects the wrong checks. Reset it
    to «все критерии» and let the administrator pick again."""
    conf = accounts.get_settings()
    if conf.get('gost_schema') != GOST_SCHEMA:
        accounts.save_settings({'gost_schema': GOST_SCHEMA, 'default_gost': []})


def _purge_temp_dirs(older_than: float = 24 * 3600):
    """Убрать временные каталоги загрузок и проверок, брошенные перезапуском.

    Порог в сутки: идущую проверку соседнего процесса задеть нельзя, а гигабайты
    от оборванных загрузок за неделю забьют диск. Приставки свои – в общем /tmp
    под `up_`/`rc_` мог лежать и чужой каталог.
    """
    now = time.time()
    root = Path(TMP_ROOT or tempfile.gettempdir())
    for prefix in (TMP_PREFIX_UPLOAD, TMP_PREFIX_JOB):
        for item in root.glob(f'{prefix}*'):
            try:
                if item.is_dir() and now - item.stat().st_mtime > older_than:
                    shutil.rmtree(item, ignore_errors=True)
            except OSError:
                pass


def _startup():
    accounts.bootstrap()
    _migrate_settings()
    _migrate_legacy_ownership()
    _recover_stale_jobs()
    _purge_expired()
    _purge_temp_dirs()


_startup()


# ─────────────────────────  Защита запросов  ─────────────────────────

SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}


def _csrf_token() -> str:
    """Токен сессии для форм. Шаблоны получают его как csrf_token()."""
    token = session.get('csrf')
    if not token:
        token = secrets.token_urlsafe(32)
        session['csrf'] = token
    return token


def _csrf_ok() -> bool:
    sent = (request.form.get('csrf_token')
            or request.headers.get('X-CSRF-Token', ''))
    stored = session.get('csrf', '')
    return bool(sent and stored and secrets.compare_digest(sent, stored))


@app.before_request
def _csrf_protect():
    """Меняющий данные запрос принимается только со своим токеном.

    Исключение – вызовы API по ключу: заголовок X-API-Key браузер сам к чужому
    запросу не добавит, подделать такой вызов с постороннего сайта нельзя.
    """
    if request.method in SAFE_METHODS:
        return None
    if request.path.startswith('/api/'):
        if request.headers.get('X-API-Key'):
            return None
        # With no browser session there is nothing for CSRF to protect; let the
        # API authentication layer return its documented JSON 401.
        if not session.get('login'):
            return None
    if _csrf_ok():
        return None
    if _wants_json():
        return jsonify({'error': 'Сессия устарела – обновите страницу '
                                 'и повторите действие.'}), 403
    return render_template('error.html', code=403,
                           message='Сессия устарела или запрос пришёл со '
                                   'стороннего сайта. Обновите страницу и '
                                   'повторите действие.'), 403


@app.after_request
def _security_headers(response):
    """Заголовки, которые закрывают типовые атаки на страницу."""
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('Referrer-Policy', 'same-origin')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault(
        'Content-Security-Policy',
        # 'unsafe-inline' нужен: разметка страниц и сам отчёт держат стили и
        # небольшие скрипты внутри. Всё остальное – только со своего адреса,
        # никаких внешних загрузок и встраивания в чужие рамки.
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; font-src 'self' data:; "
        "connect-src 'self'; form-action 'self'; base-uri 'none'; "
        "object-src 'none'; frame-ancestors 'none'")
    # Состояние проверок кэшировать нельзя: по вернувшемуся из кэша ответу
    # страница показала бы давно законченную проверку как идущую.
    if request.path.startswith(('/api/', '/status/', '/jobs')):
        response.headers.setdefault('Cache-Control', 'no-store')
    elif request.endpoint in ('static', 'logo'):
        # Стили, скрипт, шрифты и логотип меняются только с выпуском
        # программы. Flask по умолчанию помечает их 'no-cache', и браузер
        # переспрашивал про каждый из них на каждом переходе: десяток
        # обращений к серверу подряд, из-за которых страница успевала
        # мелькнуть без оформления.
        if request.args.get('v'):
            # В ссылке есть метка редакции (см. _asset): этот адрес
            # принадлежит только текущей версии файла и не устареет.
            response.headers['Cache-Control'] = \
                'public, max-age=31536000, immutable'
        else:
            # Шрифты запрашивает сам app.css по относительной ссылке, без
            # метки. Месяц – достаточно долго, чтобы не тревожить сервер, и
            # достаточно коротко, чтобы заменённый файл разошёлся сам;
            # переименование файла обновляет его сразу.
            response.headers['Cache-Control'] = 'public, max-age=2592000'
    return response


def _safe_next(raw: str) -> str:
    """Куда вернуть после входа. Только свой сайт: со ссылкой вида
    ?next=https://чужой.сайт вход превратился бы в удобный трамплин."""
    if not raw:
        return url_for('index')
    if raw.startswith('//') or '\\' in raw or ':' in raw.split('/')[0]:
        return url_for('index')
    return raw if raw.startswith('/') else url_for('index')


def _int_field(form, name: str, default: int, low: int, high: int) -> int:
    """Целое из формы, зажатое в допустимые границы. Буквы вместо числа больше
    не роняют сохранение настроек с ошибкой 500."""
    try:
        value = int(float(form.get(name, default)))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, min(high, value))


def _float_field(form, name: str, default: float, low: float, high: float) -> float:
    """Дробное из формы, зажатое в границы.

    Порог заимствования приходит из формы и из API, и его никто не проверял:
    «abc» и пустая строка роняли запуск проверки пятисоткой, «inf» – переполнением
    при округлении, а «-5» и «99» тихо запускали проверку с бессмысленным
    порогом в −500 % и 9900 %.
    """
    raw = form.get(name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value != value or value in (float('inf'), float('-inf')):   # NaN, ±inf
        return default
    return max(low, min(high, value))


# ─────────────────────────  Authentication  ─────────────────────────

@app.before_request
def _load_user():
    g.user = accounts.get_user(session.get('login', ''))
    if g.user is not None and g.user.get('state') == 'blocked':
        session.clear()
        g.user = None


def _client_ip() -> str:
    return request.remote_addr or ''


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if g.user is None:
            # Относительный путь, а не request.url: возврат после входа
            # разрешён только внутрь этого сайта (см. _safe_next).
            return redirect(url_for('login', next=request.full_path.rstrip('?')))
        if g.user.get('must_change') and request.endpoint not in ('change_password', 'logout', 'static'):
            return redirect(url_for('change_password'))
        return f(*args, **kwargs)
    return decorated


def permission_required(flag: str):
    """Guard a route behind one permission flag."""
    def wrapper(f):
        @wraps(f)
        @login_required
        def decorated(*args, **kwargs):
            if not accounts.can(g.user, flag):
                return _deny('Недостаточно прав для этого действия.')
            return f(*args, **kwargs)
        return decorated
    return wrapper


def admin_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if g.user.get('role') != 'admin':
            return _deny('Раздел доступен только администратору.')
        return f(*args, **kwargs)
    return decorated


def _wants_json() -> bool:
    """Ждёт ли вызывающий JSON, а не страницу.

    Страницы шлют формы, скрипт страницы – fetch/XHR с заголовком токена.
    Раньше признаком был только Accept: application/json, а fetch по умолчанию
    шлёт «*/*» – и на отказ скрипт получал HTML-страницу ошибки, спотыкался на
    res.json() и показывал общее «не удалось» вместо настоящей причины.
    """
    return (request.path.startswith('/api/')
            or request.is_json
            or 'X-CSRF-Token' in request.headers
            or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or 'application/json' in request.headers.get('Accept', ''))


def _deny(message: str):
    if _wants_json():
        return jsonify({'error': message}), 403
    abort(403, description=message)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if g.user is not None:
        return redirect(url_for('index'))
    error = None
    username = ''
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user, error = accounts.authenticate(
            username, password, _client_ip(),
            request.headers.get('User-Agent', ''))
        if user is not None:
            session.clear()
            session['login'] = user['login']
            session.permanent = True
            if user.get('must_change'):
                return redirect(url_for('change_password'))
            return redirect(_safe_next(request.form.get('next', '')))
    return render_template('login.html', error=error, username=username,
                           next=request.args.get('next', ''))


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/password', methods=['GET', 'POST'])
@login_required
def change_password():
    """Forced first-login change, and the ordinary self-service change."""
    error = None
    if request.method == 'POST':
        current = request.form.get('current', '')
        fresh   = request.form.get('password', '')
        repeat  = request.form.get('repeat', '')
        from werkzeug.security import check_password_hash
        if not check_password_hash(g.user['password_hash'], current):
            error = 'Текущий пароль введён неверно'
        elif fresh != repeat:
            error = 'Новый пароль и повтор не совпадают'
        else:
            error = accounts.password_problem(fresh)
        if not error:
            accounts.set_password(g.user['login'], fresh, must_change=False)
            flash('Пароль изменён')
            return redirect(url_for('index'))
    return render_template('password.html', error=error,
                           forced=bool(g.user.get('must_change')))


# ─────────────────────────  API authentication  ─────────────────────────

def _api_user():
    """The account behind an /api/v1 call: session cookie or X-API-Key."""
    key = request.headers.get('X-API-Key', '')
    if key:
        user = accounts.user_by_api_key(key)
        if user and user.get('state') != 'blocked' and accounts.can(user, 'use_api'):
            return user
        # An explicit key never falls back to an ambient browser session: the
        # request must act as the account whose credential it supplied.
        return None
    if g.user is not None and not g.user.get('must_change'):
        return g.user
    return None


def api_auth_required(f):
    """Like login_required, but returns JSON 401 instead of redirecting."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = _api_user()
        if user is None:
            return jsonify({
                'error': 'Не авторизовано: передайте заголовок X-API-Key '
                         'или войдите в сессию.'
            }), 401
        g.user = user
        return f(*args, **kwargs)
    return decorated


@app.errorhandler(403)
def forbidden(exc):
    if request.path.startswith('/api/'):
        return jsonify({'error': exc.description, 'status': 403}), 403
    return render_template('error.html', code=403,
                           message=exc.description), 403


@app.errorhandler(413)
def too_large(exc):
    """Тело запроса больше предела. Загрузка идёт из скрипта – отвечаем JSON,
    иначе на странице просто оборвётся связь без объяснения."""
    limit = app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)
    text = f'Запрос больше {limit} МБ – уменьшите порцию файлов'
    if request.path.startswith('/api/') or request.path.startswith('/upload'):
        return jsonify({'error': text, 'status': 413}), 413
    return render_template('error.html', code=413, message=text), 413


@app.route('/health')
def health():
    return jsonify({'ok': True})


@app.route('/au_logo.png')
def logo():
    return send_from_directory('.', 'au_logo.png', mimetype='image/png')


# ─────────────────────────  Ownership  ─────────────────────────

def _sees_all(user) -> bool:
    return accounts.can(user, 'see_all')


def _scope(user):
    """Owner filter for storage queries: None means 'everything'."""
    return None if _sees_all(user) else user['login']


def _checks_count() -> int:
    """Сколько проверок видит текущая запись – число для значка в меню.

    Значок стоит в общем каркасе страницы, то есть считать приходится на
    каждой. Поэтому число берётся у хранилища отдельным подсчётом, а не
    чтением всей истории: разбирать каждую проверку целиком ради одного
    числа слишком дорого. В пределах запроса считаем один раз.
    """
    user = getattr(g, 'user', None)
    if not user:
        return 0
    if 'checks_count' not in g:
        try:
            g.checks_count = job_store.count(_scope(user))
        except Exception:
            # История недоступна – это не повод ронять страницу целиком.
            g.checks_count = 0
    return g.checks_count


def _base_scope(user):
    """Owner filter for the fingerprint base – шире, чем `_scope`.

    Проверки и отчёты остаются личными, а база отпечатков общая на группу:
    ради этого группы и заводятся. Вне групп список равен `[login]`, то есть
    ровно прежней личной базе.
    """
    return None if _sees_all(user) else teams.visible_owners(user['login'])


def _may_view(job: dict, user) -> bool:
    return _sees_all(user) or job.get('owner', '') == user['login']


def _owns_job(job: dict, user) -> bool:
    return job.get('owner', '') == user['login']


def _may_delete(job: dict, user) -> bool:
    if _owns_job(job, user):
        return accounts.can(user, 'delete_own')
    return (user.get('role') == 'admin'
            and accounts.can(user, 'delete_all'))


def _login_confirmation_matches(user: dict, raw) -> bool:
    confirmed = str(raw or '').strip().encode('utf-8')
    expected = str(user.get('login') or '').encode('utf-8')
    return bool(confirmed and expected
                and secrets.compare_digest(confirmed, expected))


def _processing_job_ids_locked(owner=None) -> list:
    """Processing checks targeted by a bulk operation; jobs_lock is held."""
    processing = set()
    for jid, data in job_store.load_all(owner).items():
        current = _mark_if_stale(jid, data, _locked=True)
        if current.get('status') == 'processing':
            processing.add(jid)
    processing.update(
        jid for jid, data in jobs.items()
        if data.get('status') == 'processing'
        and (owner is None or data.get('owner', '') == owner)
    )
    return sorted(processing)


def _public_job(job: dict) -> dict:
    """Job state without the heavy inline report HTML.

    Ключи с подчёркиванием – служебные отметки времени для троттлинга записи,
    наружу они не идут.
    """
    return {k: v for k, v in job.items()
            if k != 'report_html' and not k.startswith('_')}


def _find_job(job_id: str):
    """Live in-memory state if the job is running, else the stored record."""
    # id проверки уходит в имя файла отчёта – принимаем только тот вид, в
    # котором сами его выдаём, чтобы «../» из адреса никуда не привёл.
    if not JOB_ID_RE.match(job_id or ''):
        return None
    with jobs_lock:
        job = jobs.get(job_id)
        if job is not None:
            return _public_job(job)
    stored = job_store.get(job_id)
    return stored if stored is None else _mark_if_stale(job_id, stored)


def _all_jobs(user):
    scope = _scope(user)
    merged = {jid: _mark_if_stale(jid, data)
              for jid, data in job_store.load_all(scope).items()}
    with jobs_lock:
        for jid, j in jobs.items():
            if scope is None or j.get('owner', '') == scope:
                merged[jid] = _public_job(j)
    return merged


# ─────────────────────────  Pages  ─────────────────────────

@app.route('/')
@login_required
def index():
    return render_template('checks.html', page='checks', flaw_text=FLAW_TEXT)


@app.route('/overview')
@login_required
def overview():
    stats = _overview_stats(g.user)
    return render_template('overview.html', page='overview', **stats)


@app.route('/new')
@permission_required('run_checks')
def new_check():
    conf = accounts.get_settings()
    enabled = conf.get('default_gost') or ALL_CODES
    weights = grading.clean_weights(conf.get('gost_weights'))
    return render_template('new.html', page='new', gost_checks=GOST_CHECKS,
                           enabled_codes=enabled,
                           weights={c: weights.get(c, grading.DEFAULT_WEIGHT)
                                    for c in ALL_CODES},
                           scale=grading.clean_scale(conf.get('grade_scale')),
                           upload_max_mb=UPLOAD_MAX_MB,
                           upload_max_text=_mb_text(UPLOAD_MAX_MB),
                           threshold=int(float(conf.get('default_threshold', 0.6)) * 100))


@app.route('/base')
@login_required
def base_page():
    from checker.memory_store import load_store, get_summary
    entries = get_summary(load_store(_base_scope(g.user)))
    fio_by_login = {lg: u.get('fio', lg) for lg, u in accounts.load_users().items()}
    for e in entries:
        e['owner_fio'] = fio_by_login.get(e.get('owner', ''), e.get('owner', ''))
    my_teams = teams.teams_of(g.user['login'])
    return render_template('reports_base.html', page='base', entries=entries,
                           sees_all=_sees_all(g.user),
                           my_teams=my_teams,
                           # Столбец с преподавателем нужен там, где записи
                           # могут быть не только свои.
                           show_owner=_sees_all(g.user) or bool(my_teams))


@app.route('/base/export')
@login_required
def base_export():
    """Download the fingerprint base: a spreadsheet of what is stored, or a
    full JSON dump that can be carried to another instance."""
    from checker.memory_store import load_store, get_summary

    scope = _base_scope(g.user)
    store = load_store(scope)
    stamp = datetime.now().strftime('%d.%m.%Y')

    if request.args.get('format') == 'json':
        payload = json.dumps({
            'exported_at': datetime.now().strftime('%d.%m.%Y %H:%M'),
            'exported_by': g.user['login'],
            'scope':       'all' if scope is None else ', '.join(scope),
            'count':       len(store),
            'entries':     store,
        }, ensure_ascii=False, indent=2)
        return send_file(
            io.BytesIO(payload.encode('utf-8')),
            mimetype='application/json',
            as_attachment=True,
            download_name=f'alena-base-{stamp}.json',
        )

    fio_by_login = {lg: u.get('fio', lg) for lg, u in accounts.load_users().items()}
    buf = io.StringIO()
    # Semicolons and a BOM: what Excel with Russian locale opens without a
    # column-splitting dialog.
    writer = csv.writer(buf, delimiter=';', lineterminator='\r\n')
    writer.writerow(['Студент', 'Группа', 'Файл', 'Версия', 'Страниц',
                     'Рисунков', 'Добавлен', 'Проверка', 'Преподаватель'])
    for e in get_summary(store):
        writer.writerow(_csv_row([
            e['student'].get('name', ''), e['student'].get('group', ''),
            e['filename'], e['version'], e['pages_count'], e['image_count'],
            e['added_at'], e['job_id'],
            fio_by_login.get(e.get('owner', ''), e.get('owner', '')),
        ]))
    return send_file(
        io.BytesIO(b'\xef\xbb\xbf' + buf.getvalue().encode('utf-8')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'alena-base-{stamp}.csv',
    )


def _csv_row(values: list) -> list:
    """Обезвредить ячейки, которые Excel примет за формулу.

    Имя файла вида `=cmd|...` из чужой работы иначе выполнится у того, кто
    откроет выгрузку. Апостроф в начале превращает такую ячейку в текст.
    """
    return [f"'{v}" if isinstance(v, str) and v[:1] in ('=', '+', '-', '@')
            else v for v in values]


@app.route('/profile')
@login_required
def profile():
    return render_template('profile.html', page='profile',
                           events=accounts.recent_logins(20, g.user['login']))


@app.route('/admin/users')
@admin_required
def admin_users():
    users = sorted(accounts.load_users().values(),
                   key=lambda u: (u['role'] != 'admin', u.get('fio', '')))
    counts = {}
    for data in job_store.load_all().values():
        owner = data.get('owner', '')
        counts[owner] = counts.get(owner, 0) + 1
    for u in users:
        u['checks_count'] = counts.get(u['login'], 0)
    return render_template(
        'users.html', page='users', users=users,
        permissions=accounts.PERMISSIONS,
        # Политика паролей – нижняя граница: при включённой настройке галочку
        # «сменить при первом входе» снять нельзя.
        force_change=bool(accounts.get_settings().get('pw_require_change_first')))


@app.route('/admin/log')
@admin_required
def admin_log():
    only_fail = request.args.get('filter') == 'fail'
    events = accounts.recent_logins(300)
    if only_fail:
        events = [e for e in events if not e.get('ok')]
    fio_by_login = {lg: u.get('fio', lg) for lg, u in accounts.load_users().items()}
    for e in events:
        e['fio'] = fio_by_login.get(e.get('login', ''), '')
    return render_template('log.html', page='log', events=events,
                           only_fail=only_fail)


@app.route('/admin/settings', methods=['GET', 'POST'])
@admin_required
def admin_settings():
    if request.method == 'POST':
        form = request.form
        picked = _parse_enabled_checks(form.get('gost'))
        accounts.save_settings({
            'default_threshold':       _int_field(form, 'threshold', 60, 10, 90) / 100,
            'default_gost':            picked if picked is not None else [],
            'gost_weights':            _parse_weights_form(form),
            'grade_scale':             grading.clean_scale(form.get('grade_scale')),
            'retention_days':          _int_field(form, 'retention_days', 0, 0, 3650),
            'pw_min_len':              _int_field(form, 'pw_min_len', 10, 6, 64),
            'pw_require_change_first': form.get('pw_require_change_first') == 'on',
            'pw_expire_days':          _int_field(form, 'pw_expire_days', 0, 0, 1095),
            'lock_after_fails':        _int_field(form, 'lock_after_fails', 5, 0, 20),
            'lock_minutes':            _int_field(form, 'lock_minutes', 15, 1, 1440),
        })
        flash('Настройки сохранены')
        return redirect(url_for('admin_settings'))

    conf = accounts.get_settings()
    from checker.memory_store import load_store
    reports_size = sum(p.stat().st_size for p in REPORTS_DIR.glob('*.html'))
    health = [
        ('База данных',
         'PostgreSQL' if db.DB_ENABLED else 'JSON-файлы в папке memory/',
         'ok', 'Подключена' if db.DB_ENABLED else 'Локальное хранилище'),
        ('Хранилище отчётов',
         f'{len(list(REPORTS_DIR.glob("*.html")))} отчётов · {reports_size / 1048576:.1f} МБ',
         'ok', 'Норма'),
        ('Экспорт в PDF', 'WeasyPrint',
         'ok' if WEASYPRINT_OK else 'warn',
         'Доступен' if WEASYPRINT_OK else 'Не установлен'),
        ('База отпечатков', f'{len(load_store())} записей', 'ok', 'Норма'),
    ]
    weights = grading.clean_weights(conf.get('gost_weights'))
    return render_template('settings.html', page='settings', conf=conf,
                           gost_checks=GOST_CHECKS,
                           enabled_codes=conf.get('default_gost') or ALL_CODES,
                           weights={c: weights.get(c, grading.DEFAULT_WEIGHT)
                                    for c in ALL_CODES},
                           scale=grading.clean_scale(conf.get('grade_scale')),
                           health=health)


@app.route('/admin/migration')
@admin_required
def admin_migration():
    return render_template('migration.html', page='migration',
                           counts=sqlmigrate.counts(),
                           db_enabled=db.DB_ENABLED,
                           reports_count=len(list(REPORTS_DIR.glob('*.html'))))


@app.route('/admin/migration/export')
@admin_required
def migration_export():
    """Весь состав базы одним файлом .sql – схема, данные, порядок вставки."""
    payload = sqlmigrate.dump().encode('utf-8')
    stamp = datetime.now().strftime('%d.%m.%Y')
    accounts.record_event(g.user['login'], True, _client_ip(),
                          request.headers.get('User-Agent', ''),
                          'выгрузка базы в SQL')
    return send_file(io.BytesIO(payload), mimetype='application/sql',
                     as_attachment=True, download_name=f'alena-db-{stamp}.sql')


@app.route('/admin/migration/import', methods=['POST'])
@admin_required
def migration_import():
    """Загрузка дампа. Файл разбирается, а не выполняется: в базу попадают
    только строки известных таблиц (см. checker/sqlmigrate.py)."""
    replace = request.form.get('replace') == 'on'
    if replace:
        if not accounts.can(g.user, 'delete_all'):
            flash('Полная замена требует права удалять данные всех преподавателей')
            return redirect(url_for('admin_migration'))
        if not _login_confirmation_matches(
                g.user, request.form.get('confirm_login')):
            flash('Для полной замены введите свой логин точно как в профиле')
            return redirect(url_for('admin_migration'))
    upload = request.files.get('dump')
    if upload is None or not upload.filename:
        flash('Выберите файл .sql')
        return redirect(url_for('admin_migration'))
    if not upload.filename.lower().endswith('.sql'):
        flash('Ожидается файл с расширением .sql')
        return redirect(url_for('admin_migration'))

    try:
        text = upload.read().decode('utf-8')
    except UnicodeDecodeError:
        flash('Файл не в кодировке UTF-8 – это не наш дамп')
        return redirect(url_for('admin_migration'))

    try:
        rows = sqlmigrate.parse(text)
        if replace:
            # One lock covers the final active-job check and the replacement:
            # a concurrent upload cannot register a new job between them.
            with jobs_lock:
                if _processing_job_ids_locked():
                    flash('Полная замена невозможна, пока выполняются проверки')
                    return redirect(url_for('admin_migration'))
                stats = sqlmigrate.restore(rows, replace=True,
                                           keep_login=g.user['login'])
                # Persisted rows are now authoritative. Completed jobs cached
                # by this process must not override the replacement until a
                # restart.
                jobs.clear()
        else:
            stats = sqlmigrate.restore(rows, replace=False,
                                       keep_login=g.user['login'])
    except Exception as exc:
        flash(f'Дамп не загружен: {exc}')
        return redirect(url_for('admin_migration'))

    accounts.record_event(g.user['login'], True, _client_ip(),
                          request.headers.get('User-Agent', ''),
                          'загрузка базы из SQL')
    flash('Дамп загружен. Учётных записей: {users}, проверок: {jobs}, '
          'отпечатков: {fingerprints}, записей журнала: {login_events}, '
          'групп преподавателей: {teams}.'
          .format(**stats))
    if rows.get('skipped'):
        flash(f'Пропущено непонятных инструкций: {rows["skipped"]} – '
              'загружаются только INSERT в таблицы базы.')
    return redirect(url_for('admin_migration'))


def _overview_stats(user) -> dict:
    """Aggregates for the dashboard, scoped to what the account may see."""
    records = _all_jobs(user)
    done = [d for d in records.values() if d.get('status') == 'done']
    summaries = [d['summary'] for d in done if d.get('summary')]

    files = sum(int(d.get('total', 0)) for d in done)
    gost_values = [s['gost'] for s in summaries if s.get('gost')]
    grade_values = [s['grade'] for s in summaries if s.get('grade') is not None]
    flagged = sum(len(s.get('matches', [])) for s in summaries)
    clean = sum(s.get('clean', 0) for s in summaries)
    scored = sum(len(s.get('students', [])) for s in summaries)

    fails: dict = {}
    for s in summaries:
        for code, count in s.get('fail_counts', []):
            fails[code] = fails.get(code, 0) + count
    names = {c[0]: c[1] for c in GOST_CHECKS}
    top = sorted(fails.items(), key=lambda kv: -kv[1])[:6]
    top_violations = [
        {'code': c, 'name': names.get(c, c), 'count': n,
         'pct': round(n / scored * 100) if scored else 0}
        for c, n in top
    ]

    by_day: dict = {}
    for d in records.values():
        created = job_store.parse_created_at(d.get('created_at'))
        if created is not None:
            day = created.strftime('%d.%m.%Y')
            by_day[day] = by_day.get(day, 0) + 1
    series = sorted(by_day.items(),
                    key=lambda kv: datetime.strptime(kv[0], '%d.%m.%Y'))[-30:]

    recent = sorted(records.items(),
                    key=lambda kv: (job_store.parse_created_at(
                        kv[1].get('created_at')) or datetime.min),
                    reverse=True)[:8]
    return {
        'total_checks':   len(records),
        'total_files':    files,
        'avg_gost':       round(sum(gost_values) / len(gost_values)) if gost_values else 0,
        'avg_grade':      (round(sum(grade_values) / len(grade_values))
                           if grade_values else None),
        'flagged':        flagged,
        'clean_pct':      round(clean / scored * 100) if scored else 0,
        'clean':          clean,
        'scored':         scored,
        'top_violations': top_violations,
        'series':         series,
        'recent':         [dict(job_id=jid, **data) for jid, data in recent],
        'teachers':       len({d.get('owner') for d in records.values() if d.get('owner')}),
    }


# ─────────────────────────  Shared job helpers  ─────────────────────────

def _parse_enabled_checks(raw):
    """Translate the form's `gost` value into a list of check codes.

    raw is None when the field is absent (legacy clients / API) -> None means
    "all checks". A comma-separated string -> only those (valid) codes, order
    preserved as in GOST_CHECKS.
    """
    if raw is None:
        return None
    picked = {c.strip().upper() for c in raw.split(',') if c.strip()}
    return [c for c in ALL_CODES if c in picked]


def _parse_weights_form(form) -> dict:
    """Веса критериев из полей `weight_S1`, `weight_F2`… настроек."""
    return grading.clean_weights(
        {code: form.get(f'weight_{code}') for code in ALL_CODES
         if form.get(f'weight_{code}') is not None})


def _grade_params(form) -> tuple:
    """(weights, scale) для запускаемой проверки.

    Форма может прислать свои – «S1:100,F2:40». Чего нет в запросе, берётся из
    системных настроек, поэтому старые клиенты и API без параметров считают по
    администраторским весам.
    """
    conf = accounts.get_settings()
    weights = grading.clean_weights(form.get('weights')) \
        or grading.clean_weights(conf.get('gost_weights'))
    scale = grading.clean_scale(form.get('scale') or conf.get('grade_scale'))
    return weights, scale


# Предел длины имени файла. Файловые системы меряют его в байтах, а не в
# буквах: 255 байт на ext4. Кириллическая буква занимает два байта, арабская
# или китайская – три, так что «безопасные» 180 букв превращались в 400 байт и
# запись падала с «File name too long». Оставляем запас на приписку _N, которой
# разводятся одинаковые имена внутри архива.
NAME_MAX_BYTES = 200


def _fit_name(name: str, limit: int = NAME_MAX_BYTES) -> str:
    """Укоротить имя до limit байт, сохранив расширение.

    Расширение обязано уцелеть: по нему отбираются работы и по нему же решается,
    нужна ли файлу конвертация. Обрезанное «в лоб» имя выпадало из проверки
    вместе с работой студента.
    """
    if len(name.encode('utf-8')) <= limit:
        return name
    stem, dot, tail = name.rpartition('.')
    ext = dot + tail if dot else ''
    # Метка по полному имени. Две работы из Moodle различаются иногда только
    # хвостом имени; без метки они после обрезки совпали бы, и вторая дописалась
    # бы в первую вместо своего файла.
    mark = '_' + hashlib.sha1(name.encode('utf-8')).hexdigest()[:6]
    room = max(limit - len((mark + ext).encode('utf-8')), 0)
    # Режем по байтам и отбрасываем разрубленную пополам букву.
    cut = (stem or name).encode('utf-8')[:room].decode('utf-8', 'ignore').strip()
    return (cut or 'file') + mark + ext


def _safe_upload_name(raw: str):
    r"""Имя загружаемого файла, пригодное для записи на диск.

    Кириллицу оставляем – из имени файла берутся фамилия и группа. Убираем
    путь, служебные символы и всё, что не работа и не архив.

    Обратный слэш режется наравне с прямым: на Linux это допустимый символ
    имени, и `a\..\..\x.pdf` проходило бы целиком – безвредно здесь, но на
    файловой системе Windows это выход из каталога.
    """
    name = (raw or '').replace('\\', '/')
    name = Path(name).name.replace('\x00', '').strip()
    if not name or name.startswith('.') or name in ('..', '.'):
        return None
    if not name.lower().endswith(UPLOAD_EXTS):
        return None
    return _fit_name(name)


def _unique_name(dest_dir: str, name: str, taken=()) -> str:
    """Имя, которое ещё не занято в каталоге задания.

    Две работы с одинаковым именем файла – обычное дело: выгрузка из Moodle
    раскладывает их по папкам студентов, а на сервер приходят одни basename.
    Без разведения имён вторая работа молча записывалась поверх первой и
    выпадала из проверки.
    """
    stem, dot, tail = name.rpartition('.')
    ext = dot + tail if dot else ''
    stem = stem or name
    candidate = name
    n = 1
    while candidate in taken or os.path.exists(os.path.join(dest_dir, candidate)):
        n += 1
        candidate = _fit_name(f'{stem}_{n}{ext}')
    return candidate


def _zip_name(info: zipfile.ZipInfo) -> str:
    """Имя записи архива в читаемом виде.

    Архив без флага UTF-8 zipfile разбирает как cp437 – русские и арабские
    фамилии превращаются в мусор вроде «Ð˜Ð²Ð°Ð½Ð¾Ð²». Возвращаем байты назад
    и читаем их как UTF-8: так собирают архивы Moodle и большинство архиваторов.
    """
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode('cp437').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def _doc_entries(zf: zipfile.ZipFile) -> list:
    """Записи архива с работами по его оглавлению, без распаковки."""
    entries = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = _safe_upload_name(_zip_name(info))
        if name is None or not name.lower().endswith(DOC_EXTS):
            continue
        entries.append(info)
    return entries


def _zip_doc_count(archive: str) -> int:
    """Сколько работ в архиве. Читается только оглавление – ответ мгновенный,
    поэтому «в архиве нет работ» видно сразу, а не после долгой распаковки."""
    with zipfile.ZipFile(archive, 'r') as zf:
        return len(_doc_entries(zf))


def _extract_zip(archive: str, dest_dir: str, on_file=None) -> None:
    """Распаковать только работы и только внутрь папки задания.

    Имена берём по последнему сегменту пути: архив с записью вида
    `../../etc/passwd` не должен ничего написать за пределами tmp_dir. Заодно
    ограничиваем число файлов и суммарный размер – распаковка «архива-бомбы»
    иначе забьёт диск сервера.

    on_file(готово, всего) вызывается после каждого файла: распаковка сотни
    работ занимает заметное время, и о ней надо отчитываться.
    """
    total = 0
    written = 0
    with zipfile.ZipFile(archive, 'r') as zf:
        planned = _doc_entries(zf)
        for info in planned:
            total += info.file_size
            written += 1
            if written > ZIP_MAX_FILES or total > ZIP_MAX_TOTAL:
                raise ValueError('архив слишком большой для распаковки')
            name = _safe_upload_name(_zip_name(info))
            target = Path(dest_dir) / _unique_name(dest_dir, name)
            with zf.open(info) as src, open(target, 'wb') as out:
                shutil.copyfileobj(src, out, 1024 * 256)
            if on_file is not None:
                on_file(written, len(planned))


def _parse_use_memory(raw) -> bool:
    """Form field `use_memory`: absent (legacy clients) means True."""
    if raw is None:
        return True
    return raw.strip().lower() not in ('0', 'false', 'no', 'off')


def _upload_slot(upload_id):
    """Открытая загрузка по её номеру.

    Номер выдан сервером и привязан к учётной записи: чужую партию не дополнить
    и не запустить, даже зная номер.

    Returns (slot, error_message, http_status).
    """
    with uploads_lock:
        slot = uploads.get(upload_id or '')
        if slot is None:
            return None, 'Загрузка не найдена или устарела – начните заново', 404
        if slot['owner'] != g.user['login']:
            return None, 'Загрузка принадлежит другой учётной записи', 403
        return slot, None, 200


def _sweep_uploads():
    """Убрать брошенные загрузки: вкладку закрыли, связь оборвалась.

    Иначе принятые куски навсегда остаются во временном каталоге сервера.
    """
    now = time.monotonic()
    stale = []
    with uploads_lock:
        for uid, slot in list(uploads.items()):
            if now - slot['at'] > UPLOAD_TTL:
                stale.append(uploads.pop(uid))
    for slot in stale:
        shutil.rmtree(slot['dir'], ignore_errors=True)


def _start_job(uploaded, threshold: float, owner, enabled_checks=None,
               use_memory=True, weights=None, scale=grading.DEFAULT_SCALE):
    """Принять файлы одним запросом и запустить проверку.

    Так работают API и старые клиенты: объём такой загрузки ограничен пределом
    одного запроса. Интерфейс шлёт партию кусками – см. /upload/part.
    """
    if not uploaded or all(f.filename == '' for f in uploaded):
        return None, 'Файлы не выбраны', 400

    tmp_dir = tempfile.mkdtemp(prefix=TMP_PREFIX_JOB, dir=TMP_ROOT)
    try:
        for f in uploaded:
            name = _safe_upload_name(f.filename)
            if name is None:
                continue
            # Имя разводится, а не перезаписывается: два файла с одинаковым
            # basename – это две разные работы, а не одна.
            f.save(os.path.join(tmp_dir, _unique_name(tmp_dir, name)))
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, f'Ошибка при загрузке: {e}', 500

    return _launch(tmp_dir, threshold, owner, enabled_checks, use_memory,
                   weights, scale)


def _launch(tmp_dir: str, threshold: float, owner, enabled_checks=None,
            use_memory=True, weights=None, scale=grading.DEFAULT_SCALE):
    """Пересчитать принятые файлы и запустить поток проверки.

    enabled_checks: list of GOST check codes to evaluate, or None for all.
    use_memory: when False, skip comparison against stored fingerprints
    (new reports are still added to the base).
    weights/scale: «процент использования» каждого критерия и шкала оценки.
    Захватываются в момент запуска – оценка не должна меняться задним числом.

    Returns (job_id, error_message, http_status). On success error_message is
    None; on failure job_id is None.

    Запрос завершается сразу после подсчёта работ: распаковка архива идёт уже в
    фоновом потоке. Иначе на большой партии браузер минутами ждал ответа, не
    зная ни числа работ, ни того, что происходит.
    """
    job_id = uuid.uuid4().hex[:10]
    expected = 0
    try:
        for item in sorted(Path(tmp_dir).iterdir()):
            if not item.is_file():
                continue
            # Оглавление архива читается мгновенно: число работ известно сразу,
            # а сама распаковка – уже в потоке проверки.
            expected += (_zip_doc_count(str(item))
                         if item.name.lower().endswith('.zip') else 1)
    except zipfile.BadZipFile:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, 'Архив повреждён или это не ZIP', 400
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, f'Ошибка при загрузке: {e}', 500

    if not expected:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, 'Работы не найдены в загруженных данных ' \
                     '(принимаются PDF, DOCX, ODT, DOC)', 400

    with jobs_lock:
        jobs[job_id] = {
            'status':     'processing',
            'progress':   0,
            'step':       f'Файлы приняты: {expected} шт. Подготовка…',
            'total':      expected,
            'done_files': 0,
            'text_pairs': 0,
            'img_pairs':  0,
            'created_at': datetime.now().strftime('%d.%m.%Y %H:%M'),
            'beat':       time.time(),
            'owner':      owner['login'],
            'owner_fio':  owner.get('fio', owner['login']),
            'threshold':  round(threshold * 100),
            'summary':    None,
            'report_html': None,
            'error':      None,
        }
        snapshot = _public_job(jobs[job_id])

    job_store.save(job_id, snapshot)

    threading.Thread(
        target=_process_job,
        args=(job_id, tmp_dir, threshold, owner['login'],
              enabled_checks, use_memory, weights or {},
              grading.clean_scale(scale)),
        daemon=True,
    ).start()

    return job_id, None, 200


def _job_status(job_id: str):
    job = _find_job(job_id)
    if job is None:
        abort(404)
    if not _may_view(job, g.user):
        return _deny('Эта проверка принадлежит другому преподавателю.')
    return jsonify(job)


def _job_report(job_id: str):
    job = _find_job(job_id)
    if job is None:
        abort(404)
    if not _may_view(job, g.user):
        return _deny('Эта проверка принадлежит другому преподавателю.')
    # Serve directly from disk, works across worker restarts and multi-tab use.
    report_path = REPORTS_DIR / f'{job_id}.html'
    if not report_path.exists():
        abort(404)
    return send_file(str(report_path.resolve()), mimetype='text/html')


def _job_export(job_id: str):
    """Convert the saved HTML report to PDF via WeasyPrint and return it."""
    job = _find_job(job_id)
    if job is None:
        abort(404)
    if not _may_view(job, g.user):
        return _deny('Эта проверка принадлежит другому преподавателю.')

    report_path = REPORTS_DIR / f'{job_id}.html'
    if not report_path.exists():
        abort(404)

    if not WEASYPRINT_OK:
        return jsonify({'error': 'WeasyPrint не установлен на сервере'}), 501

    html_content = report_path.read_text(encoding='utf-8')
    pdf_bytes = WeasyHTML(
        string=html_content,
        base_url=str(report_path.parent.absolute()),
    ).write_pdf()

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f'report_{job_id}.pdf',
    )


def _clear_jobs(owner, scope: str):
    """Delete one owner's data, or all data after separate authorization."""
    from checker.memory_store import clear_store
    # Keep launches and worker updates outside the whole destructive window.
    # `_launch()` registers a job under the same lock, so a new check either
    # exists before this check and blocks clearing, or starts after it finishes.
    with jobs_lock:
        processing = _processing_job_ids_locked(owner)
        if processing:
            return jsonify({
                'error': 'Нельзя очистить данные, пока выполняются проверки. '
                         'Дождитесь завершения или остановите их.',
                'processing': len(processing),
            }), 409
        ids = job_store.clear(owner)
        for jid in ids:
            jobs.pop(jid, None)
        for jid in ids:
            if JOB_ID_RE.fullmatch(jid or ''):
                (REPORTS_DIR / f'{jid}.html').unlink(missing_ok=True)
        cleared_store = clear_store(owner)
    return jsonify({'ok': True, 'scope': scope, 'cleared': len(ids),
                    'cleared_store': cleared_store})


def _clear_own():
    if not accounts.can(g.user, 'delete_own'):
        return _deny('Нет права удалять свои проверки.')
    return _clear_jobs(g.user['login'], 'own')


def _clear_everything(confirm_login: str):
    if (g.user.get('role') != 'admin'
            or not accounts.can(g.user, 'delete_all')):
        return _deny('Глобальная очистка доступна только администратору '
                     'с правом удалять данные всех преподавателей.')
    if not _login_confirmation_matches(g.user, confirm_login):
        return jsonify({'error': 'Для глобальной очистки введите свой логин '
                                 'точно так, как он указан в профиле.'}), 400
    return _clear_jobs(None, 'all')


def _confirmation_login() -> str:
    data = request.get_json(silent=True)
    if isinstance(data, dict) and 'confirm_login' in data:
        return data.get('confirm_login', '')
    return request.form.get('confirm_login', '')


def _cancel_job(job_id: str):
    """Остановить идущую проверку по просьбе владельца.

    Отметка ставится в память процесса: поток проверки видит её в ближайшей
    точке остановки (см. _tick) и сворачивается сам. Убить поток снаружи нечем,
    да и не нужно – отпущенный по-хорошему, он успевает стереть временный
    каталог с принятыми файлами.
    """
    job = _find_job(job_id)
    if job is None:
        abort(404)
    if not _owns_job(job, g.user):
        return _deny('Эта проверка принадлежит другому преподавателю.')
    if job.get('status') != 'processing':
        return jsonify({'error': 'Проверка уже завершена – прерывать нечего.'}), 409

    with jobs_lock:
        live = jobs.get(job_id)
        if live is not None:
            live['_cancel'] = True
    if live is None:
        # Отметка свежая, а потока в этом процессе нет: проверку ведёт соседний
        # процесс, и его память отсюда не достать.
        return jsonify({'error': 'Проверку ведёт другой процесс сервера – '
                                 'остановить её отсюда нельзя.'}), 409

    _update(job_id, _force=True, step='Останавливаем проверку…')
    return jsonify({'ok': True})


def _delete_job(job_id: str):
    """Delete a single check from history: in-memory state, stored record and
    the saved HTML report. Fingerprints in the student base are kept – they are
    managed separately via /memory."""
    job = _find_job(job_id)
    if job is None:
        abort(404)
    if not _may_delete(job, g.user):
        return _deny('Нет права удалять эту проверку.')
    if job.get('status') == 'processing':
        return jsonify({'error': 'Проверка ещё выполняется – дождитесь '
                                 'завершения или ошибки.'}), 409
    with jobs_lock:
        jobs.pop(job_id, None)
    job_store.delete(job_id)
    (REPORTS_DIR / f'{job_id}.html').unlink(missing_ok=True)
    return jsonify({'ok': True})


def _memory_summary():
    from checker.memory_store import load_store, get_summary
    return jsonify(get_summary(load_store(_base_scope(g.user))))


def _memory_delete(key: str):
    from checker.memory_store import load_store, delete_entry
    if not key:
        return jsonify({'error': 'Ключ не указан'}), 400
    if not accounts.can(g.user, 'manage_base'):
        return _deny('Нет права удалять записи из базы отпечатков.')
    entry = load_store().get(key)
    if entry is None:
        abort(404)
    # Общая группа и see_all дают чтение, но не изменение. Чужую запись может
    # удалить только администратор с отдельным глобальным правом.
    if entry.get('owner', '') != g.user['login']:
        if (g.user.get('role') != 'admin'
                or not accounts.can(g.user, 'delete_all')):
            return _deny('Эта запись принадлежит другому преподавателю.')
    if not delete_entry(key):
        abort(404)
    return jsonify({'ok': True})


# ─────────────────────────  UI actions  ─────────────────────────

@app.route('/upload', methods=['POST'])
@permission_required('run_checks')
def upload():
    threshold = _float_field(request.form, 'threshold', 0.6, 0.0, 1.0)
    enabled = _parse_enabled_checks(request.form.get('gost'))
    use_memory = _parse_use_memory(request.form.get('use_memory'))
    weights, scale = _grade_params(request.form)
    job_id, error, status_code = _start_job(
        request.files.getlist('files'), threshold, g.user, enabled, use_memory,
        weights, scale)
    if error:
        return jsonify({'error': error}), status_code
    return jsonify({'job_id': job_id})


@app.route('/upload/start', methods=['POST'])
@permission_required('run_checks')
def upload_start():
    """Открыть частичную загрузку.

    Партия за курс – это гигабайты, одним запросом столько не проходит:
    упирается в лимит nginx и в таймаут. Браузер берёт отсюда номер загрузки и
    досылает файлы кусками.
    """
    _sweep_uploads()
    upload_id = uuid.uuid4().hex[:16]
    with uploads_lock:
        uploads[upload_id] = {
            'dir':   tempfile.mkdtemp(prefix=TMP_PREFIX_UPLOAD, dir=TMP_ROOT),
            'owner': g.user['login'],
            'bytes': 0,
            'at':    time.monotonic(),
            # ключ файла в партии → имя, под которым он лёг на диск
            'files': {},
        }
    return jsonify({
        'upload_id':  upload_id,
        'chunk_size': UPLOAD_CHUNK,
        'max_bytes':  UPLOAD_MAX_TOTAL,
    })


@app.route('/upload/part', methods=['POST'])
@permission_required('run_checks')
def upload_part():
    """Дописать кусок файла в открытую загрузку."""
    slot, error, code = _upload_slot(request.form.get('upload_id'))
    if error:
        return jsonify({'error': error}), code

    name = _safe_upload_name(request.form.get('name'))
    if name is None:
        return jsonify({'error': f'Принимаются {UPLOAD_EXTS_TEXT}'}), 400
    chunk = request.files.get('chunk')
    if chunk is None:
        return jsonify({'error': 'Кусок файла не передан'}), 400

    # Номер файла в партии присылает браузер: по одному имени два файла не
    # различить, и раньше второй «отчет.pdf» попадал в ветку «повтор после
    # обрыва» – сервер отвечал успехом, а работа терялась. Старые клиенты и
    # прямые вызовы номера не шлют: для них ключ – по-прежнему имя.
    key = (request.form.get('idx') or '').strip() or name
    with uploads_lock:
        stored = slot['files'].get(key)
        if stored is None:
            stored = _unique_name(slot['dir'], name,
                                  taken=set(slot['files'].values()))
            slot['files'][key] = stored

    # Имя уже очищено от пути, поэтому запись остаётся внутри каталога загрузки.
    dest = os.path.join(slot['dir'], stored)
    have = os.path.getsize(dest) if os.path.exists(dest) else 0
    offset = _int_field(request.form, 'offset', have, 0, UPLOAD_MAX_TOTAL)
    if offset > have:
        return jsonify({'error': 'Кусок пришёл не по порядку – начните заново',
                        'have': have}), 409
    if offset < have:
        # Повтор после обрыва: эти байты уже записаны, второй раз не дописываем.
        return jsonify({'ok': True, 'bytes': slot['bytes'], 'repeat': True})

    data = chunk.read()
    with uploads_lock:
        over = slot['bytes'] + len(data) > UPLOAD_MAX_TOTAL
        if not over:
            slot['bytes'] += len(data)
            slot['at'] = time.monotonic()
    if over:
        return jsonify({'error': f'Объём партии больше допустимых '
                                 f'{UPLOAD_MAX_MB} МБ'}), 413

    with open(dest, 'wb' if offset == 0 else 'ab') as out:
        out.write(data)
    return jsonify({'ok': True, 'bytes': slot['bytes']})


@app.route('/upload/finish', methods=['POST'])
@permission_required('run_checks')
def upload_finish():
    """Закрыть загрузку и запустить проверку по собранным файлам."""
    upload_id = request.form.get('upload_id')
    slot, error, code = _upload_slot(upload_id)
    if error:
        return jsonify({'error': error}), code

    with uploads_lock:
        uploads.pop(upload_id, None)

    threshold = _float_field(request.form, 'threshold', 0.6, 0.0, 1.0)
    enabled = _parse_enabled_checks(request.form.get('gost'))
    use_memory = _parse_use_memory(request.form.get('use_memory'))
    weights, scale = _grade_params(request.form)
    job_id, error, status_code = _launch(
        slot['dir'], threshold, g.user, enabled, use_memory, weights, scale)
    if error:
        return jsonify({'error': error}), status_code
    return jsonify({'job_id': job_id})


@app.route('/upload/cancel', methods=['POST'])
@permission_required('run_checks')
def upload_cancel():
    """Отменить загрузку – вкладку закрыли или связь оборвалась."""
    upload_id = request.form.get('upload_id')
    slot, error, code = _upload_slot(upload_id)
    if error:
        return jsonify({'error': error}), code
    with uploads_lock:
        uploads.pop(upload_id, None)
    shutil.rmtree(slot['dir'], ignore_errors=True)
    return jsonify({'ok': True})


@app.route('/status/<job_id>')
@login_required
def status(job_id):
    return _job_status(job_id)


@app.route('/report/<job_id>')
@login_required
def report(job_id):
    return _job_report(job_id)


@app.route('/export/<job_id>')
@login_required
def export_pdf(job_id):
    return _job_export(job_id)


@app.route('/jobs/clear', methods=['POST'])
@permission_required('delete_own')
def clear_jobs():
    return _clear_own()


@app.route('/jobs/clear/all', methods=['POST'])
@admin_required
def clear_all_jobs():
    return _clear_everything(_confirmation_login())


@app.route('/jobs/<job_id>/delete', methods=['POST'])
@login_required
def delete_job(job_id):
    return _delete_job(job_id)


@app.route('/jobs/<job_id>/cancel', methods=['POST'])
@login_required
def cancel_job(job_id):
    return _cancel_job(job_id)


@app.route('/memory')
@login_required
def memory_list():
    return _memory_summary()


@app.route('/memory/delete', methods=['POST'])
@login_required
def memory_delete():
    data = request.get_json(silent=True) or {}
    return _memory_delete(data.get('key', ''))


@app.route('/jobs')
@login_required
def list_jobs():
    return jsonify(_all_jobs(g.user))


@app.route('/jobs/active')
@login_required
def list_active_jobs():
    """Незаконченные проверки этой учётной записи, новые сверху.

    Нужны странице «Новая проверка»: её открывают заново – перезагрузили,
    вернулись с другой вкладки – и она должна снова показать идущую проверку,
    а не пустую форму. Чужие сюда не попадают даже у записи с правом видеть
    всё: это «мои идущие», а не «все идущие».
    """
    mine = [dict(job_id=jid, **data)
            for jid, data in _all_jobs(g.user).items()
            if data.get('status') == 'processing'
            and data.get('owner', '') == g.user['login']]
    mine.sort(key=lambda data: (job_store.parse_created_at(
        data.get('created_at')) or datetime.min), reverse=True)
    return jsonify(mine)


# ─────────────────────────  Account management  ─────────────────────────

def _must_change(form) -> bool:
    """Требовать ли смену пароля при первом входе.

    Настройка «Требовать смену пароля при первом входе» – нижняя граница:
    администратор может её ужесточить для отдельной записи, но не ослабить.
    Раньше настройка сохранялась и не читалась никем, то есть не делала ничего.
    """
    return (form.get('must_change') == 'on'
            or bool(accounts.get_settings().get('pw_require_change_first')))


@app.route('/admin/users/create', methods=['POST'])
@admin_required
def user_create():
    """Учётная запись заводится сразу с рабочим паролем: администратор
    задаёт его вместе с ФИО и передаёт лично. Временных паролей нет – не
    остаётся окна, когда в систему можно войти по строке из общей переписки."""
    form = request.form
    login_name = form.get('login', '').strip().lower()
    fio = form.get('fio', '').strip()
    password = form.get('password', '')
    repeat = form.get('repeat', '')

    if not login_name or not fio:
        flash('Укажите логин и ФИО')
    elif not re.fullmatch(r'[a-z0-9._-]{3,32}', login_name):
        flash('Логин: латиница, цифры, точка, дефис и подчёркивание, 3–32 символа')
    elif accounts.get_user(login_name) is not None:
        flash(f'Логин {login_name} уже занят')
    elif password != repeat:
        flash('Пароль и повтор не совпадают')
    elif accounts.password_problem(password):
        flash(accounts.password_problem(password))
    else:
        accounts.create_user(
            login=login_name, fio=fio, password=password,
            role=form.get('role', 'teacher'), email=form.get('email', '').strip(),
            must_change=_must_change(form))
        flash(f'Учётная запись {login_name} создана – сообщите пароль лично.')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<login_name>/perms', methods=['POST'])
@admin_required
def user_perms(login_name):
    user = accounts.get_user(login_name)
    if user is None:
        abort(404)
    role = request.form.get('role', user['role'])
    if user['login'] == g.user['login'] and role != 'admin':
        flash('Нельзя снять с себя роль администратора')
        return redirect(url_for('admin_users'))
    user['role'] = role
    user['perms'] = {flag: request.form.get(f'perm_{flag}') == 'on'
                     for flag, _ in accounts.PERMISSIONS}
    if role != 'admin':
        user['perms']['delete_all'] = False
    accounts.save_user(user)
    flash(f'Права обновлены: {user["fio"]}')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<login_name>/password', methods=['POST'])
@admin_required
def user_reset_password(login_name):
    """Новый пароль задаёт администратор – тем же способом, что и при
    создании записи."""
    user = accounts.get_user(login_name)
    if user is None:
        abort(404)
    password = request.form.get('password', '')
    problem = (accounts.password_problem(password)
               or ('Пароль и повтор не совпадают'
                   if password != request.form.get('repeat', '') else ''))
    if problem:
        flash(f'{user["fio"]}: {problem}')
    else:
        accounts.set_password(login_name, password,
                              must_change=_must_change(request.form))
        flash(f'Пароль для {user["fio"]} изменён – сообщите его лично.')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<login_name>/state', methods=['POST'])
@admin_required
def user_state(login_name):
    user = accounts.get_user(login_name)
    if user is None:
        abort(404)
    if user['login'] == g.user['login']:
        flash('Нельзя заблокировать собственную учётную запись')
        return redirect(url_for('admin_users'))
    blocking = user['state'] != 'blocked'
    user['state'] = 'blocked' if blocking else 'active'
    if not blocking:
        user['fail_count'] = 0
        user['locked_until'] = ''
    accounts.save_user(user)
    flash(f'{user["fio"]}: {"заблокирован" if blocking else "разблокирован"}')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<login_name>/apikey', methods=['POST'])
@admin_required
def user_apikey(login_name):
    user = accounts.get_user(login_name)
    if user is None:
        abort(404)
    key = accounts.issue_api_key(login_name)
    flash(f'Ключ API для {user["fio"]}: {key} – сохраните, второй раз он не покажется')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<login_name>/delete', methods=['POST'])
@admin_required
def user_delete(login_name):
    if login_name == g.user['login']:
        flash('Нельзя удалить собственную учётную запись')
        return redirect(url_for('admin_users'))
    if accounts.delete_user(login_name):
        # Из групп логин убираем сразу: иначе он остался бы в составе строкой
        # без учётной записи, а заведённый позже тёзка с тем же логином молча
        # получил бы доступ к общей базе группы.
        teams.drop_member(login_name)
        flash('Учётная запись удалена. Её проверки и отпечатки сохранены.')
    return redirect(url_for('admin_users'))


# ─────────────────────────  Teacher groups  ─────────────────────────

def _team_members_form(form) -> list:
    """Логины из формы состава – только существующие учётные записи.

    Состав приходит галочками, а список для них строится из тех же учётных
    записей, поэтому чужой логин здесь появиться может только подделкой формы.
    Отсеиваем его тут, а не при чтении: несуществующий владелец в составе
    ничего не откроет, но будет висеть в таблице необъяснимой строкой.
    """
    known = set(accounts.load_users())
    return [lg for lg in form.getlist('members') if lg in known]


@app.route('/admin/teams')
@admin_required
def admin_teams():
    """Группы преподавателей: общая база отпечатков на несколько записей."""
    users = sorted(accounts.load_users().values(),
                   key=lambda u: (u['role'] != 'admin', u.get('fio', '')))
    fio_by_login = {u['login']: u.get('fio', u['login']) for u in users}

    from checker.memory_store import load_store
    prints_by_owner = {}
    for entry in load_store().values():
        login = entry.get('owner', '')
        prints_by_owner[login] = prints_by_owner.get(login, 0) + 1

    rows = sorted(teams.load_teams().values(), key=lambda t: t.get('name', ''))
    for team in rows:
        members = team.get('members') or []
        team['member_list'] = [
            {'login': lg, 'fio': fio_by_login.get(lg, lg),
             'prints': prints_by_owner.get(lg, 0)} for lg in members]
        team['prints'] = sum(prints_by_owner.get(lg, 0) for lg in members)

    return render_template('teams.html', page='teams', teams=rows, users=users,
                           prints_by_owner=prints_by_owner)


@app.route('/admin/teams/create', methods=['POST'])
@admin_required
def team_create():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Укажите название группы')
    else:
        team = teams.create_team(name, _team_members_form(request.form))
        flash(f'Группа «{team["name"]}» создана. '
              f'Участников: {len(team["members"])}.')
    return redirect(url_for('admin_teams'))


@app.route('/admin/teams/<team_id>/save', methods=['POST'])
@admin_required
def team_save(team_id):
    team = teams.get_team(team_id)
    if team is None:
        abort(404)
    name = request.form.get('name', '').strip()
    if not name:
        flash('Название группы не может быть пустым')
        return redirect(url_for('admin_teams'))
    team['name'] = name[:teams.NAME_MAX]
    team['members'] = _team_members_form(request.form)
    teams.save_team(team)
    flash(f'Группа «{team["name"]}» сохранена. '
          f'Участников: {len(team["members"])}.')
    return redirect(url_for('admin_teams'))


@app.route('/admin/teams/<team_id>/delete', methods=['POST'])
@admin_required
def team_delete(team_id):
    team = teams.get_team(team_id)
    if team is None:
        abort(404)
    teams.delete_team(team_id)
    # Отпечатки принадлежат преподавателям, а не группе: роспуск группы просто
    # возвращает каждому его личную базу.
    flash(f'Группа «{team.get("name", "")}» распущена. '
          f'Отпечатки участников остались у их владельцев.')
    return redirect(url_for('admin_teams'))


# ─────────────────────────  Processing  ─────────────────────────

SAVE_EVERY = 1.0    # как часто состояние уходит в хранилище, секунд
TICK_EVERY = 0.4    # как часто пересчитывается процент внутри этапа, секунд


class JobCancelled(Exception):
    """Проверку прервал преподаватель – это не сбой, а решение."""


def _update(job_id: str, _force: bool = False, **kwargs):
    """Обновить состояние проверки.

    В памяти пишем всегда – страница берёт статус оттуда. На диск (или в
    PostgreSQL) сбрасываем не чаще раза в секунду: на партии из сотни отчётов
    прогресс меняется сотни раз, и писать файл проверки на каждый шаг дороже
    самой проверки. Смена статуса сохраняется немедленно – по ней восстанавливают
    историю после перезапуска.
    """
    with jobs_lock:
        job = jobs.get(job_id)
        if job is not None:
            job.update(kwargs)
            now = time.monotonic()
            if (_force or 'status' in kwargs
                    or now - job.get('_saved_at', 0.0) >= SAVE_EVERY):
                job['_saved_at'] = now
                # Отметка «проверка жива»: по ней отличают идущую проверку от
                # брошенной вместе с умершим процессом (см. _mark_if_stale).
                job['beat'] = time.time()
                # Save under the same lock used by bulk clear. Otherwise a
                # delayed snapshot could be written after clear and resurrect
                # a job that the operation had just removed.
                job_store.save(job_id, _public_job(job))


def _stop_point(job_id: str):
    """Прерваться, если преподаватель попросил остановить проверку.

    Ставится между этапами – там, где _tick не вызывается и до следующего
    сообщения о ходе могут пройти минуты.
    """
    with jobs_lock:
        if (jobs.get(job_id) or {}).get('_cancel'):
            raise JobCancelled


def _tick(job_id: str, lo: int, hi: int, done: int, total: int, step: str):
    """Процент внутри длинного этапа: lo…hi пропорционально done/total.

    Без этого полоса замирает на одном значении на всё время сравнения пар –
    на большой партии это минуты, и проверка выглядит зависшей.

    Здесь же точка остановки внутри длинных циклов: отметку об отмене смотрим
    на каждом шаге, не считаясь с троттлингом, иначе «Прервать» отзывалось бы
    через полсекунды после нажатия только по случайности.
    """
    now = time.monotonic()
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return
        if job.get('_cancel'):
            raise JobCancelled
        if done < total and now - job.get('_ticked_at', 0.0) < TICK_EVERY:
            return
        job['_ticked_at'] = now
    share = done / total if total else 1.0
    _update(job_id, progress=int(lo + (hi - lo) * min(share, 1.0)), step=step)


def _beat(job_id: str, stop: threading.Event):
    """Отмечать в хранилище, что проверка ещё идёт.

    Между шагами бывают долгие паузы – сборка отчёта на сотне работ занимает
    минуты и не обновляет ни процент, ни надпись. Без отметки такую проверку
    сочли бы брошенной и погасили ошибкой.
    """
    while not stop.wait(JOB_BEAT_EVERY):
        _update(job_id, _force=True)


def _collect_docs(job_id: str, tmp_dir: str) -> tuple:
    """Распаковать архивы, привести DOCX/ODT/DOC к PDF и собрать список работ.

    Возвращает (пути к PDF, {путь: имя загруженного файла}, [(путь, причина)]).

    Имена нужны отдельно: после конвертации работа лежит под своим именем с
    расширением .pdf, а в отчёте и в ведомости должно стоять «Иванов.docx».
    Третьим списком идут работы, которые прочитать не удалось, – они попадают
    в отчёт карточкой с ошибкой и не пропадают из ведомости молча.

    Живёт в потоке проверки, а не в обработчике запроса: на архиве в сотни
    работ распаковка и конвертация занимают минуты, и всё это время браузер не
    получал бы даже номера проверки.
    """
    archives = sorted(Path(tmp_dir).glob('*.[zZ][iI][pP]'))
    for k, arc in enumerate(archives):
        _extract_zip(
            str(arc), tmp_dir,
            on_file=lambda done, total, k=k, name=arc.name: _tick(
                job_id, 0, 2,
                round((k + done / max(total, 1)) * 100), len(archives) * 100,
                f'Распаковка {name[:50]}: {done} из {total}…'),
        )
        arc.unlink(missing_ok=True)

    # Список составляется до конвертации: её результаты ложатся сюда же, и
    # обход после неё вернул бы каждый переведённый документ вторым разом.
    found = sorted((p for p in Path(tmp_dir).rglob('*')
                    if p.is_file() and p.suffix.lower() in DOC_EXTS),
                   key=lambda p: p.name.lower())
    origins = {str(p): p.name for p in found}
    ready   = [p for p in found if p.suffix.lower() == '.pdf']
    sources = [p for p in found if p.suffix.lower() != '.pdf']
    paths   = [str(p) for p in ready]

    if not sources:
        return paths, origins, []

    if not convert.available():
        # Одной строкой на всю партию: иначе о том, что конвертера нет,
        # сообщала бы каждая работа по очереди.
        _update(job_id, step=f'{convert.NO_CONVERTER}. '
                             f'Не принято работ: {len(sources)}')
        return paths, origins, [(str(p), convert.NO_CONVERTER) for p in sources]

    failures = []
    # Имя результата разводим заранее: «Иванов.docx» и «Иванов.pdf» из одной
    # партии иначе дали бы одинаковый путь после конвертации, а путь служит
    # ключом работы в матрицах сравнения и сводке отчёта.
    used = {p.stem for p in ready}
    for i, src in enumerate(sources):
        _tick(job_id, 2, 4, i, len(sources),
              f'Конвертация {i+1} из {len(sources)}: {src.name[:60]}…')
        stem, k = src.stem, 1
        while stem in used:
            k += 1
            stem = f'{src.stem}_{k}'
        used.add(stem)
        try:
            out = convert.to_pdf(str(src), tmp_dir, stem)
        except convert.ConversionError as e:
            failures.append((str(src), str(e)))
            continue
        origins[out] = src.name
        paths.append(out)

    paths.sort(key=lambda p: origins[p].lower())
    return paths, origins, failures


# Сбои, о которых стоит сказать по-человечески: преподаватель видит эту строку
# вместо отчёта, и «[Errno 36] File name too long» ему ничего не объясняет.
_OS_REASON = {
    errno.ENAMETOOLONG: 'слишком длинное имя файла',
    errno.ENOSPC:       'на сервере кончилось место на диске',
    errno.EACCES:       'серверу не хватило прав на временный каталог',
    errno.EMFILE:       'на сервере кончились свободные файловые дескрипторы',
}


def _error_text(exc: Exception, step: str) -> str:
    """Сообщение об обрыве проверки. Подробности остаются в поле error."""
    if isinstance(exc, MemoryError):
        reason = ('серверу не хватило памяти на этой партии – попробуйте '
                  'разделить её на части')
    elif isinstance(exc, OSError) and exc.errno in _OS_REASON:
        reason = _OS_REASON[exc.errno]
    else:
        reason = f'{type(exc).__name__}: {exc}'
    tail = f' Последний шаг: «{step}».' if step else ''
    return f'Проверка прервана: {reason}.{tail}'


def _process_job(job_id: str, tmp_dir: str, threshold: float,
                 owner: str, enabled_checks=None, use_memory=True,
                 weights=None, scale=grading.DEFAULT_SCALE):
    stop_beat = threading.Event()
    threading.Thread(target=_beat, args=(job_id, stop_beat), daemon=True).start()
    try:
        from checker.memory_store import (load_store, to_virtual_report,
                                          add_reports, student_id, NO_STUDENT)

        # 0. Unpack what was uploaded and bring every format to PDF.
        pdf_paths, origins, failures = _collect_docs(job_id, tmp_dir)
        n = len(pdf_paths) + len(failures)
        if not n:
            _update(job_id, status='error', progress=0,
                    step='Ошибка: работы не найдены в загруженных данных '
                         '(принимаются PDF, DOCX, ODT, DOC)')
            return
        _update(job_id, progress=4, total=n, step=f'К проверке принято работ: {n}')

        # 1. Load the base visible to this teacher: their own entries plus
        #    those of colleagues in their groups. Loaded even with
        #    use_memory=False – step 8 appends to it.
        store = load_store(teams.visible_owners(owner))
        if use_memory:
            _update(job_id, progress=5, step=f'База загружена: {len(store)} записей')
        else:
            _update(job_id, progress=5, step='Сравнение с базой отключено')

        # 2. Extract
        _stop_point(job_id)
        reports = []
        for i, path in enumerate(pdf_paths):
            _tick(job_id, 5, 44, i, n,
                  f'Извлечение {i+1} из {n}: {origins.get(path, "")[:60]}…')
            reports.append(extract_report(path, origins.get(path, '')))
            _update(job_id, done_files=i + 1)
        # Непрочитанные работы идут дальше наравне с прочитанными: карточка с
        # ошибкой в отчёте нужна ровно затем, чтобы работа не исчезла из
        # ведомости незаметно для преподавателя.
        for path, reason in failures:
            reports.append(extract_report(path, origins.get(path, ''), reason))
        _update(job_id, progress=44, step='Извлечение завершено', done_files=n)

        # 3. GOST
        for i, r in enumerate(reports):
            _tick(job_id, 44, 52, i, n,
                  f'Проверка ГОСТ 7.32-2017: {i+1} из {n}…')
            r['gost_results'] = check_gost(r, enabled_checks)

        # 4. Build historical list, excluding recognized students in this batch.
        #    Without this, a student's new report would match their own stored
        #    fingerprint from a previous session (false "self-plagiarism").
        #    Отбор идёт по «фио|группа», а не по ключу записи: в общей базе
        #    группы преподавателей ту же работу мог сохранить коллега, под
        #    своим владельцем, и по ключу она бы не отсеялась – пересдача у
        #    другого преподавателя показывалась бы как стопроцентное
        #    заимствование у самого себя. Для работы без ФИО и группы точечное
        #    исключение той же версии делается сравнителями по имени файла и
        #    отпечатку нормализованного текста: остальные анонимные работы
        #    должны остаться в сравнении.
        new_students = {sid for r in reports
                        if (sid := student_id(r.get('student', {}))) != NO_STUDENT}
        if use_memory:
            historical = [
                to_virtual_report(k, v) for k, v in store.items()
                if student_id(v.get('student', {})) not in new_students
            ]
            _update(job_id, step=f'Сравниваем с базой: {len(historical)} чужих отчётов')
        else:
            historical = []

        # 5. Text plagiarism (new + relevant historical)
        _stop_point(job_id)
        pairs_count = n * (n - 1) // 2 + n * len(historical)
        _update(job_id, progress=52,
                step=f'Анализ текста ({pairs_count} пар, вкл. базу)…')
        text_plag = check_text_plagiarism(
            reports + historical, threshold=threshold,
            on_progress=lambda done, total: _tick(
                job_id, 52, 75, done, total,
                f'Анализ текста: {done} из {total} пар…'))
        flagged = len(text_plag.get('pairs', []))
        _update(job_id, progress=75, text_pairs=flagged,
                step=f'Текст: {flagged} подозрительных пар')

        # 6. Image plagiarism (new + historical)
        _stop_point(job_id)
        total_imgs = sum(len(r.get('images', [])) for r in reports)
        _update(job_id, progress=76,
                step=f'Анализ изображений ({total_imgs} шт.)…')
        img_plag = check_image_plagiarism(
            reports + historical,
            on_progress=lambda done, total: _tick(
                job_id, 76, 88, done, total,
                f'Анализ изображений: {done} из {total} пар…'))
        img_flagged = len([p for p in img_plag.get('pairs', [])
                           if not p.get('ui_review')])
        _update(job_id, img_pairs=img_flagged)

        # 7. Generate HTML report. Последняя точка остановки: дальше отчёт уже
        #    собран, и бросать работу за секунду до конца незачем.
        _stop_point(job_id)
        _update(job_id, progress=88, step='Генерация HTML-отчёта…')
        html = generate_html_report(
            reports, historical, text_plag, img_plag, threshold, job_id=job_id,
            weights=weights, scale=scale
        )
        (REPORTS_DIR / f'{job_id}.html').write_text(html, encoding='utf-8')

        # 8. Persist fingerprints to this teacher's base. The JSON backend
        #    replaces its growing file once per batch, not once per report;
        #    version assignment remains atomic inside memory_store.
        _update(job_id, progress=95, step='Сохранение отпечатков в базу…')
        saveable = [r for r in reports if not r.get('error')]
        add_reports(saveable, job_id, owner)
        saved = len(saveable)

        not_read = (f' Не удалось прочитать: {len(failures)}.' if failures else '')
        _update(job_id,
                status='done',
                progress=100,
                summary=summary_mod.build(reports, historical, text_plag,
                                          img_plag, threshold, weights, scale),
                step=f'Готово! Проверено {n} отчётов, '
                     f'сохранено в базу: {saved}.{not_read}')

    except JobCancelled:
        # Ни отчёта, ни отпечатков: последняя точка остановки стоит перед седьмым
        # шагом, а сборка отчёта и запись в базу идут уже без них.
        _update(job_id, status='cancelled',
                step='Проверка прервана преподавателем. Отчёт не сформирован, '
                     'отпечатки в базу не сохранены.')

    except Exception as exc:
        import traceback
        with jobs_lock:
            last_step = (jobs.get(job_id) or {}).get('step', '')
        _update(job_id,
                status='error',
                step=_error_text(exc, last_step),
                error=traceback.format_exc())
    finally:
        stop_beat.set()
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ─────────────────────────  REST API  ─────────────────────────

api = Blueprint('api', __name__, url_prefix='/api/v1')


@api.errorhandler(HTTPException)
def api_error(exc):
    """Always answer the API with JSON, never an HTML error page."""
    return jsonify({'error': exc.description, 'status': exc.code}), exc.code


@api.route('/health')
def api_health():
    return jsonify({'ok': True, 'weasyprint': WEASYPRINT_OK, 'db': db.DB_ENABLED})


@api.route('/jobs', methods=['POST'])
@api_auth_required
def api_create_job():
    """Start a check. Multipart form: files=<pdf|docx|odt|doc|zip>… ,
    threshold=0.0-1.0,
    gost=<comma-separated check codes> (optional; omit for all checks),
    use_memory=0|1 (optional; 0 skips comparison against the stored base),
    weights=<S1:100,F2:40> (optional; per-criterion weight for the recommended
    grade), scale=<2..100> (optional; grade scale, 100 = percent)."""
    if not accounts.can(g.user, 'run_checks'):
        return jsonify({'error': 'Нет права запускать проверки'}), 403
    threshold = _float_field(request.form, 'threshold', 0.6, 0.0, 1.0)
    enabled = _parse_enabled_checks(request.form.get('gost'))
    use_memory = _parse_use_memory(request.form.get('use_memory'))
    weights, scale = _grade_params(request.form)
    job_id, error, status_code = _start_job(
        request.files.getlist('files'), threshold, g.user, enabled, use_memory,
        weights, scale)
    if error:
        return jsonify({'error': error}), status_code
    return jsonify({
        'job_id': job_id,
        'status': 'processing',
        'links': {
            'self':   f'/api/v1/jobs/{job_id}',
            'report': f'/api/v1/jobs/{job_id}/report',
            'export': f'/api/v1/jobs/{job_id}/export',
        },
    }), 201


@api.route('/jobs', methods=['GET'])
@api_auth_required
def api_list_jobs():
    return jsonify(_all_jobs(g.user))


@api.route('/jobs', methods=['DELETE'])
@api_auth_required
def api_clear_jobs():
    scope = request.args.get('scope')
    if scope not in ('own', 'all'):
        return jsonify({'error': 'Укажите обязательный параметр scope=own|all'}), 400
    if scope == 'own':
        return _clear_own()
    return _clear_everything(request.args.get('confirm_login', ''))


@api.route('/jobs/<job_id>', methods=['GET'])
@api_auth_required
def api_job_status(job_id):
    return _job_status(job_id)


@api.route('/jobs/<job_id>', methods=['DELETE'])
@api_auth_required
def api_delete_job(job_id):
    return _delete_job(job_id)


@api.route('/jobs/<job_id>/cancel', methods=['POST'])
@api_auth_required
def api_cancel_job(job_id):
    return _cancel_job(job_id)


@api.route('/jobs/<job_id>/report', methods=['GET'])
@api_auth_required
def api_job_report(job_id):
    return _job_report(job_id)


@api.route('/jobs/<job_id>/export', methods=['GET'])
@api_auth_required
def api_job_export(job_id):
    return _job_export(job_id)


@api.route('/memory', methods=['GET'])
@api_auth_required
def api_memory_list():
    return _memory_summary()


@api.route('/memory/<path:key>', methods=['DELETE'])
@api_auth_required
def api_memory_delete(key):
    return _memory_delete(key)


app.register_blueprint(api)


if __name__ == '__main__':
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    print(f'  Сервер запущен: http://localhost:{port}')
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
