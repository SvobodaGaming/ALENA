# API Reference (`/api/v1`)

A small REST layer for integrating **АЛЁНА** (Автоматический Ловец Ёрничества,
Небрежности и Аутентичности) — the report checker — into other products.
Every `/api/v1/*` endpoint returns JSON, including errors.

## Authentication

Two interchangeable methods are accepted:

- **API key (machine-to-machine):** send the header `X-API-Key: <key>`. Keys
  belong to an account and are issued in **Пользователи** → *Выдать ключ API*.
  The account must have the permission «Пользоваться API по ключу» (`use_api`);
  without it the key is rejected. `AU_API_KEY` from the environment is assigned
  to the first administrator when the instance bootstraps.
- **Browser session:** a logged-in session cookie also authorizes the API, which
  is what the built-in UI uses. Session-authorized calls that change state
  (`POST`, `DELETE`) must also carry the CSRF token of that session in the
  `X-CSRF-Token` header — the UI reads it from `<meta name="csrf-token">`.
  Without it the call is refused:

  ```json
  { "error": "Сессия устарела — обновите страницу и повторите действие." }
  ```

  Calls authorized by `X-API-Key` are exempt: a browser never attaches that
  header to a cross-site request, so there is nothing to forge.

On a missing, wrong or unauthorized key the API responds `401`:

```json
{ "error": "Не авторизовано: передайте заголовок X-API-Key или войдите в сессию." }
```

Errors use a consistent shape: `{"error": "<message>"}`, with `status` added for
HTTP errors raised by the server (e.g. `404`).

### Roles and visibility

Every call acts **on behalf of the account behind the key or session**, and the
data is scoped to it:

| Role | Sees |
|------|------|
| `teacher` | Only own checks and own fingerprint base. Borrowing is searched inside that base only. |
| `admin` | Everything, when the account keeps the `see_all` permission. |

Reading or deleting another teacher's check answers `403`:

```json
{ "error": "Эта проверка принадлежит другому преподавателю.", "status": 403 }
```

Actions also honour per-account permissions: `run_checks` for `POST /jobs`,
`delete_own` for the delete endpoints, `manage_base` for `DELETE /memory/<key>`.
Reading a check the account already owns (`GET /jobs`, `/jobs/<id>`, its report
and export) needs no extra permission beyond authentication — revoking
`run_checks` stops new checks, it does not hide the account's own history.
Stopping a check (`POST /jobs/<id>/cancel`) likewise needs only ownership: the
account that started it may call it off without the right to delete it.

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
| `files` | file(s) | One or more `.pdf`, `.docx`, `.odt` or `.doc` files, or a `.zip` of them. Repeat the field for several files. DOCX, ODT and DOC are converted to PDF on the server before checking, so the criteria apply to every format identically; a file that cannot be converted is reported as a card with an error and the rest of the batch is checked as usual. The request body must stay under 600 MB — split a larger batch into several checks. (The web interface has no such limit: it uploads in chunks, up to `AU_MAX_UPLOAD_MB`.) |
| `threshold` | float | Text-similarity threshold, `0.0`-`1.0` (default `0.6`). Values outside the range are clamped to it; anything unparsable (`abc`, `NaN`, `inf`, empty) falls back to the default instead of failing the request. |
| `gost` | string | Optional comma-separated GOST check codes to evaluate, e.g. `S1,S3,F7,F9`. Omit the field to run all 20 checks; an empty value runs none. Codes: `S1`-`S9` (structural elements), `F1`-`F11` (formatting). |
| `use_memory` | string | Optional. `0`/`false` skips plagiarism comparison against the stored fingerprint base (only files within the batch are compared). New reports are still added to the base. Default `1`. |
| `weights` | string | Optional per-criterion weight for the recommended grade, `0`-`100` each: `S1:100,F1:20,F2:100`. Codes not listed keep `100`. The weights of the criteria actually selected are normalised to sum to 100. Omit the field to use the weights set by the administrator. |
| `scale` | int | Optional grade scale, `2`-`100`. `100` (default) means the grade is a percentage; any other value also reports the grade in points of that scale. |

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

`400` when no files are supplied or no report is found inside the upload.

