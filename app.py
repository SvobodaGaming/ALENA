"""
Flask web application for autonomous student report checking.

Run locally:
    python app.py

Run with Docker:
    docker compose up --build

Run on server (production):
    gunicorn -w 2 -b 0.0.0.0:5000 app:app
"""

import io
import os
import hmac
import uuid
import zipfile
import tempfile
import shutil
import threading
from functools import wraps
from pathlib import Path
from datetime import datetime

from flask import (Flask, Blueprint, request, jsonify, render_template,
                   send_file, abort, send_from_directory, session,
                   redirect, url_for)
from werkzeug.exceptions import HTTPException

try:
    from weasyprint import HTML as WeasyHTML
    WEASYPRINT_OK = True
except Exception:
    WEASYPRINT_OK = False

from checker import db
from checker.extractor        import extract_report
from checker.gost             import check_gost, GOST_CHECKS, ALL_CODES
from checker.text_plagiarism  import check_text_plagiarism
from checker.image_plagiarism import check_image_plagiarism
from checker.reporter         import generate_html_report


app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 600 * 1024 * 1024
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-in-production')

REPORTS_DIR = Path(__file__).parent / 'reports'
REPORTS_DIR.mkdir(exist_ok=True)

jobs: dict = {}
jobs_lock = threading.Lock()


def _recover_stale_jobs():
    """Processing threads do not survive a restart. Any job still marked
    'processing' in the DB at boot is orphaned — flag it as an error so the
    UI does not show it as running forever (and so it can be deleted)."""
    if not db.DB_ENABLED:
        return
    try:
        for jid, data in db.jobs_load_all().items():
            if data.get('status') == 'processing':
                data.update(
                    status='error',
                    step='Прервано перезапуском сервера — запустите проверку заново.',
                    error='stale job recovered at startup',
                )
                db.jobs_save(jid, data)
    except Exception:
        pass  # DB not reachable yet; stale rows will simply stay until next boot


_recover_stale_jobs()

_AU_USERNAME = os.environ.get('AU_USERNAME', 'admin')
_AU_PASSWORD = os.environ.get('AU_PASSWORD', 'admin')
# Optional machine-to-machine key for the /api/v1 layer. When empty, the API
# accepts only an authenticated browser session (no token access).
_AU_API_KEY = os.environ.get('AU_API_KEY', '')


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated


@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('authenticated'):
        return redirect(url_for('index'))
    error = None
    username = ''
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if username == _AU_USERNAME and password == _AU_PASSWORD:
            session['authenticated'] = True
            next_url = request.form.get('next') or request.args.get('next') or url_for('index')
            return redirect(next_url)
        error = 'Неверные имя пользователя или пароль'
    return render_template('login.html', error=error, username=username,
                           next=request.args.get('next', ''))


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))


def _api_authorized() -> bool:
    """Accept either an authenticated session or a matching X-API-Key header."""
    if session.get('authenticated'):
        return True
    if _AU_API_KEY:
        provided = request.headers.get('X-API-Key', '')
        if provided and hmac.compare_digest(provided, _AU_API_KEY):
            return True
    return False


