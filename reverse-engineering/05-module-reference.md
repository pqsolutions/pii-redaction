# Module Reference

*Per-file technical breakdown, for engineers. Line counts and route lists
reverse-engineered directly from the source.*

## `app.py` (2,941 lines) — Flask web layer

The entire web application: routing, session/API-key auth, the inlined
HTML/JS/CSS frontend (no template files, no build step, no JS framework), MySQL
orchestration, and subprocess management for the heavy pipeline.

### Route inventory

| Route | Method | Purpose | Auth |
|---|---|---|---|
| `/` | GET | Serves the inlined SPA-style page; decrypts `?uid=` into the session | none |
| `/api/clients` | GET | Client dropdown (from `project`) | none |
| `/api/jobs` | GET | Job typeahead (from `activity`, debounced) | none |
| `/api/processes` | GET | Named process list for the rehydrate picker | `_require_uid()` |
| `/upload` | POST | Accepts files, mints ids, launches the pipeline subprocess | optional API key (never hard-gated) |
| `/stream/<job_id>` | GET | SSE progress stream for an upload | none |
| `/api/redact` | POST | Bulk caller-supplied-value redaction (async) | `_require_uid()` |
| `/stream/redact/<process_id>` | GET | SSE stream for `/api/redact` | none |
| `/download/<filename>` | GET | Download by on-disk filename | none |
| `/download/id/<file_id>` | GET | Download by `file_id` (disambiguates same-named files) | `_require_uid()` |
| `/rehydrate` | GET | Legacy rehydrate page (unlinked from UI, kept in code) | none (page) |
| `/rehydrate` | POST | Legacy single-`file_id` rehydrate | `_require_uid()` |
| `/rehydrate/upload` | POST | Current rehydrate flow — derived-document upload | `_require_uid()` |
| `/api/redaction-map/<process_id>` | GET | Reference table data for a batch | `_require_uid()` |
| `/api/preview/<file_id>` | GET | Inline preview payload | `_require_uid()` |
| `/api/preview/<file_id>/ocr-region` | POST | On-demand OCR of a preview region | `_require_uid()` |
| `/api/redaction-map/add` | POST | Manual "add a missed value" | `_require_uid()` |
| `/api/redaction-map/<row_id>` | DELETE | Delete a manual row (restores original first) | `_require_uid()` |
| `/api/redaction-map/<row_id>` | PUT | Edit a manual row | `_require_uid()` |
| `/api/categories` | GET | Category dropdown list | `_require_uid()` |
| `/api/categories` | POST | Add a new category | `_require_uid()` |

Every route is documented in Swagger via `@swag_from("docs/swagger/<name>.yml")`,
served at `/apidocs/`.

### Key internal functions

- **`_require_uid()`** — the single auth gate used across the app: passes if
  `session["uid"]` is set (browser flow) **or** a valid `X-API-Key` header is
  present. Returns 403 if neither credential exists, 401 if an API key was sent
  but is wrong.
- **`_api_key_valid()`** — `hmac.compare_digest(provided, API_KEY)`, deliberately
  constant-time to avoid a timing side-channel on key comparison.
- **`_run_pipeline()`** — the background-thread entry point for an upload: diffs
  the output directory before/after the subprocess exits to discover which files
  were produced (format-agnostic — doesn't care what extension came out), then
  patches MySQL (`update_upload_status`, `update_file_output`) per file.

## `dual_redaction_pipeline.py` (4,051 lines) — the heavy ML pipeline

Runs **only as a subprocess**, never imported directly by `app.py`, because it
eagerly loads Docling, GLiNER, Presidio, and OpenCV at import time — multi-second,
GB-scale. See [01-system-architecture.md](01-system-architecture.md) for why this
split exists.

Structure:
- **Phase 1 functions**: `pdf_to_excel_docling()`, `docx_to_excel()`,
  `spreadsheet_to_excel()`, `convert_doc_to_docx()` (Word COM), plus the Tesseract
  OCR fallback path.
- **Phase 2 functions**: `scan_text_for_entities()` (Tier 0–2 detection),
  Ollama batch-inference calls (Tier 3), `get_candidate_token()` (token minting),
  `apply_masking()` (the actual substitution).
- **"As is" writers**: `write_redacted_xlsx_as_is()`, `write_redacted_xls_as_is()`,
  `write_redacted_docx_as_is()`, `write_redacted_pdf_as_is()`,
  `write_redacted_image_as_is()` — one per format, each preserving that format's
  native structure instead of flattening to Excel.
- **Unmask functions**: `unmask_file()`, `unmask_documents()` (legacy CLI),
  `unmask_as_is()` and its per-format siblings.
- **`main()`** — CLI entry point; branches on `--manifest`, `--unmask`,
  `--unmask-file`, `--output-format`, and the plain "process everything in
  `./files/`" default mode. Contains resume-safe skip logic (won't reprocess a
  file that already has output on disk) — `app.py` deliberately deletes prior
  output before invoking this, so the *web* flow always reprocesses from scratch
  regardless of this module's own resume behaviour.

