# Architecture

## Recent Changes

**2026-09-17** — Review timer on the results screen
(see [plan-timer-implementation.md](feature-updates/plan-timer-implementation.md)):

- A pill in the right corner of the **Output** header shows **System** time,
  which is the pipeline's own time: `MIN(uploaded_at)` to `MAX(completed_on)` in
  `PII_redation_upload_log`, blank while any file is pending. It also shows
  **Review** time, the user's time from the first **Select** click to the latest
  **Download** click.
- Review time pauses after `REVIEW_IDLE_SECONDS` (`.env`, default 15) without
  mouse or keyboard input. The idle stretch is removed from the time and logged
  as `idle_time`. It also pauses while the tab is hidden
  (`visibilitychange`), logged as `tab_hidden_time`. It resumes on the next
  input.
  How idle detection works is described in Section 9 of the plan.
- A Download click stops the timer and saves. Input doesn't restart it; only
  another Select click does, continuing from the saved total.
- Every Download click upserts one row per batch per user into
  `pii_process_review_time` (migrations `0009` and `0010`; durations are TIME columns shown as `HH:MM:SS`) via
  `POST /api/process/<process_id>/review-time`. `GET` on the same path returns
  the system time and any saved review time, so reopening a batch (e.g. from a
  DMS link) continues from the saved value. Time since the last download is
  discarded if the page is closed without downloading again.

**2026-08-25** — Session cookie made permanent (30 days):

- `app.permanent_session_lifetime = timedelta(days=30)` set in `app.py`,
  plus `session.permanent = True` at all 3 places `uid` gets decrypted
  into the session (`index()`, `api_link_context()` in `app.py`;
  `dashboard._dashboard_authorize()`). Previously no session lifetime was
  configured anywhere, so Flask's default (non-permanent — cleared the
  moment the browser fully closes) applied.
- Investigated as a fix for "the URL loses `?uid=...` after clicking
  internal links (Dashboard, Rehydrate, etc.)" — turned out the session
  cookie already carries `uid` across every internal path automatically
  (confirmed: a `/dashboard` request with no `?uid=` in its own URL,
  relying only on a cookie set by an earlier `/` visit, is correctly
  authorized). The only real gap was durability, not propagation — so a
  much larger plan to rewrite every internal link's href to carry
  `?uid=` was considered and dropped in favor of this two-line fix.
- Full rationale: [wiki/feature-updates/phase-1/plan-uid-link-propagation-implementation.md](feature-updates/phase-1/plan-uid-link-propagation-implementation.md).

**2026-08-25** — Dashboard: TL tab + Upload Volume chart label fix:

