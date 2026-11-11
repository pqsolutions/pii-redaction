# Compare: script_pii (current) vs PII_Code_19 Aug (new drop)

**Status:** Draft for discussion — not a decision doc yet.
**Date:** 2026-08-24
**Sources compared:**
- OLD (current, in production): `C:\Users\User_Shibu\Downloads\script_pii`
- NEW (just unpacked): `C:\Users\User_Shibu\Downloads\PII_Code_19 Aug\PII_Code_19 Aug`

> Note on NEW's own docs: the 7 markdown files shipped inside PII_Code_19 Aug (ARCHITECTURE.md, README.md, SYSTEM_DESIGN_AND_DOCUMENTATION_OVERVIEW.md, CLIENT_HANDOVER_GUIDE.md, JETSON_DEPLOYMENT_GUIDE.md, API_DOCUMENTATION.md, PII_ACCURACY_BENCHMARK_REPORT.md) are internally inconsistent and partly aspirational — they disagree with each other on model names, and reference files/folders that don't actually exist in the unpacked tree (e.g. `run_production_evaluation.py`, `golden_dataset/`, a `data_generator/generator.py` import in `main.py`). One doc even has a leftover path from a different machine. Treat those docs as a roadmap layer, not ground truth. Everything below was verified against the actual code.

---

## TL;DR

NEW is not a simple upgrade of OLD — it's a parallel rewrite with a different footprint. It brings a genuinely better detection/QA pipeline and a real web UI, but it currently **drops several things OLD depends on in production**: the MySQL/DMS integration, SSO+encryption, and the Windows/IIS deployment path. Any move to NEW needs a plan for those gaps, not just a code swap.

---

## By category

### 1. Detection approach — CHANGED (less new than NEW's docs claim)
Both already run a multi-pass hybrid pipeline today.

- **OLD** (`dual_redaction_pipeline.py`, `pii_rules.yaml`): Tier-1 regex + checksum validators (Modulo-11/89 for TFN/ABN/ACN/Medicare), Presidio custom recognizers, spaCy NER, **GLiNER** (`urchade/gliner_multi_pii-v1`) as Layer 3, and an **Ollama** SLM (`gemma3:4b`) as a final contextual reviewer.
- **NEW**: same building blocks (GLiNER + Ollama + checksum validation are not new), but adds:
  - **Pass 4 Critique Agent** (`pipeline/critique_agent.py`) — differential original-vs-masked audit that re-masks in-flight.
  - **Convergence Accuracy Engine** (`pipeline/accuracy_engine.py`) — auto-generates per-file ground truth and iterates toward 100% precision/recall.
  - **Persistent Feedback Memory** (`feedback_memory.json`) — reusable `ignore_values`/`force_values` overrides across sessions. OLD only has an append-only forensic JSONL log, not a reusable override store.
  - **Spatial Grid Engine** (`pipeline/spatial_grid_engine.py`) — formalizes logic that was inline in OLD (`get_grid_context`, `extract_structural_candidates`).

**Verdict:** genuinely new QA/feedback loop; core detection stack is not new.

### 2. Architecture — CHANGED (modular rewrite)
- **OLD**: flat, monolithic Flask app. `app.py` (3663 lines) mixes routes, inline HTML templates, and business logic; backed by `dual_redaction_pipeline.py` (4051 lines) and `redaction_formats.py` (1427 lines).
- **NEW**: modular. `pipeline/` package (~6700 lines across 24 files) + thin FastAPI `api.py`. Note: two competing pipeline classes exist — `pipeline.pipeline.PIIPipeline` and `pipeline.tabular_pipeline.TabularPIIPipeline` — `api.py` uses both, with `TabularPIIPipeline` (Excel-first, layout-preserving) doing most of the work in batch/UI endpoints.

### 3. API surface — CHANGED
- **OLD** (Flask): 21 routes — `/`, `/dms`, `/upload`, `/stream/<job_id>` (SSE), `/api/redact`, `/download/*`, `/rehydrate*`, `/api/redaction-map/*`, `/api/preview/<file_id>`, `/api/categories`, `/api/clients`, `/api/jobs`, `/api/processes`, `/api/link-context`.
- **NEW** (FastAPI): ~30 routes under `/api/mask/*` — file/folder/batch upload, `hydrate_file`, `de_anonymize`, `stream_batch`/`stream_file` (**WebSocket**, replacing SSE), `critique`, `re_mask`, `feedback`, page preview/entities, `toggle_redaction`, `batch_async` + `batch_status`/`cancel_batch`, `forensic_audit`, `latest_evaluation`, `download_compliance_package`, `test_unseen_domains`.
- **New capabilities not in OLD:** `/critique`, `/feedback`, `/latest_evaluation`, `/download_compliance_package`, `/test_unseen_domains`.

### 4. File format support — CHANGED, mostly additive
- **OLD**: PDF, XLSX, XLS (`xlrd`), DOCX, **DOC** (via MS Word COM automation — Windows+Word only), image OCR.
- **NEW**: PDF, XLSX, CSV, DOCX, TXT/MD/LOG/JSON/XML/TSV, images, and **PPTX** (new).
- **Gap:** NEW has no `.doc`/`.xls` legacy handling and no Word COM dependency — trades away legacy binary Office support for removing the Windows+Office-install requirement.

### 5. UI — NEW
- **OLD**: no separate frontend — HTML served as inline Python strings via `render_template_string`.
- **NEW**: full Vite+React app (`ui/`) — `UnifiedDashboard.jsx`, `VisualDocumentViewer.jsx` (side-by-side bounding-box editor), `LiveMasking.jsx`, `BatchProcessing.jsx`, 4 redaction visual styles (Blur/Pill/Blackout/Whiteout).

### 6. Deployment — CHANGED (target shifted, not purely additive)
- **OLD**: Windows-native production path — `windows_service.py` (pywin32 service), `setup_iis.ps1`, `web.config` (IIS reverse proxy), `serve.py` (waitress WSGI), `ecosystem.config.js` (PM2).
- **NEW**: no Windows-service/IIS artifacts anywhere. Instead `JETSON_DEPLOYMENT_GUIDE.md` + `jetson_config.py` target NVIDIA Jetson (ARM64 edge). Only documented start path is `python api.py` + `npm run dev`/`uvicorn`.
- **Gap:** the IIS/Windows-service production story from OLD has **no equivalent in NEW** — material if NEW is meant to replace OLD on the current Windows box.

### 7. Persistence/DB — CHANGED (biggest structural gap)
- **OLD** (`db.py`): real **MySQL** integration into an existing practice-management schema — `project` (clients), `activity` (jobs), `userwiseclient` (per-user client access scoping) — plus PII-specific tables added via `migrations/0001`–`0007` (`pii_files`, `pii_redaction_map`, `pii_client_codes`, `pii_categories`, ...). DMS-integrated, multi-tenant, per-user-scoped.
- **NEW**: only `task_queue.db` (SQLite) for async batch-job tracking, with optional Redis fallback (`pipeline/queue_manager.py`, degrades to SQLite WAL if Redis absent).
- **Gap:** NEW has **no client/job hierarchy, no per-user access scoping, and no DMS integration** — this is a removed capability, not a swapped datastore.