### GET `/jobs`
List the jobs visible to the caller, keyed by `job_id`:

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
    "beat": 1782639600.42,
    "owner": "sokolova",
    "owner_fio": "Соколова Елена Викторовна",
    "threshold": 60,
    "summary": {
      "group": "ПР-21-1",
      "groups": ["ПР-21-1"],
      "gost": 87,
      "plag": 63,
      "threshold": 60,
      "clean": 8,
      "grade": 74,
      "grade_score": 7.4,
      "scale": 10,
      "weighted": true,
      "no_text": 1,
      "students": [
        { "fio": "Петров П. П.", "group": "ПР-21-1", "gost": 61,
          "plag": 63, "no_text": false, "fails": ["S2", "F3", "F7"],
          "flaws": [
            { "code": "S2", "text": "Отсутствует лист задания на практику (курсовую работу)",
              "details": "Лист задания не обнаружен" }
          ],
          "grade": {
            "pct": 58, "score": 5.8, "scale": 10, "criteria": 20,
            "lost": [{ "code": "F3", "name": "Основной текст 14 пт", "weight": 9.9 }]
          },
          "error": null }
      ],
      "matches": [
        { "a": "Петров П. П.", "b": "Белов А. Р. · ПР-20-1", "pct": 47,
          "kind": "текст", "where": "база, 18.06.2025" }
      ],
      "matches_total": 7,
      "fail_counts": [["F7", 4], ["S2", 2]]
    },
    "error": null
  }
}
```

`status` is one of `processing`, `done`, `error` and `cancelled` — the last one
means the check was stopped on request (see `POST /jobs/<id>/cancel`) and has no
report and no summary. `summary` is `null` until the check finishes;
`matches[].pct` is `null` for duplicate images, which are a yes/no match rather
than a share of the text.

`students[].no_text` is `true` when too little text could be extracted from the
file to compare it with anything — a scan, or a PDF whose fonts carry no usable
encoding. Such a work takes no part in the similarity matrix at all and its
`plag` is `null`, not `0`: borrowing in it is *unknown*, not *absent*, and has
to be checked by hand. `summary.no_text` counts them for the batch.
`matches` carries at most the 500 most prominent matches and `matches_total`
says how many there were: a course of screenshot-heavy reports produces tens of
thousands, and the digest is re-sent on every poll of the check list. The full
picture is in the HTML report, which likewise shows the closest image pairs
rather than all of them.

`beat` is a Unix timestamp the running check refreshes every 15 seconds. A
check still marked `processing` whose `beat` is over 90 seconds old is treated
as lost — the process that ran it died — and is reported as an error on the
next read. Clients need not act on it; poll `status` as before.

`grade` is the recommended formatting mark in percent — the share of the
normalised criterion weights that the work earned; `grade_score` restates it in
points of `scale` and is `null` when the scale is percent. `weighted` is `true`
when the weights were not all equal. `students[].flaws` is the flat list the
teacher pastes to the student portal: one plain-language line per failed
criterion, in GOST order, with the checker's own wording in `details`.
Checks that ran before this feature existed have no `grade` or `flaws`.

### GET `/jobs/<job_id>`
Poll one job. Same shape as a list entry. The record is read from the running
check when there is one, otherwise from stored history — so a restart does not
lose it. `404` when the id is unknown or malformed.

### POST `/jobs/<job_id>/cancel`
Stop a running check. The worker notices at its next progress step, deletes the
uploaded files and settles on `status: "cancelled"`: no report is written and no
fingerprints reach the base. A check that has already started writing its report
finishes — that tail takes seconds. `409` when the check is no longer running.

```json
{ "ok": true }
```

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
Delete the caller's jobs, their report files, and the caller's fingerprint base.
An account with `see_all` wipes everyone's.

```json
{ "ok": true, "cleared": 4, "cleared_store": 21 }
```

### GET `/memory`
List stored student fingerprints (no text or hashes, just metadata). A teacher
sees only their own base:

```json
[
  {
    "key": "sokolova|иванов иван|пр-21-1|v1",
    "version": 1,
    "filename": "Иванов_отчёт.pdf",
    "student": { "name": "Иванов Иван", "group": "ПР-21-1" },
    "pages_count": 14,
    "image_count": 3,
    "added_at": "20.06.2026 09:35",
    "job_id": "a1b2c3d4e5",
    "owner": "sokolova"
  }
]
```

The key is `<owner>|<name>|<group>|v<N>` — the owner prefix is what keeps two
teachers' bases apart when they have a namesake in the same group.

### DELETE `/memory/<key>`
Delete one fingerprint entry. Requires the `manage_base` permission. The `key`
is the value from `GET /memory` and must be URL-encoded (it contains `|` and
spaces). `403` for another teacher's entry, `404` if the key does not exist.

```
DELETE /api/v1/memory/sokolova%7C%D0%B8%D0%B2%D0%B0%D0%BD%D0%BE%D0%B2%20%D0%B8%D0%B2%D0%B0%D0%BD%7C%D0%BF%D1%80-21-1%7Cv1
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
  echo "$st" | grep -qE '"status": *"(error|cancelled)"' && break
  sleep 2
done

# 2a. Or stop it early
# curl -s -X POST -H "X-API-Key: $KEY" "$BASE/jobs/$job/cancel"

# 3. Fetch outputs
curl -s -H "X-API-Key: $KEY" "$BASE/jobs/$job/report" -o report.html
curl -s -H "X-API-Key: $KEY" "$BASE/jobs/$job/export" -o report.pdf
```

## CORS

Not enabled by default (the UI is same-origin). To call the API from a browser
on another origin, add `flask-cors` and restrict it to trusted origins.