- **New 4th tab, "TL"** (next to Cluster): card grid grouped by each
  active user's direct Team Leader (`db.get_tl_user_breakdown()`) —
  resolves `user.TLID` straight to `user.Uid` (no `teamleaderusermaster`
  needed, unlike Cluster's Cluster-Head resolution). Styled with a teal
  gradient header and circular count badge, distinct from Cluster's navy/
  pill style. `/dashboard/download` gained a matching "TL Breakdown"
  sheet. Verified live: per-card file counts reconcile exactly to the
  independent ground-truth upload total.
- **Fixed**: the Upload Volume Over Time bar chart
  (`_dashboard_bar_chart_svg()`) showed zero x-axis date labels once a
  range had more than 20 active days (the default 30-day window commonly
  does) — an all-or-nothing cutoff left the axis completely blank. Now
  always shows labels, thinning them out (every `ceil(n/15)`th bar, plus
  first/last) instead of hiding them entirely.
- Full rationale: [wiki/feature-updates/phase-1/plan-dashboard-implementation.md](feature-updates/phase-1/plan-dashboard-implementation.md), Sections 17-18.

**2026-08-25** — Dashboard code extracted into its own file:

- All dashboard code (routes, chart builders, HTML templates, auth) moved
  out of `app.py` into a new [dashboard.py](../dashboard.py), registered
  as a Flask Blueprint (`dashboard_bp`) via `app.register_blueprint()` —
  no logic or URL/behavior change, purely a maintainability move (`app.py`
  had grown to 4,524 lines; dropped to 3,792 after the move).
  `_dashboard_authorize()` still needs `app.py`'s `_decrypt_uid_param()`;
  since importing it at module level would create a circular import, that
  one import is deferred to inside the function body, resolved only at
  request time. Full regression-tested (11 checks: both admin/non-admin
  paths, all three tabs, the Excel download, both header-link locations,
  and the deferred cross-module import itself) with zero behavior change.
  Full rationale: [wiki/feature-updates/phase-1/plan-dashboard-implementation.md](feature-updates/phase-1/plan-dashboard-implementation.md), Section 16.

**2026-08-25** — Admin analytics dashboard:

- **`GET /dashboard`**: org-wide analytics page — 4 stat cards (total files,
  PII values redacted, upload success rate, total uploads), an "Upload
  Volume Over Time" bar chart, a "Success/Fail Breakdown" donut chart, a
  "PII Entities Masked by Category" horizontal bar chart, and a "Top
  Clients by Volume" table. Defaults to the last 30 days; a `start`/`end`
  date-range filter (GET query params) widens or narrows it. All charts are
  hand-rolled inline SVG (`_dashboard_bar_chart_svg()` /
  `_dashboard_hbar_chart_svg()` / `_dashboard_donut_chart_svg()` in
  `app.py`) — no charting library dependency.
- **`GET /dashboard/download`**: same data as an `.xlsx` workbook (Summary /
  Upload Volume / Status Breakdown / PII Category Breakdown / Top Clients
  sheets), built with `openpyxl`.
- **Access control**: both routes are gated to a fixed admin `uid` allowlist
  (`DASHBOARD_ADMIN_UIDS` in `app.py`, not a new DB-backed role/permission
  system) via `_dashboard_authorize()`. Any other uid — or no uid at all —
  gets a 403 "Access Denied" page, not a scoped/empty dashboard. The
  "Dashboard" header nav link (on `/` and `/rehydrate`) is itself only
  rendered for allowlisted uids.
- Data queries (`db.get_dashboard_summary()`, `get_upload_volume_over_time()`,
  `get_upload_status_breakdown()`, `get_pii_category_breakdown()`,
  `get_top_clients()`) are org-wide — no per-user client scoping — since
  access is already gated to a small trusted admin set. PII category counts
  are derived by parsing the existing `<*CATEGORY_N*>` token shape out of
  `pii_redaction_map.redacted_text` in Python (same regex shape as
  `_TOKEN_RE`/`_DMS_TOKEN_CATEGORY_RE` elsewhere), since category isn't a
  stored column.
- No new tables/columns — reads existing `PII_redation_upload_log`,
  `pii_files`, `pii_redaction_map`, and `project`.
- **Vertical & Cluster tabs**: `/dashboard` now has three tabs (Summary /
  Vertical / Cluster), switched client-side with no page reload
  (`showDashboardTab()`). **Vertical** shows upload volume/success-fail
  grouped by team (`db.get_vertical_breakdown()`, joining
  `user.TeamID = coemaster.Code` — every matched `coemaster` team is shown
  by name, not just the 6 flagged `IsBusinessVertical = 1` client
  verticals; that filter was tried first, then removed after it turned
  out to be hiding real teams like "Technology Group" behind "Unassigned"
  rather than surfacing a genuine data gap).
  **Cluster** shows a card grid — one card per Cluster Head (titled with
  their `Firstname` field alone, which can itself be multi-word, e.g.
  "Senthil Kumar" — not the first word of the full name), listing each
  active user ("Firstname - Employeeid") with a per-user file-count badge
  (`db.get_cluster_user_breakdown()`). Both derive the org hierarchy from
  `user.TLID` → `teamleaderusermaster` → `CHID` back to the Cluster Head's
  own `user` row, resolved via `db._resolve_tlid_to_chid()` (Python-side,
  not a raw SQL join — see below) — read (never write) the org's HR
  tables `user`, `teamleaderusermaster`, `coemaster`, which this app
  doesn't own, same posture as its existing `project`/`activity`/
  `userwiseclient` reads. Users that don't resolve to a vertical/Cluster
  Head are bucketed as "Unassigned" rather than dropped. No
  PII-redacted-count column on either tab — only 13/210
  `PII_redation_upload_log` rows have `process_id` populated, so a
  redaction count couldn't be reliably attributed per user for most
  history; upload counts (which have `uid` directly) are fully reliable
  and used instead. Both tabs reuse the same `DASHBOARD_ADMIN_UIDS` gate
  and date-range filter as Summary. `/dashboard/download` gained matching
  "Vertical Breakdown" / "Cluster Breakdown" sheets — the latter still
  built from a separate aggregate function, `db.get_cluster_breakdown()`
  (team size + active-user count + success/fail per Cluster Head), which
  the on-page card grid no longer uses directly but which remains useful
  in the full export.
- **Bug found and fixed while building the card grid**: the original
  `FIND_IN_SET(u.TLID, tl.TLid) > 0` join fanned out whenever
  `teamleaderusermaster` had more than one row for the same `TLID` (a real,
  confirmed data-quality gap — e.g. `TLID 48` has both a row resolving to
  a real Cluster Head and a stale row with `CHID` NULL), causing some
  users to be double-counted across two buckets. Fixed by resolving
  `TLID → CHID` once in Python (`db._resolve_tlid_to_chid()`, preferring a
  non-null `CHID`, tie-broken by highest `TLMID`) instead of relying on
  the SQL join. Verified: `sum(file_count)` across every user in every
  Cluster tab card now reconciles exactly to
  `get_dashboard_summary()["total_uploads"]`.
- Full design/rationale: [wiki/feature-updates/phase-1/plan-dashboard-implementation.md](feature-updates/phase-1/plan-dashboard-implementation.md).

**2026-07-23** — Bulk redaction-request API:

- **`POST /api/redact`**: given an existing `process_id` and, per file, an
  existing `file_id` + a list of `source_values`, masks each value wherever
  it's found in that file's already-redacted output — exact, whole-word,
  case-insensitive matching only, no automated PII detection (the caller
  already knows what to redact). Reuses the same
  `mask_single_value()`/`next_token_for_category()`/`insert_redaction_row()`
  building blocks as `POST /api/redaction-map/add`, looped per file/value.
  Runs in a background thread; the request returns immediately with
  `{process_id, stream_url}`.
- **`GET /stream/redact/<process_id>`**: SSE stream of the above — one
  `file_done` event per completed file, then a final `done` event with the
  full `{process_id, files: [...]}` result. Mirrors `POST /upload` +
  `GET /stream/<job_id>`'s existing background-thread/queue pattern, kept in
  a separate `_redact_queues` dict so it can never collide with a live
  upload stream sharing the same `process_id`.
- **Category auto-detection**: `redaction_formats.guess_category()` (new) —
  a fixed-priority set of structured regex/checksum patterns
  (TFN/ABN/ACN/Medicare/phone/email/BSB+account), duplicated from
  `dual_redaction_pipeline.py`'s Tier-0 patterns. No match (the common case
  for free text) falls back to a generic `"PII"` category/token prefix.
  Real NER-based categorization was considered and declined — see the plan.
