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
import threading
from datetime import datetime
from pathlib import Path

from checker import db, jsonstore

STORE_PATH = Path(__file__).parent.parent / 'memory' / 'store.json'
_lock = threading.Lock()


def _read_all() -> dict:
    return jsonstore.read_json(STORE_PATH, {})


def load_store(owner=None) -> dict:
    """Fingerprints visible to `owner`, or the whole base when owner is None.

    Each teacher has an isolated base: their reports are compared only against
    their own previous ones.
    """
    if db.DB_ENABLED:
        return db.fp_load_all(owner)
    store = _read_all()
    if owner is None:
        return store
    return {k: v for k, v in store.items() if v.get('owner', '') == owner}


def save_store(store: dict) -> None:
    """Persist the given entries. Entries outside `store` are left untouched,
    so saving one teacher's slice never disturbs another's."""
    if db.DB_ENABLED:
        db.fp_upsert_many(store)
        return
    with _lock:
        full = _read_all()
        full.update(store)
        jsonstore.write_json(STORE_PATH, full)


def clear_store(owner=None) -> int:
    """Delete the whole base, or just one owner's entries. Returns the count."""
    if db.DB_ENABLED:
        return db.fp_clear(owner)
    with _lock:
        store = _read_all()
        if owner is None:
            count = len(store)
            STORE_PATH.unlink(missing_ok=True)
            return count
        doomed = [k for k, v in store.items() if v.get('owner', '') == owner]
        for k in doomed:
            del store[k]
        jsonstore.write_json(STORE_PATH, store)
    return len(doomed)


def delete_entry(entry_key: str) -> bool:
    """Remove a single entry by key. Thread-safe. Returns True if it existed."""
    if db.DB_ENABLED:
        return db.fp_delete(entry_key)
    with _lock:
        store = _read_all()
        if entry_key not in store:
            return False
        del store[entry_key]
        jsonstore.write_json(STORE_PATH, store)
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
            'owner':       v.get('owner', ''),
        })
    def _parse_dt(s):
        try:
            return datetime.strptime(s, '%d.%m.%Y %H:%M')
        except Exception:
            return datetime.min

    result.sort(key=lambda x: _parse_dt(x['added_at']), reverse=True)
    return result


def _student_key(report: dict, owner: str = '') -> str:
    """Owner-scoped identity of a student's work. The owner prefix keeps two
    teachers' bases apart even when they have a namesake in the same group."""
    s = report.get('student', {})
    name  = s.get('name',  '').strip().lower()
    group = s.get('group', '').strip().lower()
    return f'{owner}|{name}|{group}'


def _make_thumb(pil_img) -> str:
    thumb = pil_img.copy()
    thumb.thumbnail((120, 96))
    buf = io.BytesIO()
    thumb.save(buf, format='JPEG', quality=55)
    return 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()


def _shrink_thumb(data_uri: str) -> str:
    """Re-encode the report-sized thumbnail (200×160) down to the compact
    store size (120×96) so the persistent store does not grow."""
    if not data_uri:
        return ''
    try:
        from PIL import Image
        raw = base64.b64decode(data_uri.split(',', 1)[1])
        return _make_thumb(Image.open(io.BytesIO(raw)))
    except Exception:
        return data_uri


def _entry_for(report: dict, job_id: str, owner: str) -> tuple:
    """(key_base, запись без номера версии) для одной проверенной работы."""
    from checker.text_plagiarism import normalize_text

    image_data = []
    for img_info in report.get('images', []):
        hashes = img_info.get('hashes')
        if not hashes:
            continue
        image_data.append({
            'page':   img_info.get('page', 0),
            'hashes': [str(h) for h in hashes],
            'thumb':  _shrink_thumb(img_info.get('thumb', '')),
            'is_ui':  img_info.get('is_ui', False),
        })

    key_base = _student_key(report, owner)
    return key_base, {
        'key_base':        key_base,
        'filename':        report.get('filename', ''),
        'student':         report.get('student', {}),
        'normalized_text': normalize_text(report.get('full_text', '')),
        'image_data':      image_data,
        'pages_count':     report.get('pages_count', 0),
        'added_at':        datetime.now().strftime('%d.%m.%Y %H:%M'),
        'job_id':          job_id,
        'owner':           owner,
    }


def add_report(report: dict, job_id: str, owner: str = '') -> int:
    """Записать работу в базу отпечатков новой версией (v1, v2, …).

    Номер версии выбирается и запись сохраняется одним неделимым действием.
    Раньше вызывающий читал базу, считал `max(версий)+1` по своему снимку и
    сохранял: две проверки одного преподавателя, идущие рядом, выбирали один и
    тот же номер, и отпечаток первой затирался отпечатком второй. Возвращает
    присвоенный номер версии.
    """
    key_base, entry = _entry_for(report, job_id, owner)

    if db.DB_ENABLED:
        return db.fp_insert_versioned(key_base, entry)

    with _lock:
        store = _read_all()
        version = max((v.get('version', 0) for v in store.values()
                       if v.get('key_base') == key_base), default=0) + 1
        store[f'{key_base}|v{version}'] = dict(entry, version=version)
        jsonstore.write_json(STORE_PATH, store)
    return version


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
