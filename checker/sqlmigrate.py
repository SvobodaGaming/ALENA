"""Миграция базы: выгрузка всего хранилища в SQL и загрузка дампа обратно.

Выгрузка идёт через общий слой хранения, поэтому одинаково работает на
PostgreSQL и на JSON-файлах: дамп с локальной машины разворачивается на сервере
с Postgres и наоборот. Файл — обычный SQL, его же можно скормить psql.

Загрузка присланный SQL **не выполняет**. Файл разбирается собственным
разборщиком, который понимает ровно тот диалект, что пишет dump(): комментарии,
BEGIN/COMMIT, CREATE/ALTER/DELETE пропускаются, из INSERT-ов берутся значения,
и записываются они через те же функции хранилища, что и обычная работа
приложения. Так право «восстановить базу» не превращается в право выполнить
произвольную команду в СУБД.
"""

import json
import re
from datetime import datetime

from checker import accounts, db, job_store, memory_store

TABLES = ('users', 'jobs', 'fingerprints', 'login_events', 'settings')

_JOB_COLS = ('job_id', 'data')
_FP_COLS = ('entry_key', 'key_base', 'version', 'filename', 'student',
            'normalized_text', 'image_data', 'pages_count', 'job_id',
            'added_at', 'owner')
_EVENT_COLS = ('ts', 'login', 'ok', 'ip', 'ua', 'reason')

LOG_LIMIT = 100000     # журнал входов выгружается целиком, но не бесконечно


# ─────────────────────────  Выгрузка  ─────────────────────────

def _quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _lit(value) -> str:
    """Значение как литерал PostgreSQL."""
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (dict, list)):
        return _quote(json.dumps(value, ensure_ascii=False)) + '::jsonb'
    return _quote(value)


def _insert(table: str, columns: tuple, values: list) -> str:
    cols = ', '.join(columns)
    vals = ', '.join(_lit(v) for v in values)
    return f'INSERT INTO {table} ({cols}) VALUES ({vals});'


def counts() -> dict:
    """Сколько чего лежит в хранилище — для страницы миграции."""
    return {
        'users':        len(accounts.load_users()),
        'jobs':         len(job_store.load_all()),
        'fingerprints': len(memory_store.load_store()),
        'login_events': len(accounts.recent_logins(LOG_LIMIT)),
    }


def dump() -> str:
    """Полный дамп хранилища: схема, очистка таблиц и данные."""
    users  = accounts.load_users()
    jobs   = job_store.load_all()
    prints = memory_store.load_store()
    events = accounts.recent_logins(LOG_LIMIT)
    conf   = accounts.get_settings()

    out = [
        '-- АЛЁНА — Автоматический Ловец Ёрничества, Небрежности и Аутентичности',
        '-- Дамп базы данных. #au_team',
        f'-- Сформирован: {datetime.now().strftime("%d.%m.%Y %H:%M")}',
        f'-- Источник: {"PostgreSQL" if db.DB_ENABLED else "JSON-файлы в папке memory/"}',
        f'-- Учётных записей: {len(users)}, проверок: {len(jobs)}, '
        f'отпечатков: {len(prints)}, записей журнала: {len(events)}',
        '--',
        '-- Готовые HTML-отчёты (папка reports/) в дамп не входят — переносите её',
        '-- отдельно, иначе кнопка «Открыть отчёт» у старых проверок вернёт 404.',
        '',
        'BEGIN;',
        '',
        '-- Схема (создаётся, если её ещё нет)',
    ]
    out += [f'{stmt.strip()};' for stmt in db.SCHEMA]

    out += [
        '',
        '-- Полная замена содержимого. Если дамп нужно дописать к уже имеющимся',
        '-- данным, удалите эти пять строк.',
        'DELETE FROM login_events;',
        'DELETE FROM jobs;',
        'DELETE FROM fingerprints;',
        'DELETE FROM users;',
        'DELETE FROM settings;',
        '',
        f'-- Учётные записи ({len(users)})',
    ]
    for user in users.values():
        out.append(_insert('users', db.USER_COLS,
                           [user.get(c) for c in db.USER_COLS]))

    out += ['', f'-- История проверок ({len(jobs)})']
    for job_id, data in jobs.items():
        out.append(_insert('jobs', _JOB_COLS, [job_id, data]))

    out += ['', f'-- База отпечатков ({len(prints)})']
    for entry_key, entry in prints.items():
        values = [entry_key] + [entry.get(c) for c in _FP_COLS[1:]]
        out.append(_insert('fingerprints', _FP_COLS, values))

    out += ['', f'-- Журнал входов ({len(events)})']
    for event in reversed(events):        # в БД — от старых к новым
        out.append(_insert('login_events', _EVENT_COLS,
                           [event.get(c) for c in _EVENT_COLS]))

    out += [
        '',
        '-- Системные настройки',
        _insert('settings', ('key', 'value'), ['app', conf]),
        '',
        'COMMIT;',
        '',
    ]
    return '\n'.join(out)


# ─────────────────────────  Разбор дампа  ─────────────────────────

_INSERT_RE = re.compile(
    r'^INSERT\s+INTO\s+(\w+)\s*\(([^)]*)\)\s*VALUES\s*\((.*)\)$',
    re.IGNORECASE | re.DOTALL)


def _statements(text: str):
    """Инструкции SQL из текста: комментарии убраны, строки не разрезаются."""
    buf, in_str, in_comment, dash = [], False, False, False
    for ch in text:
        if in_comment:
            if ch == '\n':
                in_comment = False
                buf.append(ch)
            continue
        if in_str:
            buf.append(ch)
            if ch == "'":
                in_str = False
            continue
        if ch == "'":
            in_str = True
            buf.append(ch)
            dash = False
            continue
        if ch == '-':
            if dash:
                buf.pop()          # начало комментария «--»
                in_comment = True
                dash = False
            else:
                buf.append(ch)
                dash = True
            continue
        dash = False
        if ch == ';':
            stmt = ''.join(buf).strip()
            if stmt:
                yield stmt
            buf = []
            continue
        buf.append(ch)
    tail = ''.join(buf).strip()
    if tail:
        yield tail