- `pii_redaction_map.source` enum extended with `'api'`
  (`migrations/0007_pii_redaction_map_source_api.sql`) to distinguish these
  rows from pipeline-detected and reference-table-manual ones.
- Gated by `_require_uid()`, same as every other `/api/*` route (see API key
  subsection below); `/stream/redact/<process_id>` is ungated, matching
  `/stream/<job_id>`.
- Full design/rationale: [wiki/feature-updates/phase-1/plan-api-redaction-implementation.md](feature-updates/phase-1/plan-api-redaction-implementation.md).

**2026-07-13** — Process Name tracking + rehydrate redesign + job-scoped tokens:

- **Process Name tracking**: the upload page now has a required "Process Name"
  textbox above the Client dropdown (`migrations/0005_pii_files_process_name.sql`
  adds `pii_files.process_name`) — a free-text label per upload batch, stored on
  every file row in that batch, for easy tracking/lookup later.
- **Rehydrate page redesigned**: `/rehydrate` no longer takes a hand-typed
  `file_id`. It now has Client → Job → Process Name selectors (`GET
  /api/processes?client_id=&job_id=`, backed by `db.list_processes()`), shows a
  read-only table of the files in the selected process (reusing the existing
  `GET /api/redaction-map/<process_id>`), then the user uploads the *derived*
  document (e.g. a prepared ITR draft built from several redacted source files)
  to a new endpoint, **`POST /rehydrate/upload`**, which restores the original
  values in that upload and streams it back. The old file_id-only `/rehydrate`
  route is left in the code (untouched) but no longer linked from the UI.
