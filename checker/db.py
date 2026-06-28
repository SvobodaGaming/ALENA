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

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS fingerprints (
        entry_key       TEXT PRIMARY KEY,
        key_base        TEXT NOT NULL,
        version         INTEGER NOT NULL,
        filename        TEXT,
        student         JSONB,
        normalized_text TEXT,
        image_data      JSONB,
        pages_count     INTEGER,
        job_id          TEXT,
        added_at        TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS fingerprints_key_base_idx ON fingerprints (key_base)",
    """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id     TEXT PRIMARY KEY,
        data       JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
                    for stmt in _SCHEMA:
                        cur.execute(stmt)
                _pool = pool
    return _pool


def _conn():
    return _get_pool().connection()


# Fingerprints

def fp_load_all() -> dict:
    store = {}
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT entry_key, key_base, version, filename, student, "
            "normalized_text, image_data, pages_count, job_id, added_at "
            "FROM fingerprints"
        )
        for (entry_key, key_base, version, filename, student, normalized_text,
             image_data, pages_count, job_id, added_at) in cur.fetchall():
            store[entry_key] = {
                'key_base':        key_base,
                'version':         version,
                'filename':        filename or '',
                'student':         student or {},
                'normalized_text': normalized_text or '',
                'image_data':      image_data or [],
                'pages_count':     pages_count or 0,
                'job_id':          job_id or '',
                'added_at':        added_at or '',
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
                   normalized_text, image_data, pages_count, job_id, added_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (entry_key) DO UPDATE SET
                  key_base        = EXCLUDED.key_base,
                  version         = EXCLUDED.version,
                  filename        = EXCLUDED.filename,
                  student         = EXCLUDED.student,
                  normalized_text = EXCLUDED.normalized_text,
                  image_data      = EXCLUDED.image_data,
                  pages_count     = EXCLUDED.pages_count,
                  job_id          = EXCLUDED.job_id,
                  added_at        = EXCLUDED.added_at
                """,
                (entry_key, v.get('key_base', ''), v.get('version', 1),
                 v.get('filename', ''), Json(v.get('student', {})),
                 v.get('normalized_text', ''), Json(v.get('image_data', [])),
                 v.get('pages_count', 0), v.get('job_id', ''),
                 v.get('added_at', '')),
            )


def fp_clear() -> int:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM fingerprints")
        n = cur.fetchone()[0]
        cur.execute("DELETE FROM fingerprints")
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


def jobs_load_all() -> dict:
    out = {}
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT job_id, data FROM jobs ORDER BY created_at")
        for job_id, data in cur.fetchall():
            out[job_id] = data
    return out


def jobs_get(job_id: str):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT data FROM jobs WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
        return row[0] if row else None


def jobs_clear() -> None:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM jobs")