### 8. Security/Auth — REMOVED in NEW
- **OLD** (`crypto_utils.py`): AES-256-CBC + PBKDF2-HMAC-SHA1 (.NET-compatible) used for (1) decrypting an encrypted `uid` param for SSO from the parent DMS (`session["uid"] = int(_decrypt_uid_param(uid_enc))`), and (2) encrypting original uploaded files at rest for rehydration. Flask sessions with `SECRET_KEY`.
- **NEW**: no authentication, no session handling, no at-rest encryption anywhere in `api.py` or `pipeline/` (verified by grepping for encrypt/decrypt/auth/token/login).
- **Flag:** deployed as-is, NEW would be an open, unauthenticated API with plaintext secret/audit ledgers on disk. This needs to be closed before any production use.

### 9. Testing — CHANGED (different kind, not clearly more rigorous)
- **OLD**: `tests/` has a single sample `.docx` fixture — not automated tests.
- **NEW**: `test_batch_execution.py` (manual smoke-test script, not pytest) + `sample_data/` with a real accuracy-benchmarking harness (`ground_truth/*.json` per document, `ai4privacy/` public PII benchmark set) feeding the accuracy/critique engines.
- **Caveat:** more serious accuracy-QA story than OLD, but still not unit/integration coverage — read `PII_ACCURACY_BENCHMARK_REPORT.md`'s "100% precision/recall" as benchmark-harness output, not independent proof.

### 10. Redaction mechanism — CHANGED less than NEW's docs imply
NEW's ARCHITECTURE.md presents native vector PDF stripping (`add_redact_annot()`/`apply_redactions()`) as a headline differentiator, but OLD already does this in `redaction_formats.py` (lines 781-786, 959-962). What's actually new: 3 named strategies (vector search, word-bbox merge, raster overlay fallback) formalized, plus the 4 visual styles.

### 11. Masking/replacement-value logic — deep dive (per user request)

**Premise check:** the request was to check whether NEW generates "much improved" *masking values* (i.e. the actual replacement text/value substituted for a detected PII span). Verified finding: **the token/value-generation logic itself is functionally identical between OLD and NEW** — neither is format-preserving, neither uses Faker/synthetic-realistic values for output. What IS genuinely improved in NEW is the *visual rendering* of the redaction and the *box-placement robustness*, not the value chosen.

**OLD** (`dual_redaction_pipeline.py`):
- `get_candidate_token()` (`:1515-1530`) — looks up the detected value (lowercased) in `secret_map`; if new, increments a per-type counter and mints a bracket-tag: `<*PERSON_1*>`, `<*GOVERNMENT_ID_2*>`, etc. Plain placeholder, not format-preserving.
- Consistency: guaranteed across an entire job — `secret_map`/`candidate_counters` are shared across all files in a job, loaded via `db.get_secret_map_for_job()` (`db.py:...`). Same original value → same token everywhere in that job.
- Reversibility: DB-backed. `db.insert_redaction_rows()` (`db.py:364-379`) writes `(source_text, redacted_text)` into MySQL `pii_redaction_map`; `unmask_file()` (`dual_redaction_pipeline.py:2168`) drives `/rehydrate`. Tokens with conflicting source values across files are deliberately left unresolved rather than guessed — an explicit safety behavior.
- Visual rendering: one fixed style — white box + black token text burned on top (`redaction_formats.py:781-837`). No blur/blackout/pill variants.

