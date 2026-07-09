"""
Persistent fingerprint store for cross-session plagiarism detection.

Backed by PostgreSQL when DATABASE_URL is set, otherwise by a JSON file:
  { "<name>|<group>|v<N>": { ...entry... }, ... }

Each entry stores: student info, normalized text, image hashes + thumbnails.
PIL images are NOT stored, only compact hashes (144 bits × 3 crops) and a
small JPEG thumbnail per image.
"""

import base64
import io
import json
import threading
from datetime import datetime
from pathlib import Path

from checker import db

STORE_PATH = Path(__file__).parent.parent / 'memory' / 'store.json'
_lock = threading.Lock()


def load_store() -> dict:
    if db.DB_ENABLED:
        return db.fp_load_all()
    STORE_PATH.parent.mkdir(exist_ok=True)
    if not STORE_PATH.exists():
        return {}
    try:
        return json.loads(STORE_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_store(store: dict) -> None:
    if db.DB_ENABLED:
        db.fp_upsert_many(store)
        return
    STORE_PATH.parent.mkdir(exist_ok=True)
    with _lock:
        STORE_PATH.write_text(
            json.dumps(store, ensure_ascii=False, indent=2), encoding='utf-8'
        )


def clear_store() -> int:
    """Delete entire store. Returns number of deleted entries."""
    if db.DB_ENABLED:
        return db.fp_clear()
    with _lock:
        if not STORE_PATH.exists():
            return 0
        try:
            count = len(json.loads(STORE_PATH.read_text(encoding='utf-8')))
        except Exception:
            count = 0
        STORE_PATH.unlink()
    return count


def delete_entry(entry_key: str) -> bool:
    """Remove a single entry by key. Thread-safe. Returns True if it existed."""
    if db.DB_ENABLED:
        return db.fp_delete(entry_key)
    with _lock:
        try:
            store = json.loads(STORE_PATH.read_text(encoding='utf-8'))
        except Exception:
            return False
        if entry_key not in store:
            return False
        del store[entry_key]
        STORE_PATH.write_text(
            json.dumps(store, ensure_ascii=False, indent=2), encoding='utf-8'
        )
    return True


def get_summary(store: dict) -> list:
    """Lightweight list for the UI, no text or hashes included."""
    result = []
    for key, v in store.items():
        result.append({
            'key':         key,
            'version':     v.get('version', 1),
            'filename':    v.get('filename', ''),
            'student':     v.get('student', {}),
            'pages_count': v.get('pages_count', 0),
            'image_count': len(v.get('image_data', [])),
            'added_at':    v.get('added_at', ''),
            'job_id':      v.get('job_id', ''),
        })
    def _parse_dt(s):
        try:
            return datetime.strptime(s, '%d.%m.%Y %H:%M')
        except Exception:
            return datetime.min

    result.sort(key=lambda x: _parse_dt(x['added_at']), reverse=True)
    return result


def _student_key(report: dict) -> str:
    s = report.get('student', {})
    name  = s.get('name',  '').strip().lower()
    group = s.get('group', '').strip().lower()
    return f'{name}|{group}'


def _make_thumb(pil_img) -> str:
    thumb = pil_img.copy()
    thumb.thumbnail((120, 96))
    buf = io.BytesIO()
    thumb.save(buf, format='JPEG', quality=55)
    return 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()


def upsert_report(store: dict, report: dict, job_id: str) -> None:
    """
    Add report to store as a new version (v1, v2, …).
    Mutates `store` in-place; caller must call save_store() afterwards.
    """
    from checker.text_plagiarism import normalize_text
    from checker.image_plagiarism import _compute_hashes, _is_ui_like

    key_base = _student_key(report)

    existing_versions = [
        v['version'] for v in store.values()
        if v.get('key_base') == key_base
    ]
    next_version = max(existing_versions, default=0) + 1

    image_data = []
    for img_info in report.get('images', []):
        pil = img_info.get('pil')
        if pil is None:
            continue
        try:
            hashes = _compute_hashes(pil)
            image_data.append({
                'page':   img_info.get('page', 0),
                'hashes': [str(h) for h in hashes],
                'thumb':  _make_thumb(pil),
                'is_ui':  _is_ui_like(pil),
            })
        except Exception:
            pass

    store[f'{key_base}|v{next_version}'] = {
        'key_base':        key_base,
        'version':         next_version,
        'filename':        report.get('filename', ''),
        'student':         report.get('student', {}),
        'normalized_text': normalize_text(report.get('full_text', '')),
        'image_data':      image_data,
        'pages_count':     report.get('pages_count', 0),
        'added_at':        datetime.now().strftime('%d.%m.%Y %H:%M'),
        'job_id':          job_id,
    }


def to_virtual_report(entry_key: str, entry: dict) -> dict:
    """
    Convert a store entry into a report dict compatible with all checker modules.
    The 'path' is a virtual memory:// URI used as a stable unique key.
    """
    import imagehash as ih

    precomputed_images = []
    for item in entry.get('image_data', []):
        try:
            hashes = [ih.hex_to_hash(h) for h in item['hashes']]
            precomputed_images.append({
                'page':   item.get('page', 0),
                'hashes': hashes,
                'thumb':  item.get('thumb'),
                'is_ui':  item.get('is_ui', False),
            })
        except Exception:
            pass

    return {
        'path':               f'memory://{entry_key}',
        'filename':           entry.get('filename', ''),
        'student':            entry.get('student', {}),
        'full_text':          '',
        'normalized_text':    entry.get('normalized_text', ''),
        'images':             [],
        'precomputed_images': precomputed_images,
        'is_historical':      True,
        'historical_date':    entry.get('added_at', ''),
        'historical_version': entry.get('version', 1),
        'gost_results':       [],
        'pages_count':        entry.get('pages_count', 0),
        'font_info':          {},
        'margin_info':        {},
        'is_scanned':         False,
        'error':              None,
    }
