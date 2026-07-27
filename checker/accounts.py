"""Accounts, permissions, login journal and system settings.

Two roles:
  admin   — sees every check and the whole fingerprint base, manages accounts
            and system settings.
  teacher — fully isolated: own checks, own fingerprint base. Plagiarism is
            searched only inside that base.

Backed by PostgreSQL when DATABASE_URL is set, otherwise by JSON files next to
the fingerprint store (same fallback strategy as checker/memory_store.py).
"""

import json
import os
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from checker import db

DATA_DIR    = Path(__file__).parent.parent / 'memory'
USERS_PATH  = DATA_DIR / 'users.json'
LOG_PATH    = DATA_DIR / 'login_log.json'
CONF_PATH   = DATA_DIR / 'settings.json'
LOG_KEEP    = 2000            # JSON fallback keeps the journal bounded

_lock = threading.Lock()

ROLES = {'admin': 'Администратор', 'teacher': 'Преподаватель'}

STATES = {
    'active':  'Активен',
    'blocked': 'Заблокирован',
    'pending': 'Ждёт смены пароля',
}

# Permission flags, in the order the admin UI shows them.
PERMISSIONS = [
    ('run_checks',  'Запускать проверки и открывать отчёты'),
    ('delete_own',  'Удалять свои проверки'),
    ('manage_base', 'Удалять записи из базы отпечатков'),
    ('see_all',     'Видеть проверки других преподавателей'),
    ('use_api',     'Пользоваться API по ключу'),
]

_DEFAULT_PERMS = {
    'admin':   {'run_checks': True, 'delete_own': True, 'manage_base': True,
                'see_all': True,  'use_api': True},
    'teacher': {'run_checks': True, 'delete_own': True, 'manage_base': False,
                'see_all': False, 'use_api': False},
}

DEFAULT_SETTINGS = {
    'default_threshold':       0.6,
    'default_gost':            [],      # empty list = all checks
    'gost_weights':            {},      # код критерия → 0…100; нет ключа = 100
    'grade_scale':             100,     # 100 = оценка в процентах, иначе баллы
    'retention_days':          0,       # 0 = keep reports forever
    'pw_min_len':              10,
    'pw_require_change_first': True,
    'pw_expire_days':          0,
    'lock_after_fails':        5,
    'lock_minutes':            15,
}

_STAMP = '%d.%m.%Y %H:%M'
_STAMP_SEC = '%d.%m.%Y %H:%M:%S'


def default_perms(role: str) -> dict:
    return dict(_DEFAULT_PERMS.get(role, _DEFAULT_PERMS['teacher']))


# JSON fallback helpers

def _read_json(path: Path, fallback):
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return fallback


def _write_json(path: Path, value) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2),
                    encoding='utf-8')


# Accounts

def load_users() -> dict:
    if db.DB_ENABLED:
        return db.users_load_all()
    return _read_json(USERS_PATH, {})


def get_user(login: str):
    if not login:
        return None
    return load_users().get(login)


def save_user(user: dict) -> None:
    if db.DB_ENABLED:
        db.users_save(user)
        return
    with _lock:
        users = _read_json(USERS_PATH, {})
        users[user['login']] = user
        _write_json(USERS_PATH, users)


def delete_user(login: str) -> bool:
    if db.DB_ENABLED:
        return db.users_delete(login)
    with _lock:
        users = _read_json(USERS_PATH, {})
        if login not in users:
            return False
        del users[login]
        _write_json(USERS_PATH, users)
    return True


def create_user(login: str, fio: str, password: str, role: str = 'teacher',
                email: str = '', perms=None, must_change: bool = True) -> dict:
    user = {
        'login':         login,
        'fio':           fio,
        'email':         email,
        'role':          role,
        'state':         'pending' if must_change else 'active',
        'password_hash': generate_password_hash(password),
        'perms':         perms if perms is not None else default_perms(role),
        'api_key':       None,
        'must_change':   must_change,
        'created_at':    datetime.now().strftime(_STAMP),
        'last_login':    '',
        'fail_count':    0,
        'locked_until':  '',
    }
    save_user(user)
    return user


def set_password(login: str, password: str, must_change: bool = False) -> bool:
    user = get_user(login)
    if user is None:
        return False
    user['password_hash'] = generate_password_hash(password)
    user['must_change'] = must_change
    user['fail_count'] = 0
    user['locked_until'] = ''
    if user['state'] == 'pending' and not must_change:
        user['state'] = 'active'
    elif must_change:
        user['state'] = 'pending'
    save_user(user)
    return True


def issue_api_key(login: str):
    """Generate and store a new API key for the account. Returns the key."""
    user = get_user(login)
    if user is None:
        return None
    key = secrets.token_hex(24)
    user['api_key'] = key
    save_user(user)
    return key


def user_by_api_key(key: str):
    if not key:
        return None
    for user in load_users().values():
        stored = user.get('api_key')
        if stored and secrets.compare_digest(stored, key):
            return user
    return None


