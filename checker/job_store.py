"""Persistent check history.

Wraps the PostgreSQL `jobs` table and falls back to a JSON file, so history –
and with it the record of who owns which check – survives a restart in both
deployments. Ownership matters: a report is served only to its owner (or to an
account allowed to see everything), and that decision needs the record.
"""

import threading
from datetime import datetime
from pathlib import Path

from checker import db, jsonstore

STORE_PATH = Path(__file__).parent.parent / 'memory' / 'jobs.json'
_lock = threading.Lock()

_STAMP = '%d.%m.%Y %H:%M'


def _read_all() -> dict:
    return jsonstore.read_json(STORE_PATH, {})


def _write_all(data: dict) -> None:
    jsonstore.write_json(STORE_PATH, data)


def save(job_id: str, data: dict) -> None:
    if db.DB_ENABLED:
        db.jobs_save(job_id, data)
        return
    with _lock:
        store = _read_all()
        store[job_id] = data
        _write_all(store)


def load_all(owner=None) -> dict:
    if db.DB_ENABLED:
        return db.jobs_load_all(owner)
    store = _read_all()
    if owner is None:
        return store
    return {k: v for k, v in store.items() if v.get('owner', '') == owner}


def get(job_id: str):
    if db.DB_ENABLED:
        return db.jobs_get(job_id)
    return _read_all().get(job_id)


def delete(job_id: str) -> bool:
    if db.DB_ENABLED:
        return db.jobs_delete(job_id)
    with _lock:
        store = _read_all()
        if job_id not in store:
            return False
        del store[job_id]
        _write_all(store)
    return True


def clear(owner=None) -> list:
    """Delete history. Returns the ids removed, so callers can drop the
    matching report files."""
    ids = list(load_all(owner).keys())
    if db.DB_ENABLED:
        db.jobs_clear(owner)
        return ids
    with _lock:
        store = _read_all()
        for jid in ids:
            store.pop(jid, None)
        _write_all(store)
    return ids


def expired(days: int) -> list:
    """Ids of checks older than `days`. `days <= 0` means keep forever."""
    if days <= 0:
        return []
    now = datetime.now()
    stale = []
    for jid, data in load_all().items():
        try:
            age = now - datetime.strptime(data.get('created_at', ''), _STAMP)
        except ValueError:
            continue
        if age.days > days:
            stale.append(jid)
    return stale