- **Conflict-aware, process_id-scoped token map**: `db.get_redaction_map_for_process()`
  merges every file's tokens across a batch without silently colliding
  same-named tokens from different files; `redaction_formats.rehydrate_uploaded_file()`
  scans the uploaded document for tokens actually present and substitutes only
  the unambiguous ones, returning a warning for any left unresolved.
- **Job-scoped token allocation** (`db.get_secret_map_for_job()`, new): token
  minting during redaction now shares one `secret_map`/`candidate_counters` per
  client+job (seeded from prior `pii_redaction_map` rows for that job) instead of
  resetting per file — the same real value always mints the same token going
  forward, which is what makes the conflict-aware merge above rarely need to
  fall back to a warning in practice. `db.get_redaction_map()` was updated to
  return `(mapping, conflicts)` to match.
- Full design/rationale: [wiki/feature-updates/phase-1/plan-rehyrdration-implementation.md](feature-updates/phase-1/plan-rehyrdration-implementation.md).

## System Overview

```
Browser (http://127.0.0.1:5009)
        │
        │  GET  /api/clients, /api/jobs        (Client/Job selection)
        │  POST /upload                        (client_id + job_id required)
        ▼
    app.py  ──────────────────────────────────────────────────
   (Flask)                                                     │
        │  GET  /stream/<process_id>  (Server-Sent Events)     │
        │  GET  /download/<file>            (by filename)      │
        │  GET  /download/id/<file_id>      (by file_id,       │
        │         disambiguates same-named files — session      │
        │         or X-API-Key required)                       │
        │  GET/POST /rehydrate, GET /api/processes,            │
        │  POST /rehydrate/upload  (process-name picker)       │
        │                                                      │
        ▼  subprocess                                          │
dual_redaction_pipeline.py                                     │
        │                                                      │
        ├── Phase 1: PDF → Raw Excel                           │
        │       Tier 1: Docling  (layout + text)               │
        │       Tier 2: PyMuPDF  (fallback OCR)                │
        │       Tier 3: Tesseract (image-PDF fallback)         │
        │                                                      │
        └── Phase 2: Raw Excel → Redacted Excel                │
                Tier 0: Regex + Checksum  (deterministic)      │
                Tier 1: Structural labels (field detection)    │
                Tier 2: GLiNER NER        (contextual ML)      │
                Tier 3: Ollama LLM        (privacy decisions)  │
                                                               │
   Output files ◄──────────────────────────────────────────────
   files/output/redacted/  ← redacted Excel (downloadable)
   files/output/raw/       ← raw extracted Excel
   audit/                  ← compliance audit log
   pii_redaction_map (MySQL) ← token→value map, keyed by client/job/process/file
```

## Folder Structure