def can(user, flag: str) -> bool:
    """Permission check. Admins always keep account management; every other
    flag is read from the account so it can be revoked individually."""
    if not user:
        return False
    return bool((user.get('perms') or {}).get(flag, False))


def password_problem(password: str) -> str:
    """Return a human-readable reason the password is unacceptable, or ''."""
    conf = get_settings()
    min_len = int(conf.get('pw_min_len', 10))
    if len(password) < min_len:
        return f'Пароль короче {min_len} символов'
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return 'Пароль должен содержать и буквы, и цифры'
    return ''


# Login journal

def record_event(login: str, ok: bool, ip: str, ua: str, reason: str = '') -> None:
    event = {
        'ts':     datetime.now().strftime(_STAMP_SEC),
        'login':  login,
        'ok':     ok,
        'ip':     ip,
        'ua':     ua,
        'reason': reason,
    }
    if db.DB_ENABLED:
        db.log_add(event)
        return
    with _lock:
        log = _read_json(LOG_PATH, [])
        log.insert(0, event)
        _write_json(LOG_PATH, log[:LOG_KEEP])


def add_events(events: list) -> int:
    """Записать готовые события журнала, как есть — с их собственным временем.

    Нужно загрузке дампа: record_event() проставил бы текущую дату и журнал
    перестал бы соответствовать перенесённой истории.
    """
    if not events:
        return 0
    if db.DB_ENABLED:
        for event in events:
            db.log_add(event)
        return len(events)
    with _lock:
        log = _read_json(LOG_PATH, [])
        log = list(reversed(events)) + log
        _write_json(LOG_PATH, log[:LOG_KEEP])
    return len(events)


def recent_logins(limit: int = 200, login=None) -> list:
    if db.DB_ENABLED:
        return db.log_recent(limit, login)
    log = _read_json(LOG_PATH, [])
    if login is not None:
        log = [e for e in log if e.get('login') == login]
    return log[:limit]


# Authentication

def _locked_for(user: dict) -> int:
    """Minutes of lockout still to run, 0 when the account is not locked."""
    until = user.get('locked_until') or ''
    if not until:
        return 0
    try:
        left = datetime.fromisoformat(until) - datetime.now()
    except ValueError:
        return 0
    return max(0, int(left.total_seconds() // 60) + 1) if left.total_seconds() > 0 else 0


def authenticate(login: str, password: str, ip: str, ua: str):
    """Check credentials and write the attempt to the journal.

    Returns (user, error_message). user is None whenever error_message is set.
    """
    conf = get_settings()
    user = get_user(login)

    if user is None:
        record_event(login, False, ip, ua, 'учётной записи не существует')
        return None, 'Неверный логин или пароль'

    if user.get('state') == 'blocked':
        record_event(login, False, ip, ua, 'учётная запись заблокирована')
        return None, 'Учётная запись заблокирована. Обратитесь к администратору.'

    left = _locked_for(user)
    if left:
        record_event(login, False, ip, ua, 'вход временно закрыт')
        return None, f'Слишком много неудачных попыток. Вход откроется через {left} мин.'

    if not check_password_hash(user['password_hash'], password):
        user['fail_count'] = int(user.get('fail_count', 0)) + 1
        limit = int(conf.get('lock_after_fails', 5))
        reason = 'неверный пароль'
        if limit and user['fail_count'] >= limit:
            minutes = int(conf.get('lock_minutes', 15))
            user['locked_until'] = (datetime.now() + timedelta(minutes=minutes)).isoformat()
            user['fail_count'] = 0
            reason = f'неверный пароль, вход закрыт на {minutes} мин.'
        save_user(user)
        record_event(login, False, ip, ua, reason)
        return None, 'Неверный логин или пароль'

    user['fail_count'] = 0
    user['locked_until'] = ''
    user['last_login'] = datetime.now().strftime(_STAMP)
    save_user(user)
    record_event(login, True, ip, ua)
    return user, ''


# System settings

def get_settings() -> dict:
    stored = db.settings_load() if db.DB_ENABLED else _read_json(CONF_PATH, {})
    conf = dict(DEFAULT_SETTINGS)
    conf.update(stored or {})
    return conf


def save_settings(conf: dict) -> None:
    merged = get_settings()
    merged.update(conf)
    if db.DB_ENABLED:
        db.settings_save(merged)
        return
    with _lock:
        _write_json(CONF_PATH, merged)


# First run

def bootstrap() -> None:
    """Create the first administrator when the account list is empty, using
    AU_USERNAME / AU_PASSWORD so existing installations keep working."""
    try:
        if load_users():
            return
    except Exception:
        return   # storage not reachable yet; retried on the next request

    login = os.environ.get('AU_USERNAME', 'admin').strip() or 'admin'
    password = os.environ.get('AU_PASSWORD', 'admin')
    user = create_user(
        login=login,
        fio=os.environ.get('AU_FIO', 'Администратор системы'),
        password=password,
        role='admin',
        must_change=(password in ('admin', 'changeme', '')),
    )
    legacy_key = os.environ.get('AU_API_KEY', '').strip()
    if legacy_key:
        user['api_key'] = legacy_key
        save_user(user)