def api_auth_required(f):
    """Like login_required, but returns JSON 401 instead of redirecting."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _api_authorized():
            return jsonify({
                'error': 'Не авторизовано: передайте заголовок X-API-Key '
                         'или войдите в сессию.'
            }), 401
        return f(*args, **kwargs)
    return decorated


@app.route('/health')
def health():
    return jsonify({'ok': True})


@app.route('/au_logo.png')
def logo():
    return send_from_directory('.', 'au_logo.png', mimetype='image/png')


@app.route('/')
@login_required
def index():
    return render_template('index.html', username=_AU_USERNAME,
                           gost_checks=GOST_CHECKS)


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


# Shared job helpers, used by both the UI routes and the /api/v1 layer

def _public_job(job: dict) -> dict:
    """Job state without the heavy inline report HTML."""
    return {k: v for k, v in job.items() if k != 'report_html'}


def _start_job(uploaded, threshold: float, enabled_checks=None, use_memory=True):
    """Validate uploaded files and spawn the processing thread.

    enabled_checks: list of GOST check codes to evaluate, or None for all.
    use_memory: when False, skip comparison against stored fingerprints
    (new reports are still added to the base).

    Returns (job_id, error_message, http_status). On success error_message is
    None; on failure job_id is None.
    """
    if not uploaded or all(f.filename == '' for f in uploaded):
        return None, 'Файлы не выбраны', 400

    job_id = uuid.uuid4().hex[:10]
    tmp_dir = tempfile.mkdtemp(prefix=f'rc_{job_id}_')

    pdf_paths = []
    try:
        for f in uploaded:
            name = Path(f.filename).name
            dest = os.path.join(tmp_dir, name)
            f.save(dest)
            if name.lower().endswith('.zip'):
                with zipfile.ZipFile(dest, 'r') as z:
                    z.extractall(tmp_dir)
                os.remove(dest)

        for pdf in sorted(Path(tmp_dir).rglob('*.pdf')):
            pdf_paths.append(str(pdf))
        for pdf in sorted(Path(tmp_dir).rglob('*.PDF')):
            pdf_paths.append(str(pdf))

        seen: set = set()
        pdf_paths = [p for p in pdf_paths if not (p in seen or seen.add(p))]

    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, f'Ошибка при загрузке: {e}', 500

    if not pdf_paths:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, 'PDF-файлы не найдены в загруженных данных', 400

    with jobs_lock:
        jobs[job_id] = {
            'status':     'processing',
            'progress':   0,
            'step':       'Старт...',
            'total':      len(pdf_paths),
            'done_files': 0,
            'text_pairs': 0,
            'img_pairs':  0,
            'created_at': datetime.now().strftime('%d.%m.%Y %H:%M'),
            'report_html': None,
            'error':      None,
        }
        snapshot = _public_job(jobs[job_id])

    if db.DB_ENABLED:
        db.jobs_save(job_id, snapshot)

    threading.Thread(
        target=_process_job,
        args=(job_id, pdf_paths, tmp_dir, threshold, enabled_checks, use_memory),
        daemon=True,
    ).start()

    return job_id, None, 200


def _job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if job is not None:
        return jsonify(_public_job(job))
    # Not in memory (e.g. after restart): try the DB, then a report on disk.
    if db.DB_ENABLED:
        row = db.jobs_get(job_id)
        if row is not None:
            return jsonify(row)
    if (REPORTS_DIR / f'{job_id}.html').exists():
        return jsonify({'status': 'done', 'progress': 100,
                        'step': 'Готово!', 'total': 0,
                        'done_files': 0, 'text_pairs': 0, 'img_pairs': 0})
    abort(404)


def _job_report(job_id: str):
    # Serve directly from disk, works across worker restarts and multi-tab use.
    report_path = REPORTS_DIR / f'{job_id}.html'
    if not report_path.exists():
        abort(404)
    return send_file(str(report_path.resolve()), mimetype='text/html')


def _job_export(job_id: str):
    """Convert the saved HTML report to PDF via WeasyPrint and return it."""
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


def _clear_all():
    from checker.memory_store import clear_store
    with jobs_lock:
        ids = list(jobs.keys())
        jobs.clear()
    for jid in ids:
        p = REPORTS_DIR / f'{jid}.html'
        if p.exists():
            p.unlink(missing_ok=True)
    if db.DB_ENABLED:
        db.jobs_clear()
    cleared_store = clear_store()
    return jsonify({'ok': True, 'cleared': len(ids), 'cleared_store': cleared_store})


def _delete_job(job_id: str):
    """Delete a single check from history: in-memory state, DB row and the
    saved HTML report. Fingerprints in the student base are kept — they are
    managed separately via /memory."""
    with jobs_lock:
        job = jobs.get(job_id)
        if job is not None and job.get('status') == 'processing':
            return jsonify({'error': 'Проверка ещё выполняется — дождитесь '
                                     'завершения или ошибки.'}), 409
        existed = jobs.pop(job_id, None) is not None
    if db.DB_ENABLED:
        existed = db.jobs_delete(job_id) or existed
    report_path = REPORTS_DIR / f'{job_id}.html'
    if report_path.exists():
        report_path.unlink(missing_ok=True)
        existed = True
    if not existed:
        abort(404)
    return jsonify({'ok': True})


def _memory_summary():
    from checker.memory_store import load_store, get_summary
    return jsonify(get_summary(load_store()))


def _memory_delete(key: str):
    from checker.memory_store import delete_entry
    if not key:
        return jsonify({'error': 'Ключ не указан'}), 400
    if not delete_entry(key):
        abort(404)
    return jsonify({'ok': True})


def _all_jobs():
    with jobs_lock:
        mem = {jid: _public_job(j) for jid, j in jobs.items()}
    if db.DB_ENABLED:
        merged = db.jobs_load_all()
        merged.update(mem)   # live in-memory state overrides persisted rows
        return jsonify(merged)
    return jsonify(mem)


def _parse_use_memory(raw) -> bool:
    """Form field `use_memory`: absent (legacy clients) means True."""
    if raw is None:
        return True
    return raw.strip().lower() not in ('0', 'false', 'no', 'off')


@app.route('/upload', methods=['POST'])
@login_required
def upload():
    threshold = float(request.form.get('threshold', 0.6))
    enabled = _parse_enabled_checks(request.form.get('gost'))
    use_memory = _parse_use_memory(request.form.get('use_memory'))
    job_id, error, status_code = _start_job(
        request.files.getlist('files'), threshold, enabled, use_memory)
    if error:
        return jsonify({'error': error}), status_code
    return jsonify({'job_id': job_id})


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
@login_required
def clear_jobs():
    return _clear_all()


@app.route('/jobs/<job_id>/delete', methods=['POST'])
@login_required
def delete_job(job_id):
    return _delete_job(job_id)


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
    return _all_jobs()


def _update(job_id: str, **kwargs):
    snapshot = None
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(kwargs)
            snapshot = _public_job(jobs[job_id])
    if snapshot is not None and db.DB_ENABLED:
        db.jobs_save(job_id, snapshot)


def _process_job(job_id: str, pdf_paths: list, tmp_dir: str, threshold: float,
                 enabled_checks=None, use_memory=True):
    try:
        from checker.memory_store import load_store, to_virtual_report, upsert_report, save_store

        n = len(pdf_paths)

        # 0. Load persistent store (we'll filter it after extraction).
        #    Loaded even with use_memory=False — step 7 appends to it.
        store = load_store()
        if use_memory:
            _update(job_id, step=f'База загружена: {len(store)} записей')
        else:
            _update(job_id, step='Сравнение с базой отключено')

        # 1. Extract
        reports = []
        for i, path in enumerate(pdf_paths):
            _update(job_id,
                    progress=int(5 + 38 * i / n),
                    step=f'Извлечение PDF {i+1}/{n}…',
                    done_files=i)
            reports.append(extract_report(path))
        _update(job_id, progress=44, step='Извлечение завершено', done_files=n)

        # 2. GOST
        _update(job_id, progress=49, step='Проверка ГОСТ 7.32-2017…')
        for r in reports:
            r['gost_results'] = check_gost(r, enabled_checks)

        # 3. Build historical list, excluding students present in this batch.
        #    Without this, a student's new report would match their own stored
        #    fingerprint from a previous session (false "self-plagiarism").
        def _key_base(r: dict) -> str:
            s = r.get('student', {})
            return f"{s.get('name', '').strip().lower()}|{s.get('group', '').strip().lower()}"

        new_keys = {kb for r in reports if (kb := _key_base(r)) != '|'}
        if use_memory:
            historical = [
                to_virtual_report(k, v) for k, v in store.items()
                if v.get('key_base', '') not in new_keys
            ]
            _update(job_id, step=f'Сравниваем с базой: {len(historical)} чужих отчётов')
        else:
            historical = []

        # 4. Text plagiarism (new + relevant historical)
        pairs_count = n * (n - 1) // 2 + n * len(historical)
        _update(job_id, progress=57,
                step=f'Анализ текста ({pairs_count} пар, вкл. базу)…')
        text_plag = check_text_plagiarism(reports + historical, threshold=threshold)
        flagged = len(text_plag.get('pairs', []))
        _update(job_id, progress=76, text_pairs=flagged,
                step=f'Текст: {flagged} подозрительных пар')

        # 5. Image plagiarism (new + historical)
        total_imgs = sum(len(r.get('images', [])) for r in reports)
        _update(job_id, progress=81,
                step=f'Анализ изображений ({total_imgs} шт.)…')
        img_plag = check_image_plagiarism(reports + historical)
        img_flagged = len([p for p in img_plag.get('pairs', [])
                           if not p.get('ui_review')])
        _update(job_id, img_pairs=img_flagged)

        # 6. Generate HTML report
        _update(job_id, progress=90, step='Генерация HTML-отчёта…')
        html = generate_html_report(
            reports, historical, text_plag, img_plag, threshold, job_id=job_id
        )
        (REPORTS_DIR / f'{job_id}.html').write_text(html, encoding='utf-8')

        # 7. Persist fingerprints to store
        _update(job_id, progress=97, step='Сохранение отпечатков в базу…')
        saved = 0
        for r in reports:
            if not r.get('error'):
                upsert_report(store, r, job_id)
                saved += 1
        if saved:
            save_store(store)

        _update(job_id,
                status='done',
                progress=100,
                step=f'Готово! Проверено {n} отчётов, сохранено в базу: {saved}.')

    except Exception as exc:
        import traceback
        _update(job_id,
                status='error',
                step=f'Ошибка: {exc}',
                error=traceback.format_exc())
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


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
    """Start a check. Multipart form: files=<pdf|zip>… , threshold=0.0-1.0,
    gost=<comma-separated check codes> (optional; omit for all checks),
    use_memory=0|1 (optional; 0 skips comparison against the stored base)."""
    threshold = float(request.form.get('threshold', 0.6))
    enabled = _parse_enabled_checks(request.form.get('gost'))
    use_memory = _parse_use_memory(request.form.get('use_memory'))
    job_id, error, status_code = _start_job(
        request.files.getlist('files'), threshold, enabled, use_memory)
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
    return _all_jobs()


@api.route('/jobs', methods=['DELETE'])
@api_auth_required
def api_clear_jobs():
    return _clear_all()


@api.route('/jobs/<job_id>', methods=['GET'])
@api_auth_required
def api_job_status(job_id):
    return _job_status(job_id)


@api.route('/jobs/<job_id>', methods=['DELETE'])
@api_auth_required
def api_delete_job(job_id):
    return _delete_job(job_id)


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