```
script_pii/
├── app.py                         # Flask web server
├── dashboard.py                   # /dashboard, /dashboard/download (admin analytics — Flask Blueprint)
├── serve.py                       # Waitress production WSGI server
├── windows_service.py             # Windows Service wrapper
├── dual_redaction_pipeline.py     # Core pipeline (2 542 lines)
├── pii_rules.yaml                 # Configurable redaction rules
├── pii_reference.md               # PII category documentation
├── files/
│   ├── *.pdf / *.xlsx             # Input files (uploaded via UI)
│   └── output/
│       ├── raw/                   # Raw extracted Excel files
│       └── redacted/              # Final redacted Excel files
├── audit/                         # pii_audit_log_*.xlsx
├── secrets/                       # legacy pii_secret_log_*.xlsx (ad-hoc CLI runs only) + forensic JSONL
├── unmasked/                      # Restored documents (--unmask / --unmask-file / /rehydrate)
├── migrations/                    # CREATE TABLE SQL for the PII MySQL tables
├── db.py                          # MySQL: upload log, client/job lookups, redaction map
├── src/
│   └── requirements.txt
├── wiki/                          # This documentation
└── pii_env/                       # Python virtual environment
```

## Data Flow

```
Input PDF
    │
    ▼
[Phase 1] PDF Extraction
    ├─ Docling parses layout + text → raw DataFrame
    │     └─ Fallback: PyMuPDF find_tables() + native text
    │           └─ Fallback: Tesseract OCR on page images
    │
    ▼
Raw Excel  (files/output/raw/{name}_raw.xlsx)
    │
    ▼
[Phase 2] PII Detection
    ├─ Tier 0: Regex (TFN, Medicare, ABN, ACN, Phone, Email…) → AUTO-REDACT
    ├─ Tier 1: Structural labels ("given name:", "dob:") → candidate
    ├─ Tier 2: GLiNER chunked NER → candidate with confidence score
    └─ Tier 3: Ollama (gemma3:4b) → MASK / KEEP decision
    │
    ▼
apply_masking()
    │  Replaces matched values with tokens: "John Smith" → [PERSON:001]
    │
    ▼
Redacted Excel  (files/output/redacted/{name}_redacted.xlsx)
Audit Log       (audit/pii_audit_log_{name}.xlsx)
Redaction Map   (MySQL pii_redaction_map — manifest-driven web uploads)
                (or secrets/pii_secret_log_{name}.xlsx — ad-hoc CLI runs with no manifest)
Forensic Log    (secrets/pii_forensic_log.jsonl)
```

## AI/ML Components

| Component | Model | Purpose |
|-----------|-------|---------|
| **Docling** | Granite-Docling 258M (GPU) / Layout Heron (CPU) | PDF layout analysis, table extraction, OCR |
| **GLiNER** | `urchade/gliner_multi_pii-v1` | Named entity recognition with contextual awareness |
| **Ollama** | `gemma3:4b` (local) | Privacy decision-making: MASK vs KEEP |
| **Presidio** | Custom AU recognizers | Regex-backed entity detection for AU-specific IDs |
| **Tesseract** | v5.4 | Fallback OCR for image-based scanned PDFs |

## Threading Model

- Flask runs on the main thread with `threaded=True`
- Each upload spawns a `threading.Thread` + `subprocess.Popen`, keyed by that upload's `process_id` (e.g. `PRC-ABC-000120`) — there's no separate ad-hoc UUID anymore, the same id used for DB attribution is used as the SSE/job-queue key
- Pipeline stdout is streamed line-by-line into a `queue.Queue`
- `/stream/<process_id>` reads from that queue and pushes SSE events to the browser
- Job state lives in `_job_queues` dict (in-memory, single-process)

## Upload Logging (MySQL)

`db.py` records every uploaded **file** as its own row in `PII_redation_upload_log`,
separate from the in-memory job/queue state above, so upload history survives process
restarts and each file can be attributed to the employee (`uid`) who uploaded it.

```
POST /upload
    │
    ▼
for each file: db.log_upload(uid, input_file)
    INSERT ... file_count=1, status='pending'   → returns log_id
    (collected into a file_id → log_id map)
    │
    ▼
threading.Thread(_run_pipeline, process_id, filenames, file_id_to_log_id)
    │
    ▼
subprocess.Popen(dual_redaction_pipeline.py)
    │
    ├─ per-file result available → db.update_upload_status(log_id, status, error_message, output_file=redacted_filename)
    ├─ no per-file result        → db.update_upload_status(log_id, status, error_message)  (batch-wide fallback, output_file=NULL)
    └─ python/script missing     → every log_id in the batch marked 'fail' immediately
```