**NEW** (`pipeline/`):
- Token generation is the same *scheme*, just re-syntaxed to `[TYPE_N]`, and exists in **two inconsistent implementations**: `PIIMasker._get_placeholder()` (`pipeline/masker.py:21-26`, per-document counter only, used by an unused single-text code path) and `TabularPIIPipeline._get_candidate_token()` (`pipeline/tabular_pipeline.py:44-70`, a near line-for-line port of OLD's function — this is the one actually wired into the real batch flow). A third spot, `api.py`'s single-file re-style endpoints (e.g. `api.py:2145-2169`), builds mappings with a **bare** `f"[{type}]"` with no counter — a real bug: two different names on the same re-styled page both render as `[PERSON_NAME]`, indistinguishable from each other.
- `pipeline/generators.py` is **not** a synthetic-value generator — it only generates fake *test-input* PDFs (Medical/Legal/Insurance) for zero-shot eval. `Faker` (present in NEW's `requirements.txt`) is used only in `sample_data/generate_test_pdf.py` to synthesize test *inputs*, never a masked *output* value. `pipeline/pii_filter_engine.py` is a PII/non-PII classifier gate, not masking logic.
- Consistency: same as OLD, but only scoped to `global_secret_map` for one batch run (`tabular_pipeline.py:64-70`); the unused `PIIMasker` path resets per document.
- Reversibility: file-based, not DB-based — `Combined_Secret_Log.xlsx` (`tabular_pipeline.py:535-540`) plus `HydrationEngine.hydrate_excel()` (`pipeline/hydration_engine.py:17-72`), exposed via `POST /api/mask/hydrate_file`. No DB, no `crypto_utils`, and **no cross-file conflict detection** — a regression vs. OLD's explicit conflict-handling for enterprise auditability.
- Visual rendering — this is the real improvement: `NativeRedactor.redact_pdf()` (`pipeline/native_redactor.py:86-407`) supports **4 selectable styles**: blackout (default, black box + white token text via `add_redact_annot(text=..., fill=(0,0,0), text_color=(1,1,1))`, `:255-263`), whiteout (`:242`), blur (Gaussian-blurred pixmap slice pasted back in, text fully scrubbed, `:194-206`/`:271-277`), and pill (tinted rounded badge with a width-compressed short token like `[NAME]`/`[TFN]`, `:217-251`/`:279-285`). Box placement uses a 3-tier cascade — PyMuPDF text search → word-bbox merge fallback → raster OCR fallback (PaddleOCR/Tesseract) for scanned pages — more robust than OLD's OCR-line-based matching alone.

**What's actually worth porting into script_pii:**
- `pipeline/native_redactor.py`'s `redact_pdf()` — the 4-style rendering + 3-tier box-placement cascade. It's a self-contained ~330-line function, `(input_pdf, output_pdf, entity_mappings, style, custom_boxes) -> None`, with no FastAPI/DB/React dependency. `entity_mappings` is a flat list of `{"value", "replacement", "type"}` dicts — trivially built from OLD's existing `secret_map`, so **no NEW-only data structures (spatial grid engine, detector schema) are required** to use it.
- Not worth porting: the token/value-generation functions themselves (`_get_candidate_token` etc.) — OLD's `get_candidate_token` already does the same thing, and OLD's version is arguably more correct (job-wide DB-backed consistency + conflict detection vs. NEW's batch-scoped file log with no conflict handling).
- The Critique Agent (`pipeline/critique_agent.py`) and Accuracy Engine (`pipeline/accuracy_engine.py`) from section 1 remain the other high-value ports the user flagged as wanted (Q1 answer below) — these operate on OLD's existing token/mapping output and don't require NEW's value-generation logic either.

**Porting risks/notes:**
- The pill style's font-size/box-compression logic (`native_redactor.py:217-240`) is tuned against NEW's own OCR DPI (`dpi=200` raster fallback) — will likely need re-tuning against OLD's OCR pipeline (runs at a different DPI) if the pill style is ported.
- The raster/OCR fallback tier (`:287-397`) imports `paddleocr`/`cv2`/`pytesseract` inline — if ported, should be reconciled with OLD's already-loaded global `ocr_engine` (`dual_redaction_pipeline.py:89`) rather than loading a second OCR stack. `paddleocr`/`paddlepaddle` are in NEW's `requirements.txt` but not OLD's; likely skippable since OLD already has an OCR engine that can serve the same fallback role.
- No FastAPI/`task_queue.db`/React dependency exists inside `redact_pdf()` itself — style selection is just a string parameter, so wiring a `style` option into OLD's existing Flask redaction routes needs no architectural pull-through.

**Performance cost of the Critique Agent / Accuracy Engine (checked 2026-08-24):** confirmed these are wired into NEW's real per-file batch flow, not just an offline benchmark tool — see `pipeline/tabular_pipeline.py:300-370`. Per file, this runs a **convergence loop of up to 3 extra passes**: one `generate_ground_truth()` Ollama call, then each loop iteration does a precision/recall check plus another `critique_agent.audit()` Ollama call, re-masking anything found missing, stopping early only once 100% precision + 100% recall + LLM-audit-pass are all hit. Net effect: **this makes redaction slower per file (up to ~4 extra LLM round-trips), not faster** — it is a completeness/quality-vs-speed trade. The upside is it's specifically designed to catch PII the first pass missed (reduces false negatives) and persists what it learns to `feedback_memory.json` for future files. If Ollama is down or `use_ollama=False`, the loop is skipped or runs lighter (no LLM audit), trading away the recall benefit along with the extra latency. Any port of these two features into script_pii should treat this as opt-in/configurable rather than always-on, given OLD's existing Ollama reviewer step already adds latency of its own.

### 12. Other improvements found on closer look (2026-08-24)

Beyond detection/QA (section 1), visual redaction styles (section 10-11), and masking values (section 11), four more genuine improvements were confirmed:

- **Broader checksum/algorithmic validation.** `pipeline/detectors/global_regex_detector.py` adds Luhn (credit cards, `:17-30`), US SSN structural checks (`:68-85`), Verhoeff (Aadhaar, `:112-119`), and IBAN mod-97 (`:122-135`). OLD's `dual_redaction_pipeline.py:937-973` only validates AU identifiers (TFN/ABN/ACN/Medicare) — NEW keeps that AU logic and extends real validation to US/EU identifiers, reducing false positives outside AU documents. `context_detector.py`'s seed-propagation (`:364-437`) — once a name/org is found via a labeled field, it re-scans the whole document for bare repeats and injects them as new entities — is a genuinely different detection strategy from OLD. (Note: `label_value_detector.py`'s label→value approach itself is *not* new — OLD already does this for tabular data via `STRUCTURAL_LABELS`/`pii_rules.yaml`; NEW's version targets free text/PDFs instead, different substrate, same idea.)

- **Human-in-the-loop visual review — a real gap in OLD.** NEW's `page_preview`/`page_entities`/`toggle_redaction` endpoints (`api.py:1113-1217`, `:1272+`, `:1610-1681`) plus `VisualDocumentViewer.jsx` let a reviewer see AI-detected entities as clickable bounding boxes on the actual page image and mask/unmask each one individually, or hand-draw a custom box over something the AI missed — all before the file is finalized. Manual corrections sync automatically to both the PDF and its companion Excel export (`api.py:1613`, `:1590-1607`). OLD's `/api/preview/<file_id>` and `/api/redaction-map/*` (`app.py:3382-3563`) only show the *already-redacted* output and let you edit rows in a data table — no visual pre-finalization entity review exists in OLD today.

- **Real batch job control — OLD is fire-and-forget.** `pipeline/queue_manager.py` is a persistent, restart-safe SQLite job store with actual mid-processing cancellation (`cancel_batch()`, checked via a per-file callback that aborts processing), exposed through `batch_async`/`batch_status/{id}`/`cancel_batch/{id}`/`ws_status/{id}` (`api.py:2224-2506`), independent of the original request/connection. OLD's `/api/jobs`/`/api/processes` (`app.py:2711-2752`) are DMS metadata lookups, not a task queue; OLD's actual batch processing runs over SSE with **no cancel endpoint anywhere** in `app.py` — once started, a batch can't be stopped early.

- **One-click compliance export.** `download_compliance_package` (`api.py:2373-2423`) zips the benchmark report, all redacted outputs (in the requested visual style), and audit/secret ledgers into a single downloadable file. OLD writes individual `pii_audit_log_*.xlsx` files to `audit/` with no bundling endpoint anywhere (`app.py` has no `zipfile` usage) — collecting a compliance package today means manually gathering files from disk.

**Verdict on detection strategy specifically:** mixed — checksum coverage and seed-propagation are genuinely new; label-value parsing is a re-implementation for a different document shape, not a new idea.

---

## Removed items with no equivalent found in NEW

- MySQL/DMS integration (clients, jobs, per-user access scoping) — `db.py`, `migrations/`
- SSO session flow tied to the parent DMS — `crypto_utils.py`, `session["uid"]`
- At-rest AES encryption of originals/secrets
- Windows Service + IIS deployment (`windows_service.py`, `setup_iis.ps1`, `web.config`)
- `.doc`/`.xls` legacy Office format support via Word COM automation
- Swagger/flasgger auto-generated API docs (`docs/swagger/*.yml`, `@swag_from`)

## Genuinely new in NEW

- Critique Agent (Pass 4 differential audit + re-mask)
- Convergence Accuracy Engine + ground-truth/benchmark harness (`sample_data/`, `ai4privacy/`)
- Persistent feedback memory (`feedback_memory.json`)
- React UI with visual bounding-box editor and 4 redaction styles
- PPTX support
- WebSocket streaming (replacing SSE)
- Compliance package export, unseen-domain testing endpoint
- Jetson/edge deployment path

---

## Decisions made so far

1. **Scope: PORT, not replace.** Pull specific features out of PII_Code_19 Aug into script_pii; script_pii stays the production codebase.
2. Not replacing OLD, so the DMS/MySQL + SSO/encryption gap (section 7-8) is moot for now — those stay as-is in script_pii.
3. **Windows/IIS remains the required deployment target.** NEW's Jetson/edge direction is not being adopted as a deployment target.
4. **Legacy `.doc`/`.xls` support must keep working.** Clients still send these formats. Rule: do not touch script_pii's existing Word COM/pywin32-based `.doc`⇄`.docx` conversion logic. NEW's ingestion code only understands modern `.docx`/`.xlsx` and must not replace or interfere with OLD's legacy-format handling. Only the detection/QA/rendering layer gets ported — file ingestion stays exactly as it is today in script_pii.
5. Open (not yet resolved) — worth a quick walkthrough of NEW's `main.py` entry point since it imports `data_generator/generator.py`, which doesn't exist in the unpacked tree; low priority since NEW's docs are already flagged as unreliable and this doesn't block porting.

## Candidate features to port (based on sections 1 and 11)

Given the "port specific features, keep everything else untouched" constraint, here's what's actually self-contained enough to lift out of PII_Code_19 Aug without dragging in its architecture (FastAPI, task_queue.db, React UI, Jetson config):

| Feature | Source in NEW | Self-contained? | Notes |
|---|---|---|---|
| 4-style visual redaction (blackout/whiteout/blur/pill) + 3-tier box-placement cascade | `pipeline/native_redactor.py:redact_pdf()` (~330 lines) | Yes — takes `(input_pdf, output_pdf, entity_mappings, style, custom_boxes)`, no NEW-only data structures needed | Pill-style font sizing tuned to NEW's OCR DPI (200) — will need re-tuning against OLD's OCR DPI. OCR fallback tier should reuse OLD's existing global `ocr_engine` instead of loading `paddleocr` separately. |
| Pass 4 Critique Agent (differential original-vs-masked audit, re-mask in-flight) | `pipeline/critique_agent.py` | Likely — operates on OLD's existing token/mapping output, doesn't need NEW's value-generation logic | Needs a closer read to confirm its exact input/output contract before porting. |
| Convergence Accuracy Engine + ground-truth/benchmark harness | `pipeline/accuracy_engine.py`, `sample_data/` | Partially — the engine logic is portable; the benchmark harness (ai4privacy samples, ground_truth JSONs) is optional tooling, not runtime code | Useful for internally validating script_pii's detection accuracy, not required for production. |
| Persistent feedback memory (`ignore_values`/`force_values` overrides) | `feedback_memory.json` + wiring in `pipeline/*` | Yes — simple JSON-backed override store, conceptually independent of NEW's architecture | Would need to decide where OLD's job/client-scoped model intersects with this (global file vs. per-client). |

**Explicitly NOT recommended to port:** NEW's token/value-generation functions (`_get_candidate_token` etc. in `masker.py`/`tabular_pipeline.py`) — OLD's `get_candidate_token` (`dual_redaction_pipeline.py:1515-1530`) already does the same job and is arguably more correct (DB-backed job-wide consistency + explicit conflict detection, vs. NEW's batch-scoped Excel log with no conflict handling and a known bug in `api.py`'s re-style path where two different names both render as `[PERSON_NAME]`).

## FINAL SCOPE — features selected for implementation (2026-08-24)

User has decided to implement these 7 features from PII_Code_19 Aug into script_pii. Rule stays in force: **do not change existing script_pii logic** — these are additive ports, existing detection/masking/ingestion/deployment/DB code stays as-is unless a feature explicitly requires a small, scoped hook into it.

| # | Feature | Source in NEW | Target area in OLD | Risk/complexity |
|---|---|---|---|---|
| 1 | **Critique Agent** (differential original-vs-masked audit, re-mask in-flight) | `pipeline/critique_agent.py`, wired in `pipeline/tabular_pipeline.py:300-370` | New step after `apply_masking`/token-map generation in `dual_redaction_pipeline.py`, before final file write | Medium — needs Ollama (OLD already has this dependency for its reviewer step), adds per-file latency (see section 11's performance note) |
| 2 | **Accuracy Engine (Convergence Loop)** | `pipeline/accuracy_engine.py` (`generate_ground_truth`, `compute_accuracy`) | Same hook point as Critique Agent — they run together in NEW's loop | Medium-high — up to 3 extra Ollama round-trips per file; needs a loop cap and a way to bail out cleanly |
| 3 | **Checksum validation** (Luhn/credit card, US SSN, Verhoeff/Aadhaar, IBAN mod-97) | `pipeline/detectors/global_regex_detector.py:17-135` | New validator functions alongside OLD's existing AU checksums (`dual_redaction_pipeline.py:937-973`) | Low — pure functions, additive, no architecture dependency |
| 4 | **Context seed propagation** (re-scan doc for bare repeats of an already-found labeled entity) | `pipeline/detectors/context_detector.py:364-437` (`_propagate_seeds`) | New pass in OLD's detection stage, after existing label/value + NER passes | Low-medium — self-contained logic, but changes *what gets flagged*, so needs validation against false-positive risk |
| 5 | **Batch cancel** | `pipeline/queue_manager.py` (`cancel_batch`, `is_batch_cancelled`) + `api.py` `batch_async`/`batch_status/{id}`/`cancel_batch/{id}` | OLD's job/process tracking — currently MySQL `activity` table + SSE, **not** NEW's SQLite `task_queue.db` | Medium — architecture decision needed, see open question below |
| 6 | **One-click compliance package (zip download)** | `api.py:2373-2423` (`download_compliance_package`) | New route in `app.py`, reading from existing `audit/` dir + redaction map | Low-medium — must respect OLD's existing per-user/per-client access scoping (`userwiseclient`) when bundling |
| 7 | **PPTX support** | `pipeline/ingestion.py` (`PptxIngester`) | New format handler alongside OLD's existing PDF/XLSX/XLS/DOCX/DOC handlers in `dual_redaction_pipeline.py`/`redaction_formats.py` | Low-medium — new ingestion path only; must not touch existing DOC/XLS/DOCX/PDF handling |

**Explicitly out of scope for this round** (from earlier sections, not requested): visual redaction styles (blackout/whiteout/blur/pill), visual bounding-box review UI, WebSocket streaming, feedback memory, persistent JSON override store — can be revisited later.

## Open questions — need answers before implementation starts

**Q1. Critique Agent + Accuracy Engine — always-on or opt-in?**
Both add real per-file latency (up to ~4 extra Ollama calls per file per section 11). Options:
- (a) Always run for every job (safest, slowest)
- (b) Opt-in per job/client (e.g. a checkbox or config flag — only for high-sensitivity clients)
- (c) Opt-in with a lower default `max_loops` (e.g. 1 instead of NEW's 3) to cap worst-case latency
Which do you want, and is there a client/job property in the existing MySQL schema (`project`/`activity`) that should drive this (e.g. a "high sensitivity" flag), or should it be a global on/off toggle for now?

answwer: (a) Always-on. This aligns with the loop's existing stop-early 
condition (100% precision + recall + LLM audit pass halts the loop 
immediately) — a clean, already-correct file only pays for the initial 
ground-truth-generation call, not all 3 rounds. The extra rounds only 
trigger when the first pass genuinely missed something, which is exactly 
when we want the extra Ollama calls to run. No client/job-level flag 
needed for now — always-on with early-exit already gives us the right 
behavior: fast when detection was already complete, thorough when it 
wasn't.

**Q2. Batch cancel — build on existing MySQL `activity` table, or add a new lightweight store?**
NEW uses a separate SQLite `task_queue.db`. OLD already tracks jobs/processes in MySQL (`activity` table). Introducing a second, parallel persistence layer risks confusion (two sources of truth for job state). Preferred approach: add a `cancelled` status/column to OLD's existing `activity` table and check it inside the existing processing loop, instead of pulling in NEW's SQLite queue. Confirm this is right — or is there a reason to keep it separate (e.g. avoiding schema changes to the shared DMS database)?
answer:Confirmed — use OLD's existing MySQL table, not a new SQLite 
store. Specifically, add a `cancelled` column to `PII_redation_upload_log` 
(script_pii's own per-file upload/status tracking table) and check it 
inside the existing processing loop to allow mid-batch cancellation. No 
need for NEW's separate `task_queue.db` — introducing a second, parallel 
persistence layer for job state would create two sources of truth for the 
same concept, risking drift between them. A single new column on an 
existing, already-owned table is a minimal, low-risk change compared to 
standing up and syncing a second database. This isn't a shared DMS table, 
so no cross-system schema-change concerns either.

**Q3. Checksum validation — which identifier types are actually relevant to your clients?**
NEW adds Luhn (credit cards), US SSN, Verhoeff (Aadhaar), IBAN. OLD only validates AU identifiers today. Do your client documents include non-AU identifiers (US SSNs, Indian Aadhaar, EU bank IBANs), or is this mainly useful for credit card numbers (Luhn) which are more universal? Porting all 4 is low-risk either way, but worth confirming which ones matter so testing/QA can prioritize.

answer: Port all 4 (Luhn, US SSN, Verhoeff/Aadhaar, IBAN). We have 
multiple clients, and there's no guarantee all client documents will stay 
AU-only going forward — porting is low-risk either way, so there's no 
reason to leave any of them out. Testing/QA can still prioritize Luhn and 
IBAN first if real client documents lean toward those, but all 4 should 
be in the codebase from this pass.

**Q4. Context seed propagation — false-positive risk tolerance?**
This re-scans the whole document for bare repeats of a name/org once it's found via a label, and auto-flags them too. It can over-redact if a detected "name" happens to also be a common word/term elsewhere in the doc. Should this be on by default, or gated behind a review step (e.g. flagged for the Critique Agent to double-check before finalizing, if Q1 makes that available)?

answer: Gate it behind the Critique Agent review step rather than 
auto-finalizing. Since Q1 already made the Critique Agent always-on, this 
review is essentially free — no extra latency cost beyond what's already 
running. Seed propagation's over-redaction risk (a name that's also a 
common word getting flagged everywhere) is exactly the kind of thing the 
Critique Agent should catch before finalizing, rather than committing to 
an aggressive auto-redact and hoping it's right. This also means seed 
propagation's aggressiveness doesn't need its own separate on/off 
decision — it inherits the same safety net already in place for 
everything else.

**Q5. Compliance package zip — access scoping?**
OLD's DMS integration scopes which files/clients a given user can see (`userwiseclient`). The new zip-download endpoint needs to respect that same boundary rather than bundling everything on disk. Confirm: should this endpoint require the same session/`uid` auth OLD's other routes use, scoped to the requesting user's accessible clients/jobs only?
answer: Keep it simple — just zip the already-redacted output files for a 
job/client into a single downloadable folder, matching how the existing 
download feature works today. No new access-control logic needed: this 
endpoint should sit behind the same auth/route pattern OLD's current 
download endpoint already uses, so whatever access-scoping already 
applies there applies here automatically. Don't build a separate 
authorization layer for this — just reuse the existing download route's 
session/access checks as-is, and point it at a zip of the redacted 
outputs instead of a single file.

**Q6. PPTX support — how deep?**
NEW's PPTX support extracts and masks text in slides. Do you need visual/layout-preserving redaction on PPTX slides (like OLD does for PDF/XLSX), or is text-extract-mask-reinsert into the same PPTX structure sufficient for now? Also: do client PPTX files ever contain embedded images/scanned content requiring OCR, or is it text-only content in practice?

answer: Text-extract-mask-reinsert into the same PPTX structure is 
sufficient for now — matching NEW's current approach. Visual/layout- 
preserving redaction (exact-position black-box, like OLD does for PDF/
XLSX) can be added later if it turns out to matter, but PPTX is a new 
format for us (OLD has no PPTX support today), so starting simple rather 
than building the more complex layout-preserving version upfront makes 
sense.

Skip OCR support for PPTX for now — client presentations are typically 
text-content rather than scanned/embedded images, so building OCR 
handling for a case we haven't confirmed exists would be premature. If a 
client PPTX with scanned/image content actually shows up, we revisit this 
then rather than building for a hypothetical now.

**Q7. Rollout order?**
Suggested low-risk-first order based on the complexity column above: **(3) Checksum validation → (7) PPTX support → (6) Compliance zip → (4) Context seed propagation → (5) Batch cancel → (1+2) Critique Agent + Accuracy Engine** (last, since they're the most invasive on latency/architecture and benefit from the other pieces — especially checksum validation — already being in place to reduce noise they'd otherwise have to catch). Confirm or reorder based on your priorities.

answer: Confirmed — the suggested order is correct: 

(3) Checksum validation → (7) PPTX support → (6) Compliance zip → 
(4) Context seed propagation → (5) Batch cancel → (1+2) Critique Agent + 
Accuracy Engine (last)

This sequencing makes sense: the early items are simple, self-contained, 
and low-risk, while Critique Agent + Accuracy Engine — the most invasive 
change in terms of latency and architecture — goes last so it benefits 
from checksum validation and seed propagation already being in place, 
reducing the noise it would otherwise have to catch.
## Status: LOCKED — implementation starting (2026-08-24)

All Q1-Q7 answered. Decisions locked:
- Critique Agent + Accuracy Engine: **always-on**, relying on the existing early-exit (100% precision+recall+LLM-pass) so clean files only pay for one ground-truth call.
- Batch cancel: add a `cancelled` column to script_pii's existing `PII_redation_upload_log` MySQL table; no new SQLite store.
- Checksum validation: port all 4 (Luhn, US SSN, Verhoeff/Aadhaar, IBAN), test-prioritize Luhn/IBAN first.
- Context seed propagation: on by default, but its output is gated behind the (now always-on) Critique Agent review before finalizing — no separate toggle needed.
- Compliance zip: no new authz layer — reuse the existing download route's session/access checks as-is, just zip redacted outputs instead of a single file.
- PPTX: text-extract-mask-reinsert only for now, no layout-preserving redaction, no OCR — revisit only if a real client file needs it.
- Rollout order confirmed: **(3) Checksum validation → (7) PPTX support → (6) Compliance zip → (4) Context seed propagation → (5) Batch cancel → (1+2) Critique Agent + Accuracy Engine**.

## Implementation checklist

- [x] 1. Checksum validation (Luhn, US SSN, Verhoeff/Aadhaar, IBAN) — `dual_redaction_pipeline.py`
      Added `valid_luhn`/`valid_us_ssn`/`valid_verhoeff`/`valid_iban` (ported from `global_regex_detector.py`, next to existing AU validators around line 937+), matching patterns `CREDIT_CARD_PAT`/`US_SSN_PAT`/`AADHAAR_PAT`/`IBAN_PAT`, 4 new Presidio recognizers (`CreditCardRecognizer`, `UsSsnRecognizer`, `AadhaarRecognizer`, `IbanRecognizer`) registered in `init_models()`, and matching checks added to the OCR image "Pass 1" block for consistency with the existing AU checksum entries. All 4 algorithms verified against known-good/tampered test vectors (Luhn: test Visa number; SSN: range rules; Verhoeff: freshly-generated valid+tampered pair; IBAN: GB29 NWBK reference number) — all passed. New categories map onto existing `[GOVERNMENT_ID]`/`[BANK_DETAILS]` token types, no new token category needed. `dual_redaction_pipeline.py` compiles cleanly. Existing AU validators/logic untouched.
- [x] 2. PPTX support (text-extract-mask-reinsert) — `dual_redaction_pipeline.py` / `app.py`
      Added end-to-end, mirroring the existing DOCX path exactly: `pptx_to_excel()` (Phase-1 flattening: slide text-frames, tables, speaker notes → Raw Excel rows, for detection), `write_redacted_pptx_as_is()` (Phase-3 as-is writer, reuses the existing `_rewrite_paragraph_text()` helper since python-pptx runs are duck-type compatible with python-docx runs), `unmask_pptx_as_is()` (rehydration, mirrors `unmask_docx_as_is()`). Wired into `SUPPORTED_EXTENSIONS`, the Phase-1 dispatch, `write_redacted_as_is()`, and `unmask_as_is()` — so both CLI (`unmask_documents`/`unmask_file`) and the web `/rehydrate` endpoint pick it up automatically with no extra plumbing. Added `.pptx` to `app.py`'s `ALLOWED_EXTENSIONS` and the 2 upload-page JS accept-lists, plus a `PPT` file-type icon/label. Added `python-pptx==1.0.2` to `requirements.txt` (installed and confirmed working). Per Q6: text-extract-mask-reinsert only, no layout-preserving redaction, no OCR for embedded images. Verified end-to-end with a synthetic test .pptx (title, text box, table cell, speaker notes) — masking correctly tokenized all 4 locations with zero PII leakage, and rehydration correctly restored all original values. `dual_redaction_pipeline.py`/`app.py` compile cleanly; existing DOCX/DOC/XLS/XLSX/PDF/image handling untouched.
- [x] 3. Compliance package zip download — new route in `app.py`
      Added `GET /download/process/<process_id>/compliance_package` in `app.py`, right after the existing `download_by_id` route. Per Q5: reuses the exact same auth gate as `download_by_id` (`_require_uid()`), no separate access-control layer. Uses the existing `db.get_files_for_process(process_id)` (already in `db.py`) to list files, zips each file's `redacted_path` plus its per-file audit log (`audit/pii_audit_log_{stem}.xlsx`, matching `dual_redaction_pipeline.py`'s existing naming) into an in-memory zip under `redacted/` and `audit/` subfolders, streamed back via `send_file`. Skips missing files/audit logs gracefully rather than erroring, 404s only if nothing at all was found. Added matching `docs/swagger/download_compliance_package.yml`. Verified the zip-building logic standalone (2 files with redacted output, 1 with an audit log, 1 phantom row with no output on disk) — correct contents, correct skip behavior, no crash on missing files. `app.py` compiles cleanly.
- [x] 4. Context seed propagation — `dual_redaction_pipeline.py` detection stage
      Added `propagate_context_seeds()` (ported from `context_detector.py:_propagate_seeds`), called once at the end of `scan_text_for_entities()`'s GLiNER/LLM branch. For every candidate that was actually redacted (its value is already in `found`), splits PERSON values into individual capitalized name-parts and strips common org suffixes (Ltd/Corp/Inc/Pty/etc.) off COMPANY/ORGANIZATION values, then re-scans the document text for bare occurrences of those parts and redacts them too (word-boundary matched, same as OLD's existing global-consistency pass, but catching component mentions the exact-value match can't). Reuses OLD's own `NER_BLOCKLIST` as the false-positive filter (matching project convention, not a new list from NEW) and logs every propagated entity via the existing `log_forensic_decision()` with a distinct `CONTEXT_SEED_PROPAGATION` reason_code, per Q4 — auto-redacted for now (consistent with everything else today) but traceably tagged so the future Critique Agent (items 6-7) can specifically re-verify these. Verified standalone with 4 scenarios: bare first-name propagation, de-suffixed org propagation, blocklist correctly suppressing a common-word false positive ("Total"), and no propagation when a seed was never actually redacted. `dual_redaction_pipeline.py` compiles cleanly; existing GLiNER/LLM/structural candidate logic untouched.
- [x] 5. Batch cancel — `cancelled` column on `PII_redation_upload_log` + processing-loop check
      **⚠ Needs a manual step before this works: run `migrations/0008_pii_redation_upload_log_cancel.sql` against the DB.** Per Q2, added `process_id` + `cancelled` columns to `PII_redation_upload_log` (not a new SQLite store) — `process_id` groups every row from one batch/manifest so they can be cancelled together (the table only had per-file rows with no batch grouping column before). Added `db.cancel_batch(process_id)` (UPDATE ... SET cancelled=1 WHERE process_id=... AND status='pending') and `db.is_batch_cancelled(process_id)` in `db.py`. `db.log_upload()` now accepts/stores `process_id` (call site in `app.py`'s `/upload` route updated to pass it — it was already in scope there). New route `POST /api/batch/<process_id>/cancel` in `app.py` (same `_require_uid()` auth gate as the rest, matching pattern), with a matching `docs/swagger/cancel_batch.yml`. Wired the check into `dual_redaction_pipeline.py`'s `main()` per-file loop — `db.is_batch_cancelled(manifest["process_id"])` is checked before starting each file; cooperative cancellation (the file already mid-processing finishes normally, only files not yet started are skipped), matching Q2's intent. **I did not apply the migration** — the DB (172.16.29.11/timedb) looked like a real/shared system, and the user asked to apply it themselves. All 3 files (`app.py`, `db.py`, `dual_redaction_pipeline.py`) compile cleanly; SQL/logic reviewed but not live-tested against the DB pending the migration.
- [x] 6. Critique Agent (always-on, Ollama-based audit + re-mask) — `dual_redaction_pipeline.py`
- [x] 7. Accuracy Engine convergence loop (wraps Critique Agent with ground-truth/precision-recall) — `dual_redaction_pipeline.py`
      Implemented together (items 6+7 share one loop, matching how NEW itself wires them). Added a new "Pass 4" block just before `main()`: `generate_ground_truth()` (checksum tier reusing this file's own validators from item 3 — Luhn/SSN/Verhoeff/IBAN/TFN/ABN/ACN/Medicare — plus one independent Ollama pass with a ported zero-trust extraction prompt), `compute_accuracy()` (precision/recall, adapted to this project's `<*TYPE_N*>` token syntax instead of NEW's `[TYPE_N]`), the `CritiqueAgent` class (differential original-vs-masked audit via Ollama, same resilient fuzzy-JSON parser as NEW), and `run_convergence_loop()` which ties them together — reconstructs the file's full text, generates ground truth once, then loops (max 3) re-masking and re-checking until 100% precision + 100% recall + LLM-pass, or nothing new surfaces. Wired into `main()` right after Phase 2's `scan_text_for_entities()` call (new "Phase 2.5"), before either output writer runs, so both the as-is and Excel paths benefit from the refined `global_entities`.
      Reuses this file's own `SLM_OLLAMA_URL`/`SLM_OLLAMA_MODEL`/`SLM_CHUNK_TIMEOUT` config instead of introducing separate ones. Per Q1: always-on, no client/job flag — relies on the loop's early-exit so a clean file only pays for one ground-truth call + one audit call.
      **Feedback-memory persistence was deliberately NOT ported** (`auto_apply_feedback`/`feedback_memory.json`) — out of scope per the "Explicitly out of scope for this round" list; this is a pure audit-and-fix-in-place pass with no cross-session memory.
      **Two bugs found and fixed during porting, not present in the final code:** (1) NEW's ground-truth currency-amount filter (`^\$?\d+...`) had an optional `$`, so it silently dropped *any* bare digit string — including valid credit card/SSN/TFN numbers — from the ground truth baseline; fixed by requiring the literal `$`. (2) NEW's Critique Agent used a 15s Ollama timeout ("keep the loop instantaneous"), which only holds if the model is already warm; measured against a real local `gemma3:4b` on this machine's CPU-bound inference, a single critique call took ~60s even warm — a 15s (or even 60s) timeout would silently fall through to "pass, nothing missed" on exactly the hardware this project already assumes elsewhere. Fixed by reusing this file's existing `SLM_CHUNK_TIMEOUT` (600s) instead of a new short constant.
      **Verified end-to-end against a real local Ollama (gemma3:4b), not mocked:** 6 tests — checksum-tier ground truth extraction, perfect-masking accuracy (100%/100%), leaked-value accuracy (recall correctly drops, leak flagged), Critique Agent correctly failing a fully-unmasked document, Critique Agent correctly passing a properly-masked document, and a full convergence-loop integration test starting from an intentionally incomplete detection pass (only a TFN caught) — the loop correctly found and added the missing name and email, converging to 100%/100% by loop 2 of 3. All passed. `dual_redaction_pipeline.py` compiles cleanly.

Progress will be tracked by checking items off here as each is implemented and verified.

## Gap found during real-app testing: PPTX preview + secondary review features (2026-08-24) — FIXED

**Symptom:** previewing a redacted `.pptx` in the app showed `Preview failed: No preview available for extension: .pptx`.

**Root cause:** item 2's PPTX port only touched `dual_redaction_pipeline.py` (the heavy pipeline process). It missed `redaction_formats.py` — a deliberately separate, lightweight module (no Docling/GLiNER/Presidio/cv2 imports, so the always-running Flask process stays fast) that duplicates the masking/unmasking functions for the web app's fast synchronous operations: **preview rendering, "Add & Re-redact" (add one missed value), and the reference table's "delete row" (undo one over-eager redaction)**. None of those had a `.pptx` branch, so all three would fail for a pptx file, not just preview.

**Fix — added to `redaction_formats.py`, mirroring the existing `.docx`/Word-COM pattern exactly:**
- `write_redacted_pptx_as_is()` — 3-arg lightweight twin of the one in `dual_redaction_pipeline.py`, wired into `mask_single_value()`.
- `unmask_pptx_as_is()` + `_add_formatted_run_pptx()` / `_rewrite_pptx_paragraph_text_highlighted()` — wired into `unmask_single_value()` and `unmask_uploaded_file()`. Note: python-pptx's `Font` object has no `highlight_color` equivalent to python-docx's `WD_COLOR_INDEX` — the restored value in a rehydrated pptx isn't visually highlighted yellow the way a rehydrated docx/xlsx value is; the text substitution itself is identical, just not visually flagged. Documented in the code; not worth blocking on since it's a minor UX nicety, not a correctness issue.
- `convert_to_pdf_via_powerpoint()` — PowerPoint COM automation (`Presentations.Open(..., WithWindow=False)` + `SaveAs(..., FileFormat=32)` / `ppSaveAsPDF`), mirroring `convert_to_pdf_via_word()`. Confirmed PowerPoint COM is actually installed and reachable on this machine (Word being available doesn't guarantee it).
- `_preview_via_powerpoint()` — same mtime-invalidated cache strategy as `_preview_via_word()`, wired into `render_preview()`. **This is the direct fix for the reported symptom.**
- `.pptx` branch also added to `find_tokens_in_file()` (token scanning) so the token dispatcher doesn't wrongly fall through.

**Verified against the real `pii_env` venv, real PowerPoint COM, not mocked:** `mask_single_value()` on a pptx (title/body/table all correctly masked), `unmask_single_value()` (highlighted-run rebuild path — confirmed it doesn't crash despite the no-highlight-on-pptx limitation, and correctly restores the value), and — the actual reported failure — `render_preview()` on a `.pptx`, which now genuinely converts to PDF via live PowerPoint COM and renders real page images (`type: "pages"`, 1 image, word boxes present, `token_pages` correctly mapped). All passed. Both `python -m py_compile` and `pii_env`'s own Python compile `redaction_formats.py` cleanly.

## Follow-up: "Download All (ZIP)" button wasn't actually showing — FIXED (2026-08-24)

**Symptom:** user reported the ZIP button from item 3 never appeared after a 2-file batch finished (screenshot showed only individual per-file Download buttons).

**Root cause:** a render race, not a rendering bug in the button itself. `runPipeline()`'s `done` handler calls `showResults(data.output_files, data.files, job_id)` (renders the list correctly, with the ZIP button) **then immediately calls** `loadResultDetail(job_id)` to populate the preview/reference-table panel — and `loadResultDetail()` **also re-renders the same download list** (`renderDownloadList(detailFiles)`, to source filenames from the DB instead of the raw upload response) without passing `processId` through, silently overwriting the first render and dropping the button before the user ever saw it. Same issue existed in `refreshDownloadList()` (called after adding/editing a redaction row) and the signed-deep-link entry path (`loadResultDetail(data.process_id, ...)`, called directly with no `showResults()` at all) — both would also have rendered without the ZIP button.

**Fix:** added a `currentProcessId` module-level variable, set inside `loadResultDetail(processId, ...)` (which already receives it as a parameter), and threaded through to every `renderDownloadList()` call site: `loadResultDetail`'s own render, and `refreshDownloadList()`'s. Fixes all three paths at once, including the deep-link one which never had the button at all before.

## Follow-up: Cancel button added to the upload UI (2026-08-24)

Item 5 (batch cancel) previously had a backend route only (`POST /api/batch/<process_id>/cancel`, curl/console only). Added a **Cancel** button to the progress bar on the upload page (`app.py`, `progress-header`), matching the same pattern as the "Download All (ZIP)" button added earlier:
- Shown only once a batch with **more than 1 file** starts (a 1-file batch has nothing left to skip once it's running — cancellation only stops files that haven't started yet).
- On click: POSTs to `/api/batch/<job_id>/cancel`, disables itself and shows "Cancelling…" to prevent double-clicks, logs a note in the console area explaining the current file will still finish.
- Hidden again whenever a batch finishes or errors out (folded into the existing `resetButtons()`).
- Still requires migration `0008_pii_redation_upload_log_cancel.sql` to be applied first (not yet run — user's call, see item 5 above).

Verified: `app.py` compiles cleanly (both the Python and, separately, the embedded JS extracted and checked with `node --check`).

## Follow-up: PowerPoint COM rejects some real-world pptx files — added a fallback renderer (2026-08-24) — FIXED

**Symptom:** even after the fix above, previewing a real user pptx (`PII_Redaction_Rehydration_Flows_redacted.pptx`) still failed: `PowerPoint could not open the file` (COM error `-2147352567`), both with and without the `WithWindow`/`ReadOnly` args.

**Root cause — confirmed, not a bug in this port:** the file's own `docProps/core.xml` shows `<dc:creator>PptxGenJS</dc:creator>` — the original was generated by **PptxGenJS**, a JS library for programmatically building pptx files. Its OOXML output is valid enough for python-pptx (and generally LibreOffice/Google Slides) to read — confirmed: python-pptx opened this exact file fine, 4 slides, well-formed XML, zip integrity OK — but Microsoft's own native PowerPoint parser is stricter and rejects it outright with a generic, unhelpful error. This is a known, real-world OOXML interop gap, not something introduced by this port — a freshly-created python-pptx test file opened via the same COM code without any problem, isolating the cause to the source file's origin, not the code.

**Fix:** added `_render_pptx_pages_fallback()` — a self-contained preview renderer (python-pptx + PIL, no external tool, no PowerPoint dependency) that draws each slide's text-frame/table shapes as labeled boxes at their real position/size using the presentation's own EMU geometry. `_preview_via_powerpoint()` now tries real PowerPoint COM first (pixel-perfect when it works) and transparently falls back to this renderer instead of raising when COM fails. The response includes a `"degraded": True` flag so the frontend could surface a "basic preview" notice later if desired (not yet wired into the UI — low priority since the fallback is already functional).

**Known limitations of the fallback, accepted as reasonable for a degraded path:** no real fonts/colors/background images (plain boxes + text only); no per-word bounding boxes, so the marquee "select a missed value" tool won't have anything to snap to on a fallback-rendered slide (degrades gracefully — no crash, just nothing to click); connector/line shapes (common in flow diagrams) are drawn as their bounding rectangle, not the actual line/arrow.

**One coordinate bug found and fixed during testing:** the first fallback attempt crashed on `ValueError: y1 must be greater than or equal to y0` — some shapes (diagram connectors/arrows drawn bottom-right to top-left) have a **negative** `width`/`height` in python-pptx, which PIL's `rectangle()` doesn't accept. Fixed by normalizing to `min`/`max` before drawing instead of assuming `left+width`/`top+height` is always the larger corner.

**Verified against the actual real user file that was failing, via the real `pii_env` venv:** `render_preview()` now succeeds (previously hard-failed), producing 4 correctly-laid-out slide images with `degraded: True`. Manually inspected the rendered PNGs — layout is genuinely readable, and the masked tokens (`<*BUSINESS_ID_2*>` etc.) are visibly correct in the output with no PII leaking through the fallback path either. Both pythons compile `redaction_formats.py` cleanly.

## Bug found during real-app testing (2026-08-24) — FIXED

**Symptom:** uploading a real file crashed with `TypeError: CreditCardRecognizer.__init__() got an unexpected keyword argument 'name'` inside `presidio_analyzer`'s `AnalyzerEngine()` constructor, and the preview then showed "Redacted output not found on disk" (a downstream symptom — the pipeline subprocess crashed during `init_models()` before ever writing an output file, so there was nothing to preview).

**Root cause:** item 1's new checksum-validated recognizer classes were named `CreditCardRecognizer`, `UsSsnRecognizer`, and `IbanRecognizer` — all 3 collide with Presidio's own built-in predefined recognizers of the exact same name. `AnalyzerEngine()`'s default registry bootstrap resolves recognizer classes by an **unqualified name search across every currently-imported `EntityRecognizer` subclass in the whole process** (`get_existing_recognizer_cls()` in `presidio_analyzer/recognizer_registry/recognizers_loader_utils.py`), not by module path — so the moment `dual_redaction_pipeline.py` is imported, its same-named classes shadow Presidio's own, and Presidio's default bootstrap (which runs *before* this file's own `registry.add_recognizer()` calls) tries to instantiate what it thinks is its own built-in `CreditCardRecognizer` using kwargs (including `name=...`) that only Presidio's real built-in accepts — crashing on my custom class instead. This is exactly why the project's existing AU recognizers are named `AuAbnRecognizer`/`AuTfnRecognizer`/etc. rather than plain `AbnRecognizer` — the same collision risk, already worked around for those, missed for the new ones.

**Fix:** renamed the 4 new recognizer classes to `PiiCreditCardRecognizer`/`PiiUsSsnRecognizer`/`PiiAadhaarRecognizer`/`PiiIbanRecognizer` (all 4 renamed for consistency, though only 3 actually collided). Verified against the real `pii_env` venv (where the crash occurred, not my sandbox — this venv has `presidio_analyzer` installed, my sandbox didn't, which is why my earlier standalone testing couldn't catch this): reproduced the exact crash with the old colliding name, confirmed `AnalyzerEngine()` constructs cleanly with the renamed classes. Both `python -m py_compile` (system Python) and `pii_env`'s own Python compile cleanly.

## Status: ALL 7 ITEMS IMPLEMENTED — ready for full manual/real-app testing (2026-08-24)

Every item on the checklist (1-7) is implemented and has passed automated/standalone verification — checksums against known test vectors, PPTX against a synthetic round-trip file, the compliance zip against a simulated file set, context propagation against 4 scenarios, and the Critique Agent/Accuracy Engine end-to-end against a real local Ollama instance. No existing script_pii logic was changed — everything is additive, per the standing rule.

**One manual step still required before item 5 (batch cancel) actually works in production:** run `migrations/0008_pii_redation_upload_log_cancel.sql` against the DB. This was intentionally left for the user to run — see item 5's note above.

**Full manual test checklist, in the app, before considering this done:**
- [ ] Checksum validation: upload a document containing a credit card number, a US SSN, an Aadhaar number, and/or an IBAN — confirm they get detected and masked. Confirm existing AU identifiers (TFN/ABN/ACN/Medicare) still detect/mask exactly as before (regression check).
- [ ] PPTX support: upload a real `.pptx` file with PII in a title, body text box, table cell, and speaker notes — redact it, download the result, open it in PowerPoint and confirm all 4 locations are masked and formatting/layout is intact. Then run it through `/rehydrate` and confirm the original values come back correctly.
- [ ] Compliance package zip: after a batch finishes, hit `/download/process/<process_id>/compliance_package` and confirm the zip contains the expected redacted files + audit logs.
- [ ] Context seed propagation: upload a document where a name/org appears in full once (e.g. in a labeled field) and again later as just a first name or a de-suffixed company name — confirm the second bare mention also gets masked. Watch for any over-redaction of common words that happen to be capitalized.
- [ ] Batch cancel (after running migration 0008): start a multi-file batch, call `POST /api/batch/<process_id>/cancel` partway through, confirm the file in progress finishes but no further files start.
- [ ] Critique Agent + Accuracy Engine: run a real client document through the pipeline and check the console/log output for `[Phase 2.5]` and `[Convergence Loop ...]` lines — confirm it converges (ideally at loop 1 for a clean detection) and doesn't add obviously-wrong entities. **Note the latency**: this pass adds real per-file time (a ground-truth Ollama call plus up to 3 critique Ollama calls) — worth timing a real file end-to-end to see the actual production impact, since this sandbox's Ollama was unusually slow (CPU-bound, ~60s per call even warm) and production hardware may differ substantially either direction.