def _split_values(raw: str) -> list:
    """Значения одного VALUES(...) — запятые внутри строк и скобок не считаются."""
    parts, buf, depth, in_str = [], [], 0, False
    i = 0
    while i < len(raw):
        ch = raw[i]
        if in_str:
            buf.append(ch)
            if ch == "'":
                if i + 1 < len(raw) and raw[i + 1] == "'":
                    buf.append("'")
                    i += 1
                else:
                    in_str = False
        elif ch == "'":
            in_str = True
            buf.append(ch)
        elif ch in '([':
            depth += 1
            buf.append(ch)
        elif ch in ')]':
            depth -= 1
            buf.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append(''.join(buf).strip())
    return parts


def _value(token: str):
    """Литерал SQL в значение Python."""
    text = token.strip()
    upper = text.upper()
    if upper == 'NULL':
        return None
    if upper == 'TRUE':
        return True
    if upper == 'FALSE':
        return False
    if text.startswith("'"):
        end = text.rfind("'")
        body = text[1:end].replace("''", "'")
        if text[end + 1:].strip().lower() in ('::jsonb', '::json'):
            try:
                return json.loads(body)
            except ValueError:
                return body
        return body
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def parse(text: str) -> dict:
    """Строки из дампа по таблицам. Всё, что не INSERT в известную таблицу,
    молча пропускается — схема и DELETE нас не интересуют."""
    rows = {name: [] for name in TABLES}
    skipped = 0
    for stmt in _statements(text):
        match = _INSERT_RE.match(stmt)
        if match is None:
            head = stmt.split(None, 1)[0].upper() if stmt.split() else ''
            if head not in ('BEGIN', 'COMMIT', 'CREATE', 'ALTER', 'DELETE',
                            'SET', 'TRUNCATE', 'START', 'ROLLBACK', ''):
                skipped += 1
            continue
        table, cols_raw, vals_raw = match.groups()
        table = table.lower()
        if table not in rows:
            skipped += 1
            continue
        cols = [c.strip().strip('"') for c in cols_raw.split(',')]
        values = [_value(v) for v in _split_values(vals_raw)]
        if len(cols) != len(values):
            skipped += 1
            continue
        rows[table].append(dict(zip(cols, values)))
    rows['skipped'] = skipped
    return rows


# ─────────────────────────  Загрузка  ─────────────────────────

def restore(rows: dict, replace: bool = False, keep_login: str = '') -> dict:
    """Записать разобранный дамп в текущее хранилище.

    replace — предварительно очистить проверки, отпечатки и журнал входов и
    удалить учётные записи, которых в дампе нет. Учётная запись `keep_login`
    (тот, кто грузит файл) не удаляется никогда, иначе восстановление отрезало
    бы админа от системы, если дамп снят с другого сервера.
    """
    stats = {name: 0 for name in TABLES}
    stats['deleted_users'] = 0
    stats['cleared_jobs'] = 0

    if replace:
        memory_store.clear_store(None)
        for _ in job_store.clear(None):
            stats['cleared_jobs'] += 1
        # Журнал тоже: он не «дописывается» к дампу, а заменяется им — иначе
        # каждое восстановление удваивало записи. Тот же смысл несёт строка
        # DELETE FROM login_events в самом дампе, когда его скармливают psql.
        accounts.clear_events()
        incoming = {r.get('login') for r in rows.get('users', [])}
        for login in list(accounts.load_users()):
            if login not in incoming and login != keep_login:
                accounts.delete_user(login)
                stats['deleted_users'] += 1

    for rec in rows.get('users', []):
        user = {c: rec.get(c) for c in db.USER_COLS}
        if not user.get('login') or not user.get('password_hash'):
            continue
        user['perms'] = user.get('perms') or accounts.default_perms(user.get('role', 'teacher'))
        user['must_change'] = bool(user.get('must_change'))
        user['fail_count'] = int(user.get('fail_count') or 0)
        accounts.save_user(user)
        stats['users'] += 1

    for rec in rows.get('jobs', []):
        job_id, data = rec.get('job_id'), rec.get('data')
        if job_id and isinstance(data, dict):
            job_store.save(job_id, data)
            stats['jobs'] += 1

    store = {}
    for rec in rows.get('fingerprints', []):
        key = rec.get('entry_key')
        if not key:
            continue
        store[key] = {
            'key_base':        rec.get('key_base') or '',
            'version':         int(rec.get('version') or 1),
            'filename':        rec.get('filename') or '',
            'student':         rec.get('student') or {},
            'normalized_text': rec.get('normalized_text') or '',
            'image_data':      rec.get('image_data') or [],
            'pages_count':     int(rec.get('pages_count') or 0),
            'job_id':          rec.get('job_id') or '',
            'added_at':        rec.get('added_at') or '',
            'owner':           rec.get('owner') or '',
        }
    if store:
        memory_store.save_store(store)
        stats['fingerprints'] = len(store)

    events = [{c: rec.get(c) for c in _EVENT_COLS}
              for rec in rows.get('login_events', [])]
    if events:
        accounts.add_events(events)
        stats['login_events'] = len(events)

    for rec in rows.get('settings', []):
        value = rec.get('value')
        if rec.get('key') == 'app' and isinstance(value, dict):
            accounts.save_settings(value)
            stats['settings'] += 1

    return stats
