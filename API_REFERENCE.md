# API Reference (`/api/v1`)

A small REST layer for integrating **АЛЁНА** (Автоматический Ловец Ёрничества,
Небрежности и Аутентичности) — the report checker — into other products.
Every `/api/v1/*` endpoint returns JSON, including errors.

## Authentication

Two interchangeable methods are accepted:

- **API key (machine-to-machine):** send the header `X-API-Key: <AU_API_KEY>`.
  Set `AU_API_KEY` in the environment (`.env`). When it is empty, key access is
  disabled.
- **Browser session:** a logged-in session cookie also authorizes the API, which
  is what the built-in UI uses.

On a missing or wrong key the API responds `401`:

```json
{ "error": "Не авторизовано: передайте заголовок X-API-Key или войдите в сессию." }
```

Errors use a consistent shape: `{"error": "<message>"}`, with `status` added for
HTTP errors raised by the server (e.g. `404`).

## Base URL

```
http://<host>:5000/api/v1
```

## Endpoints

### GET `/health`
Public liveness check. No auth. `db` reflects whether `DATABASE_URL` is set
(Postgres) or the JSON fallback is in use.

```json
{ "ok": true, "weasyprint": true, "db": true }
```

### POST `/jobs`
Start a check. `multipart/form-data`:

| Field | Type | Notes |
|-------|------|-------|
| `files` | file(s) | One or more `.pdf` files, or a `.zip` of PDFs. Repeat the field for several files. |
| `threshold` | float | Text-similarity threshold, `0.0`-`1.0` (default `0.6`). |
| `gost` | string | Optional comma-separated GOST check codes to evaluate, e.g. `S1,S3,F7,F9`. Omit the field to run all 15 checks; an empty value runs none. Codes: `S1`-`S6` (structural elements), `F1`-`F9` (formatting). |
| `use_memory` | string | Optional. `0`/`false` skips plagiarism comparison against the stored fingerprint base (only files within the batch are compared). New reports are still added to the base. Default `1`. |

`201 Created`:

```json
{
  "job_id": "a1b2c3d4e5",
  "status": "processing",
  "links": {
    "self":   "/api/v1/jobs/a1b2c3d4e5",
    "report": "/api/v1/jobs/a1b2c3d4e5/report",
    "export": "/api/v1/jobs/a1b2c3d4e5/export"
  }
}
```

`400` when no files are supplied or no PDF is found inside the upload.

### GET `/jobs`
List in-memory jobs, keyed by `job_id`:

```json
{
  "a1b2c3d4e5": {
    "status": "done",
    "progress": 100,
    "step": "Готово! Проверено 12 отчётов, сохранено в базу: 12.",
    "total": 12,
    "done_files": 12,
    "text_pairs": 3,
    "img_pairs": 1,
    "created_at": "28.06.2026 11:40",
    "error": null
  }
}
```

### GET `/jobs/<job_id>`
Poll one job. Same shape as a list entry. Returns `done` with `progress: 100` if
the job is gone from memory (e.g. after a restart) but the report file still
exists on disk. `404` if neither is found.

### GET `/jobs/<job_id>/report`
The self-contained HTML report (`Content-Type: text/html`). `404` if missing.

### GET `/jobs/<job_id>/export`
The report as PDF (`Content-Type: application/pdf`, attachment). `404` if the
report is missing, `501` if WeasyPrint is not installed on the server.

### DELETE `/jobs/<job_id>`
Delete a single job: its history entry (memory + DB) and the saved HTML report.
Stored student fingerprints are kept — remove them via `DELETE /memory/<key>`.
`409` while the job is still processing, `404` if nothing was found.

```json
{ "ok": true }
```

### DELETE `/jobs`
Delete all jobs, their report files, and the entire memory store.

```json
{ "ok": true, "cleared": 4, "cleared_store": 21 }
```

### GET `/memory`
List stored student fingerprints (no text or hashes, just metadata):

```json
[
  {
    "key": "иванов иван|пр-21-1|v1",
    "version": 1,
    "filename": "Иванов_отчёт.pdf",
    "student": { "name": "Иванов Иван", "group": "ПР-21-1" },
    "pages_count": 14,
    "image_count": 3,
    "added_at": "20.06.2026 09:35",
    "job_id": "a1b2c3d4e5"
  }
]
```

### DELETE `/memory/<key>`
Delete one fingerprint entry. The `key` is the value from `GET /memory` and must
be URL-encoded (it contains `|` and spaces). `404` if the key does not exist.

```
DELETE /api/v1/memory/%D0%B8%D0%B2%D0%B0%D0%BD%D0%BE%D0%B2%20%D0%B8%D0%B2%D0%B0%D0%BD%7C%D0%BF%D1%80-21-1%7Cv1
```

```json
{ "ok": true }
```

## Example: end-to-end with curl

```bash
KEY=your-api-key
BASE=http://localhost:5000/api/v1

# 1. Start a job
job=$(curl -s -X POST "$BASE/jobs" \
  -H "X-API-Key: $KEY" \
  -F "files=@reports.zip" -F "threshold=0.6" | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")

# 2. Poll until done
while :; do
  st=$(curl -s -H "X-API-Key: $KEY" "$BASE/jobs/$job")
  echo "$st"
  echo "$st" | grep -q '"status": *"done"' && break
  echo "$st" | grep -q '"status": *"error"' && break
  sleep 2
done

# 3. Fetch outputs
curl -s -H "X-API-Key: $KEY" "$BASE/jobs/$job/report" -o report.html
curl -s -H "X-API-Key: $KEY" "$BASE/jobs/$job/export" -o report.pdf
```

## CORS

Not enabled by default (the UI is same-origin). To call the API from a browser
on another origin, add `flask-cors` and restrict it to trusted origins.