## `redaction_formats.py` (1,427 lines) — lightweight, synchronous format handling

A deliberately **duplicated**, not shared, subset of `dual_redaction_pipeline.py`'s
per-format logic — same masking/unmasking behaviour, but built with only
lightweight dependencies (`openpyxl`, `xlrd`, `python-docx`, `pymupdf`, `pillow`)
so `app.py` can import and call it directly, in-process, with no ML-stack loading
cost. This is what makes the manual-correction UI and previews feel instant rather
than taking as long as a full pipeline run.

Key functions:
- **`render_preview()`** — dispatches by format to produce a browser-renderable
  payload: HTML table (xlsx/xls), structured HTML (docx), paginated PNG images
  (pdf, and docx when pixel-perfect fidelity is needed via a Word-COM → PDF
  conversion), or direct image (png/jpg).
- **`mask_single_value()` / `unmask_single_value()`** — the single-value find/
  replace used by the manual "add missed value" flow and the bulk API endpoint.
- **`next_token_for_category()`** — reconstructs the next free per-category token
  counter by parsing existing tokens already in `pii_redaction_map`, so a manual
  addition doesn't collide with an existing one.
- **`guess_category(value)`** — lightweight regex/checksum category auto-detection
  (duplicated from the pipeline's Tier 0 patterns) for the bulk API, falling back
  to a generic `"PII"` category for free text.
- **`find_tokens_in_file()` / `rehydrate_uploaded_file()`** — scans an arbitrary
  uploaded document for token-looking strings and performs the conflict-aware
  substitution described in [02-workflows.md](02-workflows.md) Workflow 4.
- **`remask_image_from_original()`** — since pixel redaction on images is
  destructive (no searchable text layer survives), image "rehydration" works by
  decrypting and restoring the original stored bytes instead of searching for
  tokens.

## `db.py` (600 lines) — all MySQL access

Every SQL statement in the application lives here — no other module talks to
MySQL directly. Organized into clear sections (see [04-data-model.md](04-data-model.md)
for the schema each of these reads/writes):

- Upload logging (`log_upload`, `update_upload_status`)
- Client/job lookups against the external `project`/`activity` tables
  (`get_clients`, `get_job`, `search_jobs`)
- Client code resolution (`get_or_create_client_code`)
- Id sequence allocation (`next_id`)
- File/redaction-map CRUD (`record_file`, `insert_redaction_rows`,
  `get_redaction_map`, `get_redaction_map_for_process`, `get_secret_map_for_job`,
  `list_processes`)
- Reference-table support (`get_files_for_process`, `get_redaction_rows`,
  `insert_redaction_row`, `delete_redaction_row`)
- Category management (`get_categories`, `add_category`)

Every function opens and closes its own connection (no pooling) — acceptable at
this application's request volume, but worth knowing if load ever increases
materially.

## `crypto_utils.py` (74 lines) — AES encryption

Two independent uses of the same AES key, derived once via
`PBKDF2-HMAC-SHA1(password, salt, 1000 iterations)`:

1. **`encrypt()` / `decrypt()`** — a **fixed derived IV**, matching a legacy .NET
   helper class byte-for-byte (`Rfc2898DeriveBytes`), because this is how the
   pre-authenticated `?uid=` link is generated by another system and must be
   decrypted identically here. UTF-16LE encoding (`.NET`'s `Encoding.Unicode`).
2. **`encrypt_bytes()` / `decrypt_bytes()`** — a **random IV per call** (prepended
   to the ciphertext), used for storing original image bytes securely. No
   external compatibility constraint here, so it doesn't need the fixed-IV scheme.

The hardcoded key/salt (`"MAKV2SPBNI99212"` / `"Ivan Medvedev"`) are a known,
publicly-documented example pair from an old ASP.NET tutorial — see
[06-security-and-auth.md](06-security-and-auth.md) for why this matters.

## `serve.py` (41 lines) / `windows_service.py` (62 lines) — deployment entrypoints

- `serve.py` — production entrypoint via Waitress (8 threads, 2-hour channel
  timeout to tolerate long pipeline runs, 100 connection limit).
- `windows_service.py` — wraps `serve.py`'s server startup as a Windows Service
  (`pywin32`), so the app restarts automatically on failure/reboot.

## Configuration surfaces

- **`pii_rules.yaml`** — hot-reloaded redaction rules (see
  [03-pii-detection-pipeline.md](03-pii-detection-pipeline.md)).
- **`.env`** — `SECRET_KEY` (Flask session signing), `API_KEY` (server-to-server
  auth), `DB_HOST`/`DB_PORT`/`DB_USER`/`DB_PASSWORD`/`DB_NAME`.
- **Constants near the top of `dual_redaction_pipeline.py`** — folder paths, DPI,
  GLiNER model id/threshold, Ollama model/host/timeout/thread-count, hardcoded
  Tesseract binary path.
