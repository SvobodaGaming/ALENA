"""PostgreSQL persistence for fingerprints and job history.

Active only when DATABASE_URL is set; otherwise the app falls back to the JSON
file store (see checker/memory_store.py). Uses psycopg 3 with a small
connection pool and plain SQL, no ORM. psycopg is imported lazily so the
fallback path works without the package installed.
"""
import os
import threading

DATABASE_URL = os.environ.get('DATABASE_URL', '')
DB_ENABLED = bool(DATABASE_URL)

_pool = None
_pool_lock = threading.Lock()

# Схема таблиц. Отсюда же её берёт выгрузка дампа (checker/sqlmigrate.py),
# поэтому список публичный.
SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS fingerprints (
        entry_key       TEXT PRIMARY KEY,
        key_base        TEXT NOT NULL,
        version         INTEGER NOT NULL,
        filename        TEXT,
        student         JSONB,
        normalized_text TEXT,
        text_hash       TEXT,
        image_data      JSONB,
        pages_count     INTEGER,
        job_id          TEXT,
        added_at        TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS fingerprints_key_base_idx ON fingerprints (key_base)",
    "ALTER TABLE fingerprints ADD COLUMN IF NOT EXISTS text_hash TEXT",
    # Fingerprints and jobs belong to the teacher who ran the check: every
    # teacher has an isolated base and an isolated history.
    "ALTER TABLE fingerprints ADD COLUMN IF NOT EXISTS owner TEXT",
    "CREATE INDEX IF NOT EXISTS fingerprints_owner_idx ON fingerprints (owner)",
    """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id     TEXT PRIMARY KEY,
        data       JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        login         TEXT PRIMARY KEY,
        fio           TEXT NOT NULL,
        email         TEXT,
        role          TEXT NOT NULL,
        state         TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        perms         JSONB NOT NULL DEFAULT '{}'::jsonb,
        api_key       TEXT,
        must_change   BOOLEAN NOT NULL DEFAULT false,
        created_at    TEXT,
        last_login    TEXT,
        fail_count    INTEGER NOT NULL DEFAULT 0,
        locked_until  TEXT
    )
    """,
    # Дата последней смены пароля – по ней считается срок его действия
    # (настройка pw_expire_days). У записей, заведённых раньше, пусто:
    # отсчёт для них идёт от даты создания.
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS pw_changed_at TEXT",
    """
    CREATE TABLE IF NOT EXISTS login_events (
        id     BIGSERIAL PRIMARY KEY,
        ts     TEXT NOT NULL,
        login  TEXT,
        ok     BOOLEAN NOT NULL,
        ip     TEXT,
        ua     TEXT,
        reason TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS login_events_id_idx ON login_events (id DESC)",
    """
    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value JSONB NOT NULL
    )
    """,
    # Группы преподавателей: состав лежит в самой группе, а не в учётной
    # записи, поэтому преподаватель состоит в скольких угодно группах и
    # таблица users не меняется (см. checker/teams.py).
    """
    CREATE TABLE IF NOT EXISTS teams (
        team_id    TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        members    JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_at TEXT
    )
    """,
]


def _get_pool():
    """Open the pool and create the schema on first use (thread-safe)."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                from psycopg_pool import ConnectionPool
                pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=8,
                                      open=False, kwargs={'autocommit': False})
                pool.open()
                with pool.connection() as conn, conn.cursor() as cur:
                    for stmt in SCHEMA:
                        cur.execute(stmt)
                _pool = pool
    return _pool


def _conn():
    return _get_pool().connection()


# Fingerprints

def fp_load_all(owners=None) -> dict:
    """All fingerprints, or only those belonging to the given owners.

    `owners` – список логинов: у преподавателя в группе видимая база это его
    собственные отпечатки плюс отпечатки коллег по группам, а не один владелец.
    """
    store = {}
    sql = ("SELECT entry_key, key_base, version, filename, student, "
           "normalized_text, text_hash, image_data, pages_count, job_id, added_at, owner "
           "FROM fingerprints")
    params: tuple = ()
    if owners is not None:
        if not owners:
            return store
        sql += " WHERE owner = ANY(%s)"
        params = (list(owners),)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        for (entry_key, key_base, version, filename, student, normalized_text,
             text_hash, image_data, pages_count, job_id, added_at,
             row_owner) in cur.fetchall():
            store[entry_key] = {
                'key_base':        key_base,
                'version':         version,
                'filename':        filename or '',
                'student':         student or {},
                'normalized_text': normalized_text or '',
                'text_hash':       text_hash or '',
                'image_data':      image_data or [],
                'pages_count':     pages_count or 0,
                'job_id':          job_id or '',
                'added_at':        added_at or '',
                'owner':           row_owner or '',
            }
    return store