Every `update_upload_status` call also stamps `completed_on = CURRENT_TIMESTAMP`.
`status`/`error_message` are still batch-wide — `dual_redaction_pipeline.py` reports one
exit code for the whole run, so if a batch fails every row in it gets `status='fail'`
even if some files produced output. Only `input_file`/`output_file`/`completed_on` are
genuinely per-file.

The `uid` comes from an encrypted query-param decrypted in `index()` via
`crypto_utils.decrypt()` and stashed in the Flask `session`. If no `uid` is present
(e.g. direct access without the encrypted link), the upload proceeds but is not logged.

### API key (server-to-server callers)

Alongside the browser session above, `app.py` accepts an `X-API-Key` header as a
second, independent credential for non-browser callers (scripts, other systems) —
see `wiki/feature-updates/phase-1/plan-api-key-implementation.md` for the design.

- `_require_uid()` — the gate on `/api/processes`, `/rehydrate` (POST),
  `/rehydrate/upload`, `/api/redaction-map/*`, `/api/preview/*`, `/api/categories`,
  `/download/id/<file_id>`, `/api/redact` — passes if *either* `session["uid"]` is set (browser flow, unchanged) *or* the
  request carries a valid `X-API-Key` header. A missing session **and** a missing
  key still fails with 403 (unchanged wording); a session-less request with a
  **present but wrong** key fails with 401 instead, so a broken integration reads
  as "your key is wrong," not "you're just not logged in."
- `POST /upload` still has no hard auth gate (unchanged, browser flow keeps working
  with zero credential) — but if it receives an `X-API-Key` header, that header must
  be valid (401 if not), and the uid to log the upload under is then read from a
  `uid` form field instead of the session (there is no session to read it from).
- The key itself is one shared secret, `API_KEY` in `.env` (same shape as
  `SECRET_KEY`), compared with `hmac.compare_digest` (constant-time, avoids leaking
  a partial match via response timing) — see `_api_key_valid()` in `app.py`.
- `/apidocs/` (Swagger UI, see below) declares this as an `apiKey` security scheme
  named `ApiKeyAuth`, so it can be set once via the UI's Authorize button.

Schema:

```sql
CREATE TABLE PII_redation_upload_log (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    uid           INT NOT NULL,
    file_count    INT,
    status        ENUM('pending','success','fail') NOT NULL DEFAULT 'pending',
    error_message TEXT NULL,
    input_file    VARCHAR(255) NULL,
    output_file   VARCHAR(255) NULL,
    uploaded_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_on  TIMESTAMP NULL
);
```

`file_count` stays at 1 per row now that logging is per-file (kept for backward
compatibility with any existing reporting). See `db.py`'s module docstring for the
`ALTER TABLE` statements to bring an older table up to date.

Connection details (`DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`) come
from the environment — see `.env.example`. A fresh MySQL connection is opened and
closed per call (`log_upload`, `update_upload_status`) rather than pooled.

## Client / Job Selection

Every upload must be tagged with a **Client** and a **Job** before it runs, so the
redaction map (below) can be attributed and later looked up. These are **not** new
business entities owned by this app — they're read live from the existing
practice-management tables in the same MySQL DB:

- **Client** = `project` row (`Pid`, `ClientName`, `ShortName`, `access`). The
  `GET /api/clients` dropdown lists `project` rows with `access=1` (147 of 318 today).
- **Job** = `activity` row (`Aid`, `Pid`, `jobdescription`, `Date`) — a work-log entry,
  not a small fixed catalog (one client alone has 19k+ activity rows). `GET
  /api/jobs?client_id=&q=` is a live typeahead: `SELECT ... WHERE Pid=%s AND
  jobdescription LIKE %s ORDER BY Date DESC LIMIT 20`, driven from the browser with a
  250ms debounce. The Job field is a search input + suggestion list, not a plain
  `<select>`.

`app.py` does not migrate or write to `project`/`activity` — only reads.

## DB-backed Redaction Map & Rehydration

