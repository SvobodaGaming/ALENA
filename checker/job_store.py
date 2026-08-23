"""Persistent check history.

Wraps the PostgreSQL `jobs` table and falls back to JSON files, so history –
and with it the record of who owns which check – survives a restart in both
deployments. Ownership matters: a report is served only to its owner (or to an
account allowed to see everything), and that decision needs the record.
"""

import re
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from checker import db, jsonstore

try:
    import fcntl
except ImportError:  # Windows local development; production image is Linux.
    fcntl = None

STORE_PATH = Path(__file__).parent.parent / 'memory' / 'jobs.json'
STORE_DIR = Path(__file__).parent.parent / 'memory' / 'jobs'
_lock = threading.Lock()
_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,128}$')

_STAMP = '%d.%m.%Y %H:%M'
_STAMP_RE = re.compile(r'^\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}$')


def parse_created_at(value):
    """Parse a persisted job timestamp, or return None for legacy bad data."""
    if not isinstance(value, str) or not _STAMP_RE.fullmatch(value):
        return None
    try:
        return datetime.strptime(value, _STAMP)
    except ValueError:
        return None


def _job_path(job_id: str) -> Path:
    job_id = str(job_id or '')
    if not _ID_RE.fullmatch(job_id):
        raise ValueError('invalid job id')
    return STORE_DIR / f'{job_id}.json'


@contextmanager
def _process_lock():
    """Serialize JSON migration and writes across server processes."""
    path = STORE_DIR.parent / 'jobs.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _migrate_legacy_unlocked() -> None:
    """Move the old monolithic jobs.json to one atomic file per job."""
    if not STORE_PATH.exists():
        return
    legacy = jsonstore.read_json(STORE_PATH, None)
    if not isinstance(legacy, dict):
        return

    remaining = {}
    for job_id, data in legacy.items():
        try:
            path = _job_path(job_id)
        except ValueError:
            remaining[job_id] = data
            continue
        existing = jsonstore.read_json(path, None)
        if not isinstance(existing, dict):
            jsonstore.write_json(path, data)

    if remaining:
        jsonstore.write_json(STORE_PATH, remaining)
    else:
        STORE_PATH.unlink(missing_ok=True)


def _read_all_unlocked() -> dict:
    _migrate_legacy_unlocked()
    store = jsonstore.read_json(STORE_PATH, {})
    if not isinstance(store, dict):
        store = {}
    if STORE_DIR.exists():
        for path in STORE_DIR.glob('*.json'):
            data = jsonstore.read_json(path, None)
            if isinstance(data, dict):
                store[path.stem] = data
    return store


def save(job_id: str, data: dict) -> None:
    if db.DB_ENABLED:
        db.jobs_save(job_id, data)
        return
    path = _job_path(job_id)
    with _lock, _process_lock():
        _migrate_legacy_unlocked()
        jsonstore.write_json(path, data)


def load_all(owner=None) -> dict:
    if db.DB_ENABLED:
        return db.jobs_load_all(owner)
    with _lock, _process_lock():
        store = _read_all_unlocked()
    if owner is None:
        return store
    return {k: v for k, v in store.items() if v.get('owner', '') == owner}


def get(job_id: str):
    if db.DB_ENABLED:
        return db.jobs_get(job_id)
    try:
        path = _job_path(job_id)
    except ValueError:
        return None
    with _lock, _process_lock():
        _migrate_legacy_unlocked()
        data = jsonstore.read_json(path, None)
    return data if isinstance(data, dict) else None


def delete(job_id: str) -> bool:
    if db.DB_ENABLED:
        return db.jobs_delete(job_id)
    try:
        path = _job_path(job_id)
    except ValueError:
        return False
    with _lock, _process_lock():
        _migrate_legacy_unlocked()
        if not path.exists():
            return False
        path.unlink()
        return True


def clear(owner=None) -> list:
    """Delete history. Returns the ids removed, so callers can drop the
    matching report files."""
    if db.DB_ENABLED:
        ids = list(load_all(owner).keys())
        db.jobs_clear(owner)
        return ids
    with _lock, _process_lock():
        store = _read_all_unlocked()
        ids = [jid for jid, data in store.items()
               if owner is None or data.get('owner', '') == owner]
        for job_id in ids:
            try:
                _job_path(job_id).unlink(missing_ok=True)
            except ValueError:
                pass
        legacy = jsonstore.read_json(STORE_PATH, {})
        if isinstance(legacy, dict):
            for job_id in ids:
                legacy.pop(job_id, None)
            if legacy:
                jsonstore.write_json(STORE_PATH, legacy)
            else:
                STORE_PATH.unlink(missing_ok=True)
    return ids


def expired(days: int) -> list:
    """Ids of checks older than `days`. `days <= 0` means keep forever."""
    if days <= 0:
        return []
    now = datetime.now()
    stale = []
    for jid, data in load_all().items():
        created = parse_created_at(data.get('created_at'))
        if created is None:
            continue
        age = now - created
        if age.days > days:
            stale.append(jid)
    return stale