def fp_upsert_many(store: dict) -> None:
    if not store:
        return
    from psycopg.types.json import Json
    with _conn() as conn, conn.cursor() as cur:
        for entry_key, v in store.items():
            cur.execute(
                """
                INSERT INTO fingerprints
                  (entry_key, key_base, version, filename, student,
                   normalized_text, text_hash, image_data, pages_count, job_id,
                   added_at, owner)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (entry_key) DO UPDATE SET
                  key_base        = EXCLUDED.key_base,
                  version         = EXCLUDED.version,
                  filename        = EXCLUDED.filename,
                  student         = EXCLUDED.student,
                  normalized_text = EXCLUDED.normalized_text,
                  text_hash       = EXCLUDED.text_hash,
                  image_data      = EXCLUDED.image_data,
                  pages_count     = EXCLUDED.pages_count,
                  job_id          = EXCLUDED.job_id,
                  added_at        = EXCLUDED.added_at,
                  owner           = EXCLUDED.owner
                """,
                (entry_key, v.get('key_base', ''), v.get('version', 1),
                 v.get('filename', ''), Json(v.get('student', {})),
                  v.get('normalized_text', ''), v.get('text_hash', ''),
                  Json(v.get('image_data', [])),
                 v.get('pages_count', 0), v.get('job_id', ''),
                 v.get('added_at', ''), v.get('owner', '')),
            )