Redaction is reversible by design — every masked value can be restored. As of this
feature, the token↔value map for **web uploads** is stored centrally in MySQL
(`pii_redaction_map`) instead of a per-file local secret log, and rehydration is
exposed in the web UI at `/rehydrate`. This also fixes the earlier failure mode:
the old flow matched a redacted file to its secret map purely by filename stem in a
single shared `files/` folder, so two uploads with the same filename (different
clients, or just bad timing) could silently overwrite each other's mapping.

**Token generation** (`get_candidate_token()` in `dual_redaction_pipeline.py`, unchanged):
each unique PII value is assigned a token of the form `<*CATEGORY_N*>`
(e.g. `<*PERSON_1*>`, `<*EMAIL_1*>`).

**IDs**: every upload batch gets a `process_id` (`PRC-<CODE>-NNNNNN`), every file in
it gets a `file_id` (`FIL-<CODE>-NNNNNN`), both minted atomically via
`db.next_id()` / `pii_id_sequences`. `<CODE>` is the client's 3-letter code
(`db.get_or_create_client_code()`): `project.ShortName` when set, otherwise derived
from `ClientName` (first 3 letters, deduped with a numeric suffix on collision) —
`ShortName` is blank for ~85% of active clients today, so the generated fallback is
the common case, not an edge case. The resolved code is cached in
`pii_client_codes` so a client always gets the same code.

**Upload flow** (`POST /upload` in `app.py`):

```
client_id + job_id + files
    │
    ▼
db.get_or_create_client_code(client_id, ...) → code
db.next_id("PRC", code)                      → process_id
db.next_id("FIL", code)  (once per file)     → file_id
db.record_file(file_id, process_id, ...)     → pii_files row
    │
    ▼
files/manifest_{process_id}.json   {client_id, job_id, process_id, files:[{filename, file_id}]}
    │
    ▼
subprocess: dual_redaction_pipeline.py --manifest <path>
    │  reads exactly the files named in the manifest (not a folder glob)
    ▼
db.insert_redaction_rows(client_id, job_id, process_id, file_id, [(source_text, redacted_text), ...])
    → one pii_redaction_map row per unique PII value found in that file
```

**Rehydration** — two paths, both sourced from the DB and gated behind
`session["uid"]` being set (i.e. the app was opened via the normal encrypted
`?uid=` link) since both reverse redaction:

1. **Rehydrate the pipeline's own stored output**, by `file_id` (legacy path,
   kept in code but no longer linked from the UI — see 2026-07-13 above):

   ```
   POST /rehydrate  (form: file_id)   ── or ──   python dual_redaction_pipeline.py --unmask-file <file_id>
       │
       ▼
   db.get_file(file_id)               → pii_files row (redacted_path, filename)
   db.get_redaction_map(file_id=...)  → (mapping, conflicts) — process-scoped, conflict-aware
       │
       ▼
   _apply_token_map() / unmask_as_is()  — string-replace every safe token occurrence
       │
       ▼
   unmasked/{stem}_unmasked.xlsx
   ```

2. **Rehydrate an uploaded, derived document** (current UI flow — e.g. an ITR
   draft built elsewhere from several redacted source files):

   ```
   GET /rehydrate → Client → Job → Process Name pickers, then a files table
       │
       ▼
   POST /rehydrate/upload  (form: process_id, file)
       │
       ▼
   db.get_files_for_process(process_id)          → every file_id in the batch
   db.get_redaction_map_for_process(process_id)  → {token: {source_text: [file_id,...]}}
       │
       ▼
   redaction_formats.rehydrate_uploaded_file()
       — scans the upload for tokens actually present, substitutes the
         unambiguous ones, leaves conflicting/unknown tokens as literal text
       │
       ▼
   rehydrated/{stem}_rehydrated{ext}  (+ X-Rehydrate-Warnings header if any conflicts)
   ```

**Legacy CLI path** (`unmask_documents()`, run via `python dual_redaction_pipeline.py
--unmask`): still works exactly as before, but only for files produced by an ad-hoc
CLI run with no `--manifest` — those still write `secrets/pii_secret_log_{stem}.xlsx`
since there's no client/job to attribute a DB row to. Manifest-driven web uploads no
longer write that per-file xlsx at all; `pii_redaction_map` is the single source of
truth for them.

**Security note:** the plaintext original values live in `pii_redaction_map` (MySQL)
for web uploads, and in `secrets/*.xlsx` for ad-hoc CLI runs — either is as sensitive
as the unredacted source documents. `secrets/pii_forensic_log.jsonl` (append-only
audit trail of every masking decision) is written regardless of manifest/DB usage and
only contains raw values for entries it explicitly redacted.

### Schema

```sql
-- see migrations/0001_pii_redaction_tables.sql for the full, idempotent version

CREATE TABLE pii_client_codes (
    pid    INT NOT NULL PRIMARY KEY,   -- project.Pid
    code   VARCHAR(3) NOT NULL UNIQUE,
    source ENUM('shortname','generated') NOT NULL
);

CREATE TABLE pii_id_sequences (
    prefix     VARCHAR(8) NOT NULL PRIMARY KEY,  -- 'FIL' or 'PRC'
    next_value INT NOT NULL
);

CREATE TABLE pii_files (
    file_id       VARCHAR(20) NOT NULL PRIMARY KEY,
    process_id    VARCHAR(20) NOT NULL,
    process_name  VARCHAR(255) NULL,           -- migrations/0005, NULL for pre-2026-07-13 rows
    client_id     INT NOT NULL,   -- project.Pid
    job_id        INT NOT NULL,   -- activity.Aid
    filename      VARCHAR(255) NOT NULL,
    redacted_path VARCHAR(500),
    output_format ENUM('excel','as_is') NOT NULL DEFAULT 'excel',      -- migrations/0002
    original_encrypted_path VARCHAR(500)                                -- migrations/0003
);

CREATE TABLE pii_redaction_map (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    client_id     INT NOT NULL,
    job_id        INT NOT NULL,
    process_id    VARCHAR(20) NOT NULL,
    file_id       VARCHAR(20) NOT NULL,
    source_text   TEXT NOT NULL,
    redacted_text TEXT NOT NULL,
    source        ENUM('pipeline','manual') NOT NULL DEFAULT 'pipeline'  -- migrations/0004
);
```

No FK constraints against `project`/`activity` — this app reads them but doesn't own
their migrations. See `migrations/0001` through `0005` for the full, idempotent
`ALTER`/`CREATE` history behind this schema.

## Output Format: Excel vs As is

`pii_files.output_format` (`migrations/0002_pii_output_format.sql`) records which
output format a file's `redacted_path` actually is: `'excel'` (flattened `.xlsx`,
the original/default behaviour) or `'as_is'` (redacted in the original file's own
format — PDF/DOCX/DOC/XLSX/XLS). The upload-time value in this column is only a
placeholder; `_run_pipeline()` in `app.py` patches it to the *actual* format used
once the pipeline subprocess finishes (via a `manifest_{process_id}_result.json`
sidecar the pipeline writes), since "As is" can fail and fall back to Excel
per-file at runtime.

PDF "as is" redaction (`write_redacted_pdf_as_is()` in `dual_redaction_pipeline.py`)
is a separate, self-contained pass over the *original* PDF using PyMuPDF redaction
annotations — it does not reuse or modify the existing Docling/PyMuPDF/Tesseract
Raw-Excel extraction path described in [pipeline-details.md](pipeline-details.md).
It locates each PII value's bounding box via the PDF's native text layer, falling
back to Tesseract OCR word boxes for scanned/image-only pages, then draws over the
original text and burns in the token at that exact location. Rehydration
(`unmask_pdf_as_is()`) reverses this by searching the redacted PDF for the
burned-in token text (a real, searchable text layer written by the redaction
step) and swapping in the original value the same way.

Full design/rationale: [wiki/feature-updates/phase-1/plan-as-it-is-implementation.md](feature-updates/phase-1/plan-as-it-is-implementation.md).