def fp_insert_versioned(key_base: str, entry: dict) -> int:
    """Вставить отпечаток следующей свободной версией. Возвращает её номер.

    Номер и вставка идут одной транзакцией, а столкновение по первичному ключу
    гасится повтором: два рабочих процесса, сохраняющих отпечаток одного
    студента одновременно, получают разные версии, а не затирают друг друга.
    """
    from psycopg.errors import UniqueViolation
    from psycopg.types.json import Json

    for _ in range(8):
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM fingerprints "
                    "WHERE key_base = %s", (key_base,))
                version = cur.fetchone()[0]
                cur.execute(
                    """
                    INSERT INTO fingerprints
                      (entry_key, key_base, version, filename, student,
                       normalized_text, text_hash, image_data, pages_count,
                       job_id, added_at, owner)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (f'{key_base}|v{version}', key_base, version,
                     entry.get('filename', ''), Json(entry.get('student', {})),
                      entry.get('normalized_text', ''),
                      entry.get('text_hash', ''),
                      Json(entry.get('image_data', [])),
                     entry.get('pages_count', 0), entry.get('job_id', ''),
                     entry.get('added_at', ''), entry.get('owner', '')),
                )
            return version
        except UniqueViolation:
            continue    # номер занял сосед – берём следующий
    raise RuntimeError('не удалось подобрать свободный номер версии отпечатка')


def fp_clear(owner=None) -> int:
    """Wipe the fingerprint base – all of it, or one owner's slice."""
    with _conn() as conn, conn.cursor() as cur:
        if owner is None:
            cur.execute("SELECT count(*) FROM fingerprints")
            n = cur.fetchone()[0]
            cur.execute("DELETE FROM fingerprints")
        else:
            cur.execute("SELECT count(*) FROM fingerprints WHERE owner = %s", (owner,))
            n = cur.fetchone()[0]
            cur.execute("DELETE FROM fingerprints WHERE owner = %s", (owner,))
    return n


def fp_delete(entry_key: str) -> bool:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM fingerprints WHERE entry_key = %s", (entry_key,))
        return cur.rowcount > 0


# Job history

def jobs_save(job_id: str, data: dict) -> None:
    from psycopg.types.json import Json
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs (job_id, data) VALUES (%s, %s)
            ON CONFLICT (job_id) DO UPDATE SET data = EXCLUDED.data
            """,
            (job_id, Json(data)),
        )


def jobs_load_all(owner=None) -> dict:
    """Job history – all of it, or only the jobs started by `owner`."""
    out = {}
    sql = "SELECT job_id, data FROM jobs"
    params = ()
    if owner is not None:
        sql += " WHERE data->>'owner' = %s"
        params = (owner,)
    sql += " ORDER BY created_at"
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        for job_id, data in cur.fetchall():
            out[job_id] = data
    return out


def jobs_count(owner=None) -> int:
    """How many checks are in the history – without fetching them."""
    sql = "SELECT count(*) FROM jobs"
    params = ()
    if owner is not None:
        sql += " WHERE data->>'owner' = %s"
        params = (owner,)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def jobs_get(job_id: str):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT data FROM jobs WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
        return row[0] if row else None


def jobs_clear(owner=None) -> None:
    with _conn() as conn, conn.cursor() as cur:
        if owner is None:
            cur.execute("DELETE FROM jobs")
        else:
            cur.execute("DELETE FROM jobs WHERE data->>'owner' = %s", (owner,))


def jobs_delete(job_id: str) -> bool:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM jobs WHERE job_id = %s", (job_id,))
        return cur.rowcount > 0


# Accounts, login journal and system settings

USER_COLS = ('login', 'fio', 'email', 'role', 'state', 'password_hash',
              'perms', 'api_key', 'must_change', 'created_at', 'pw_changed_at',
              'last_login', 'fail_count', 'locked_until')


def users_load_all() -> dict:
    out = {}
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(USER_COLS)} FROM users")
        for row in cur.fetchall():
            rec = dict(zip(USER_COLS, row))
            rec['perms'] = rec['perms'] or {}
            out[rec['login']] = rec
    return out


def users_save(user: dict) -> None:
    from psycopg.types.json import Json
    assign = ', '.join(f'{c} = EXCLUDED.{c}' for c in USER_COLS if c != 'login')
    values = []
    for c in USER_COLS:
        v = user.get(c)
        values.append(Json(v or {}) if c == 'perms' else v)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO users ({', '.join(USER_COLS)}) "
            f"VALUES ({', '.join(['%s'] * len(USER_COLS))}) "
            f"ON CONFLICT (login) DO UPDATE SET {assign}",
            values,
        )


def users_delete(login: str) -> bool:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE login = %s", (login,))
        return cur.rowcount > 0


TEAM_COLS = ('team_id', 'name', 'members', 'created_at')


def teams_load_all() -> dict:
    out = {}
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(TEAM_COLS)} FROM teams")
        for row in cur.fetchall():
            rec = dict(zip(TEAM_COLS, row))
            rec['members'] = rec['members'] or []
            out[rec['team_id']] = rec
    return out


def teams_save(team: dict) -> None:
    from psycopg.types.json import Json
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO teams (team_id, name, members, created_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (team_id) DO UPDATE SET
              name       = EXCLUDED.name,
              members    = EXCLUDED.members,
              created_at = EXCLUDED.created_at
            """,
            (team.get('team_id'), team.get('name', ''),
             Json(team.get('members') or []), team.get('created_at', '')),
        )


def teams_delete(team_id: str) -> bool:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM teams WHERE team_id = %s", (team_id,))
        return cur.rowcount > 0


def log_add(event: dict) -> None:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO login_events (ts, login, ok, ip, ua, reason) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (event.get('ts', ''), event.get('login', ''), bool(event.get('ok')),
             event.get('ip', ''), event.get('ua', ''), event.get('reason', '')),
        )


def log_clear() -> None:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM login_events")


def log_recent(limit: int = 200, login=None) -> list:
    sql = "SELECT ts, login, ok, ip, ua, reason FROM login_events"
    params: tuple = ()
    if login is not None:
        sql += " WHERE login = %s"
        params = (login,)
    sql += " ORDER BY id DESC LIMIT %s"
    params += (limit,)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return [{'ts': ts, 'login': lg, 'ok': ok, 'ip': ip, 'ua': ua,
                 'reason': reason or ''}
                for ts, lg, ok, ip, ua, reason in cur.fetchall()]


def settings_load() -> dict:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key = 'app'")
        row = cur.fetchone()
        return row[0] if row else {}


def settings_save(value: dict) -> None:
    from psycopg.types.json import Json
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO settings (key, value) VALUES ('app', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (Json(value),),
        )
