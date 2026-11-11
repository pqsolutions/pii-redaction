# Plan: Output Preview + Redaction Reference Table + Manual "Add Missing Value" Flow

Status: **DRAFT — Section 5 (original design) is answered; Section 5b (preview) is new and needs answers**
Owner: TBC
Created: 2026-07-09
Updated: 2026-07-09 — added in-browser **file preview** requirement (Section 3b, 4b, 5b) on top of the already-answered reference-table + manual-add design below. Nothing in Sections 1–7 (original) was changed except where explicitly marked "(updated)".

---

## 1. Goal

Today, after clicking **Run Redaction**, the user only sees a list of output files with
a Download link ([app.py:587-609](app.py#L587-L609)). The token↔value mapping
(`"John Smith" → <*PERSON_1*>`) exists but is invisible in the UI — it only lives in
MySQL (`pii_redaction_map`) or a local `secrets/*.xlsx` file (see
[wiki/architecture.md](../../architecture.md), "DB-backed Redaction Map & Rehydration").

We want, on the same results screen, after a run finishes:

1. **A preview of the redacted output file itself** — rendered inline in the browser
   (not just a filename + Download link), so the user can visually inspect the actual
   redacted content before downloading. *(new requirement, this update)*
2. **A reference table** shown alongside the preview, listing every
   `Original Value → Masked Token` pair the pipeline produced for that file — e.g.

   | Original Value | Masked As |
   |---|---|
   | Shibu | `<*NAME_1*>` |
   | 0412 345 678 | `<*PHONE_1*>` |

3. **A "missing value" add flow**: while looking at the preview + table, if the user
   spots a PII value the pipeline *didn't* catch (e.g. a phone number), they type it
   into a new row, pick/type a category, click **Add**, and the app re-redacts —
   producing an updated output file where that value is now masked too. The table gets
   a new row, the preview refreshes to show the newly-masked value, and the file is
   available to download or preview again — repeatable, so the user can keep adding
   missed values until satisfied.

This plan reuses the same `pii_redaction_map` store used for rehydration, so every
manually-added value round-trips through `/rehydrate` like every other redacted value.

---

## 2. Current State (what exists today)

### 2.1 How a value becomes a token today

- `scan_text_for_entities()` ([dual_redaction_pipeline.py:1868](dual_redaction_pipeline.py#L1868), Phase 2 entry point) finds candidate PII via Tier 0 regex / Tier 1 structural labels / Tier 2 GLiNER / Tier 3 Ollama, and builds `global_entities` (`{value_lower: token}`).
- `get_candidate_token()` ([dual_redaction_pipeline.py:1516](dual_redaction_pipeline.py#L1516)) mints the token string itself: `<*{CATEGORY}_{N}*>`, e.g. `<*PERSON_1*>`, `<*EMAIL_1*>`. `N` is a per-category counter (`candidate_counters` dict) that only exists **in memory for the duration of one pipeline run** — it is never persisted or reloaded.
- `apply_masking()` ([dual_redaction_pipeline.py:2061](dual_redaction_pipeline.py#L2061)) does the actual case-insensitive string substitution of every `global_entities` value against a cell/string, and returns audit events.
- At the end of processing one file, `secret_map` (all `{value_lower: {candidate, original_value}}` pairs collected for that file) is flushed to MySQL: [dual_redaction_pipeline.py:3856-3866](dual_redaction_pipeline.py#L3856-L3866) calls `db.insert_redaction_rows(client_id, job_id, process_id, file_id, pairs)` ([db.py:308](db.py#L308)) — one row per unique value into `pii_redaction_map (source_text, redacted_text, ...)`.
- This only happens for **manifest-driven web uploads** (`manifest is not None`). Ad-hoc CLI runs with no `--manifest` fall back to `secrets/pii_secret_log_{stem}.xlsx` instead — out of scope for this plan, which targets the web UI flow.

### 2.2 Resume-safe skip logic works against a cheap "re-run to add one value" flow

In `main()`:

- Phase 1 (extraction) is skipped if `{stem}_raw.xlsx` already exists ([dual_redaction_pipeline.py:3749](dual_redaction_pipeline.py#L3749)).
- Phase 2 (masking) is skipped entirely if **any** `{stem}_redacted.*` file already exists ([dual_redaction_pipeline.py:3776](dual_redaction_pipeline.py#L3776)) — it does not reprocess, it just moves on.
- But `app.py`'s `_run_pipeline()` **deletes** both the raw and redacted output for every uploaded filename *before* invoking the subprocess ([app.py:850-862](app.py#L850-L862)), specifically so the web UI's "Run Redaction" always regenerates from scratch.

Net effect: today, clicking **Run Redaction** again always re-does Phase 1 (Docling/OCR)
**and** Phase 2 (GLiNER + Ollama) from zero. That's the expensive full pipeline — minutes
per file, not something you want to trigger just to add one manually-typed value to an
already-correct redaction. This is the central design fork resolved in Section 3 below.

### 2.3 Frontend

Single inlined HTML/JS/CSS in `app.py` (`HTML` string, no template files, no build step):

- Results list rendering — `showResults()`: [app.py:587-609](app.py#L587-L609). Renders filename + Download link per output file, nothing about the redaction map, nothing rendered inline.
- SSE `done` handler that calls it: [app.py:569-578](app.py#L569-L578) — receives `{success, output_files}` only; no token/value data or preview content comes back from the backend today.
- No existing UI shows plaintext original values anywhere except `/rehydrate`, which is a **separate, explicitly gated page** ([app.py:963-1012](app.py#L963-L1012)) requiring `session["uid"]` to be set (i.e. opened via the signed encrypted link). This is the precedent to follow for any new UI that displays plaintext PII.
- **No in-browser preview of any output format exists today** — `/download/<filename>` ([app.py:955-960](app.py#L955-L960)) always streams the file as a download/blob; nothing renders file contents inline in the page. *(new note, this update)*

### 2.4 Database

`pii_redaction_map` schema ([wiki/architecture.md:280-288](../../architecture.md#L280-L288)):

```sql
CREATE TABLE pii_redaction_map (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    client_id     INT NOT NULL,
    job_id        INT NOT NULL,
    process_id    VARCHAR(20) NOT NULL,
    file_id       VARCHAR(20) NOT NULL,
    source_text   TEXT NOT NULL,
    redacted_text TEXT NOT NULL
);
```

- `db.insert_redaction_rows()` ([db.py:308](db.py#L308)) is a bulk `INSERT`, always called once at the end of a run. There's no "add one more row later" helper today, and no uniqueness constraint on `(file_id, source_text)` — calling it twice for the same value would duplicate the row.
- `db.get_redaction_map(file_id=...)` ([db.py:326](db.py#L326)) already returns `{redacted_text: source_text}` for one file — this is reusable as-is for populating the reference table.

### 2.5 Token category vocabulary

Categories in play today (drives what a "pick a category" input should offer):
Tier 0 regex — `CREDIT_CARD, TFN, ABN, EMAIL, PHONE, MEDICARE, BSB_ACCOUNT, ACCOUNT_NUMBER, REF_NUMBER` ([pii_rules.yaml:86-95](../../pii_rules.yaml#L86-L95)); Tier 1 structural labels — `PERSON, DATE, ADDRESS, COMPANY, REF_NUMBER, ACCOUNT_NUMBER, MEDICARE` ([pii_rules.yaml:42-84](../../pii_rules.yaml#L42-L84)); GLiNER (Tier 2) can also emit categories like `ORGANIZATION`, `LOCATION` dynamically based on its own model output, not just this static list.

### 2.6 Output format variety (drives preview design — new, this update)

The pipeline supports two output modes per `pii_files.output_format` ([migrations/0002_pii_output_format.sql](../../../migrations/0002_pii_output_format.sql)), documented in [plan-as-it-is-implementation.md](plan-as-it-is-implementation.md):

- **`excel`** — everything flattened into a redacted `.xlsx` workbook, regardless of source type.
- **`as_is`** — format-preserving output, one writer per source type: `write_redacted_xlsx_as_is` (dual_redaction_pipeline.py:3211), `write_redacted_xls_as_is` (3237), `write_redacted_docx_as_is` (3273), `write_redacted_pdf_as_is` (3427, visual PyMuPDF box-redaction), `write_redacted_image_as_is` (2949).

A single "preview" implementation has to handle at least: `.xlsx`/`.xls` (tabular), `.docx` (flowed text/tables), `.pdf` (paginated, already an image-redacted visual document), and images (`.png`/`.jpg`, already pixel-redacted). See Section 3b for the per-format rendering approach.

---

## 3. Design Fork: how does "Add & Re-mask" actually re-mask the file?

**Resolved — Option B (targeted re-mask), confirmed by answer to Open Question #1 below.**

**Option A — Full pipeline re-run (rejected).**
Reuse "Run Redaction" verbatim, force-inject the manual value into `secret_map` before
Phase 2, let Docling/GLiNER/Ollama reprocess from scratch. Rejected: slow (full OCR/NER/LLM
pass) for a one-value correction — bad UX for what should feel like a quick edit.

**Option B — Targeted re-mask (chosen).**
A new, cheap endpoint that:
1. Loads the **existing** Raw Excel (`files/output/raw/{stem}_raw.xlsx`, Phase 1 output — retained per answer to Open Question #3) and the **existing** redacted output file.
2. Reconstructs just enough state to mint a token consistent with what's already there — query `pii_redaction_map` for this `file_id`, parse existing tokens (e.g. `<*PERSON_2*>`) to find the next free counter per category, so a new `PERSON` addition becomes `<*PERSON_3*>` rather than colliding with `<*PERSON_1*>`.
3. Runs `apply_masking()`-equivalent logic against the redacted output using just this one new `{value: token}` pair (skip re-scanning everything else — it's already redacted).
4. Overwrites the redacted output file in place, `INSERT`s the one new row into `pii_redaction_map`, and returns the updated table (and, per this update, a refreshed preview) to the frontend.

Per answer to Open Question #2, this must work for **all** output formats (`excel` and every `as_is` writer), not just flattened Excel — meaning step 3 needs a per-format targeted-remask routine (string replace for Excel cells / docx runs / xlsx-native cells; a new visual box-redaction pass over the specific bounding box for PDF and image `as_is` outputs, reusing the box-redaction logic already in `write_redacted_pdf_as_is` / `write_redacted_image_as_is`, but scoped to just the newly-found occurrences of the one added value rather than a full re-scan).

---

## 3b. Design: In-Browser Preview (new, this update)

Per-format rendering approach — goal is "good enough to visually confirm redaction", not
pixel-perfect fidelity to the original document, and to reuse existing dependencies
(`requirements.txt` already has `pymupdf`, `openpyxl`, `xlrd`, `python-docx`, `pillow` —
**no new Python dependencies needed**):

| Output type | Preview approach | Library (already in `requirements.txt`) |
|---|---|---|
| `.xlsx` (excel mode, or `as_is` xlsx) | Read workbook, render first sheet (or all sheets, tabbed) as an HTML `<table>` | `openpyxl` |
| `.xls` (`as_is`) | Same, HTML `<table>` | `xlrd` |
| `.docx` (`as_is`) | Walk paragraphs + tables, render as simple structured HTML (headings/paragraphs/table rows) — not full visual fidelity, but shows redacted text in place | `python-docx` |
| `.pdf` (`as_is`) | Render each page to a PNG via `page.get_pixmap()`, return as `data:image/png;base64,...` per page, paginated viewer | `pymupdf` (already used for PDF box-redaction) |
| `.png`/`.jpg` (`as_is`) | Direct `<img>` — already pixel-redacted, no processing needed | `pillow` (already used for image redaction) |

Backend: **`GET /api/preview/<file_id>`** (auth-gated like `/rehydrate`, see Open Question
#8-preview) — dispatches on `pii_files.output_format` + file extension, returns
`{type: "table"|"html"|"pages"|"image", content: ...}`. Frontend renders based on `type`.

Frontend: a preview pane (left/top) next to or above the reference table (right/bottom) in
the same results section — see Section 4b.

Large-file handling: PDFs with many pages / huge spreadsheets need a cap (see Open
Question #12-preview).

---

## 4. Proposed Architecture (assuming Option B)

### 4.1 Backend

- **`GET /api/redaction-map/<process_id>`** — new endpoint, gated behind `session["uid"]` like `/rehydrate` ([app.py:970](app.py#L970)). Returns, per file in that batch: `file_id`, `filename`, and a list of `{source_text, redacted_text}` pairs (via `db.get_redaction_map(file_id=...)` per file, or one call per `process_id` plus grouping).
- **`POST /api/redaction-map/add`** — new endpoint, same auth gate. Body: `{file_id, original_value, category}`.
  - `category` is a free-text field, per answer to Open Question #9 (no fixed allowlist) — token becomes `<*{CATEGORY_UPPER}_{N}*>` from whatever the user types (e.g. typing "client name" → `<*CLIENT_NAME_1*>`).
  - Looks up `pii_files` row for `file_id` → `redacted_path`, `raw_excel_path` (new column, see Database section below), `client_id`, `job_id`, `process_id`, `output_format`.
  - Rejects duplicates per answer to Open Question #5: if `original_value` (case-insensitive) already exists in `pii_redaction_map` for this `file_id`, return an error `"Already masked as <*PERSON_1*>"` instead of inserting.
  - Mints the next token for `category` (per the counter-reconstruction logic in Section 3, Option B step 2).
  - Dispatches to the correct targeted-remask routine based on `output_format` (Excel cell replace, docx run replace, or PDF/image box-redaction pass), overwrites the redacted output in place.
  - Calls a new `db.insert_redaction_row()` (singular) to append one row to `pii_redaction_map`.
  - Returns the updated pair list *and* a freshly-rendered preview payload for that file.
- **`PUT /api/redaction-map/<row_id>`** / **`DELETE /api/redaction-map/<row_id>`** — supports edit/undo of manually-added rows, per answer to Open Question #6. Only allowed on rows with `source = 'manual'` (see Database section) — pipeline-generated rows stay append-only/read-only, matching the "Out of Scope" note in Section 7.
- New `db.py` helpers: `insert_redaction_row(...)` (single-row insert, with `source='manual'`), `update_redaction_row(...)`, `delete_redaction_row(...)`, and reuse of `get_redaction_map()`/token-parsing to compute the next free counter (no new SQL needed for the counter — parse existing tokens in Python).

### 4.2 Frontend (`app.py`'s inlined `HTML`)

- Extend the `done` SSE handler ([app.py:569-578](app.py#L569-L578)) — after `showResults()`, also call a new `loadResultDetail(process_id)` that hits both `GET /api/preview/<file_id>` and `GET /api/redaction-map/<process_id>`, rendering a combined section below Results **per file**:
  - **Preview pane** (Section 4b) showing the redacted output inline.
  - **Reference table** below/beside it: columns **Original Value | Masked As | (Edit/Delete for manual rows)**.
  - **"Add missing value" row** at the bottom: a text input (original value) + a free-text category input + an **Add & Re-redact** button.
- On **Add & Re-redact** click: `POST /api/redaction-map/add`, then re-render that file's table rows *and* preview pane from the response, and refresh the Download link (the underlying output changed). Loop is repeatable — user can keep adding values.
- On **Edit/Delete** click (manual rows only): `PUT`/`DELETE /api/redaction-map/<row_id>`, same refresh pattern.
- Styling reuses the existing dark theme tokens already defined in `HTML`'s `<style>` (`--surface`, `--surface2`, `--border`, `.badge`, etc.) — no new design system needed; preview pane + table are additive `.result-card`-style blocks.
- **Auth-gate UX (fixed 2026-07-09):** all four new endpoints require `session["uid"]`, same as `/rehydrate` (Open Question #8). Originally a failed fetch (403, or any other non-2xx) just silently hid the whole preview+table section — indistinguishable from a real bug. Fixed: `loadResultDetail()` now shows an explicit message in that spot instead — "Reference table & preview are only shown when you open this app via your normal signed-in link" for a 403, or the server's own error text for anything else — so an unauthenticated session reads as an explained state, not a blank screen.

#### "Add missing value" form field reference

The form has two inputs — they answer different questions and are both required:

| Field | Answers | Example |
|---|---|---|
| **Missed value** | *What text, exactly as it appears in the document, should be found and masked?* Matched case-insensitively as a substring, same rule `apply_masking()` already uses. | `61,871.62` (matches inside `$61,871.62` too) |
| **Category** | *What should the replacement token be called?* Free text (Open Question #9) — sanitized/uppercased and combined with a per-file counter into `<*CATEGORY_N*>`. Doesn't need to match anything already in the document. | `BALANCE` → produces `<*BALANCE_1*>` |

Worked example against a real "as is" PDF preview (superannuation statement) where "Balance $61,871.62" wasn't caught by the pipeline: Missed value = `61,871.62`, Category = `BALANCE` → the document gets `<*BALANCE_1*>` burned in wherever that value appears, and the table gets a new manual row. Typing a value that's already masked returns "Already masked as `<*...*>`" (Open Question #5) instead of creating a duplicate token; typing a value that doesn't appear anywhere in the document returns "Value not found" instead of silently doing nothing.

### 4.3 Data flow diagram (updated)

```
Run Redaction (existing)
    │
    ▼
Redacted output + Raw Excel retained + pii_redaction_map rows (existing, unchanged)
    │
    ▼
GET /api/preview/<file_id>          ──►  Preview pane rendered in browser
GET /api/redaction-map/<process_id> ──►  Reference table rendered in browser
    │
    │  user types "0412 345 678" + types "PHONE", clicks "Add & Re-redact"
    ▼
POST /api/redaction-map/add {file_id, original_value: "0412 345 678", category: "PHONE"}
    │
    ├─ dedupe check against pii_redaction_map (Open Question #5)
    ├─ mint <*PHONE_N*> (N = next free counter for this file_id + category)
    ├─ targeted re-mask: format-specific routine overwrites redacted output in place
    ├─ db.insert_redaction_row(client_id, job_id, process_id, file_id, "0412 345 678", "<*PHONE_N*>", source='manual')
    ▼
Updated table row + refreshed preview + refreshed Download link
    │
    │  (user can repeat: preview again, download, or add another value)
```

---

## 5. Open Questions — original set (ANSWERED)

1. **Option A vs Option B (§3)** — full pipeline re-run vs. targeted re-mask?
   **Answer: Option B — Targeted re-mask.**
2. **Scope: Excel output only, or "as is" (PDF/DOCX/XLSX-native) too?**
   **Answer: must work for all formats.**
3. **Raw Excel retention** — does `files/output/raw/{stem}_raw.xlsx` need to be durably retained (e.g. a new `pii_files` column) so "Add & Re-mask" works even after the session/page is gone?
   **Answer: yes, need to save it.**
4. **Multiple occurrences / case variants** — exact match only, or same relaxed case-insensitive substring matching as `apply_masking()`?
   **Answer: same relaxed matching.**
5. **Duplicate / conflicting adds** — error out, or silently create a second row/token?
   **Answer: show an error message ("Already in the table").**
6. **Undo** — append-only, or can the user edit/remove a manually-added row?
   **Answer: yes, allow the user to edit.**
7. **Batch scope** — does adding a value in File A's table propagate to other files in the same batch?
   **Answer: only affects File A (per-file only).**
8. **Security/exposure** — is matching `/rehydrate`'s existing gate (`session["uid"]`) sufficient for showing plaintext PII in this table?
   **Answer: yes, show the plaintext on screen (same gate as `/rehydrate`).**
9. **Token category for manual adds** — fixed dropdown, or free text?
   **Answer: free-text box.**
10. **Rehydration correctness** — do we need an explicit regression test that a manually-added row still rehydrates correctly via `/rehydrate`?
    **Answer: yes, need an explicit test case.**

---

## 5b. Open Questions — preview feature (NEW, needs your answers)

11. **Preview scope per session** — should the preview + table load automatically right after a run finishes (as part of the existing `done` SSE flow), or only when the user clicks a new "Preview" button per file (lazy-load, saves rendering cost for files the user doesn't inspect)?
answer: Auto-load

12. **Large document caps** — for a multi-page PDF or a spreadsheet with thousands of rows, do we cap the preview (e.g. first 20 pages / first 200 rows with a "truncated" notice), or must the full document always render? This affects response size and render time.
answer:load full document render

13. **DOCX preview fidelity** — the planned approach (Section 3b) renders `.docx` as simplified structured HTML (paragraphs/tables/headings), not a pixel-accurate visual match to Word. Is that acceptable, or is exact visual fidelity required (which would need a much heavier approach — e.g. converting to PDF via a LibreOffice headless dependency we don't currently have)?
answer: i need it as Pixel-perfect

14. **Preview after "Add & Re-redact"** — should the preview pane auto-scroll/highlight the newly-masked value's location so the user can immediately confirm it worked, or is it enough that the value shows as masked somewhere in the refreshed preview?
answer: jsut refresh  

15. **Multi-file batches** — if a job has multiple output files, do we show one preview+table+add-form block per file stacked vertically, or a file-switcher (tabs/dropdown) with one active preview+table at a time? (Affects layout more than logic.)
answer: Tabs/file-switcher

16. **Access to preview outside the just-finished run** — like the reference table (Open Question #3), should the preview also remain viewable later (e.g. from a "past jobs" list), or is it strictly tied to the page/session right after a run? This affects whether we need a job-history UI at all, which doesn't exist today.
answer: for current session only 

---

## 6. Database changes needed

**Implementation update (2026-07-09):** while implementing Section 3's targeted re-mask,
re-reading `write_redacted_xlsx_as_is`/`write_redacted_pdf_as_is`/etc. and their
`unmask_*_as_is` counterparts ([dual_redaction_pipeline.py:3211-3647](dual_redaction_pipeline.py#L3211-L3647))
showed the Raw Excel isn't actually needed: the **currently redacted output file itself**
still contains real plaintext for any value the pipeline didn't catch (only matched
values got replaced with tokens). So a manual add can search-and-replace directly against
the current redacted output — same technique `unmask_xlsx_as_is` already uses in the
token→original direction. This drops the `raw_excel_path` migration from the plan (Open
Question #3 was answered "yes, need to save" under the original framing that Option B
required reloading Raw Excel — that premise turned out to be unnecessary once the actual
writer/unmasker functions were read closely). Only one migration is needed:

- **`migrations/0004_pii_redaction_map_source.sql`** — `ALTER TABLE pii_redaction_map ADD COLUMN source ENUM('pipeline','manual') NOT NULL DEFAULT 'pipeline' AFTER redacted_text;` — needed to distinguish pipeline-generated rows (read-only, per Section 7 "Out of Scope") from manually-added rows (editable/deletable, per answer to Open Question #6).

### 6.1 New shared module: `redaction_formats.py`

`dual_redaction_pipeline.py` eagerly imports Docling, GLiNER, Presidio, and `cv2` at
module load ([dual_redaction_pipeline.py:8-36](dual_redaction_pipeline.py#L8-L36)) — importing
it directly into `app.py` would load all of that (multi-second, ~GB-scale) into the
always-running Flask process just to reuse a handful of pure-format functions, which
defeats the entire point of choosing Option B (fast, no heavy pipeline re-run) and directly
contradicts the "auto-load" answer to Open Question #11 (preview needs to feel instant).

So the per-format mask/unmask writer functions (`write_redacted_xlsx_as_is`,
`write_redacted_docx_as_is`, `write_redacted_pdf_as_is`, `write_redacted_image_as_is`,
their `unmask_*_as_is` counterparts, and their shared helpers like `_pdf_page_words`/
`_find_spans_in_line`/`_image_words`/`_rewrite_paragraph_text`) are duplicated — not
moved — into a new `redaction_formats.py` with only lightweight imports (`openpyxl`,
`xlrd`/`xlwt`, `python-docx`, `pymupdf`, `PIL`, `pywin32` for Word COM). `app.py` imports
this module directly (fast, synchronous, in-process). `dual_redaction_pipeline.py` is left
completely unmodified to avoid any regression risk to the existing working pipeline —
intentional duplication, not an oversight.

### 6.2 DOCX pixel-perfect preview (Open Question #13 answer: pixel-perfect required)

`convert_doc_to_docx()` ([dual_redaction_pipeline.py:3025](dual_redaction_pipeline.py#L3025))
already proves Word COM automation (`pywin32`, already a dependency) works in this
environment. `redaction_formats.py` adds a `convert_to_pdf_via_word(input_path,
output_pdf_path)` using the same pattern (`Documents.Open` → `SaveAs2(FileFormat=17)` /
wdFormatPDF) for both `.doc` and `.docx`, then reuses the same PyMuPDF page-to-PNG
rendering as the native PDF preview path — genuinely pixel-perfect since it's driven by
the same rendering engine Word itself uses to produce the PDF. Requires Microsoft Word
installed on the host (same requirement `convert_doc_to_docx`/`convert_docx_to_doc`
already have).

---

## 7. Task List

- [x] Get answers to Open Questions §5b (preview-specific — #11 through #16)
- [x] `migrations/0004_pii_redaction_map_source.sql` — add `source` column to `pii_redaction_map` (written; **not yet applied** to the dev DB — see Section 9)
- [x] `db.py`: add `insert_redaction_row()` (single-row, `source='manual'`), `delete_redaction_row()`, `get_files_for_process()`, `get_redaction_rows()`, `get_redaction_row()`, and token-counter reconstruction helper (`redaction_formats.next_token_for_category()`) — no separate `update_redaction_row()`: edit is implemented as restore-old + mask-new against the same row id, see `PUT /api/redaction-map/<row_id>` below
- [x] Backend: `GET /api/redaction-map/<process_id>` (auth-gated, returns per-file rows incl. `source`)
- [x] Backend: `POST /api/redaction-map/add` (auth-gated, dedupe check, mints token, targeted re-mask per format, persists row)
- [x] Backend: `PUT /api/redaction-map/<row_id>` and `DELETE /api/redaction-map/<row_id>` (manual rows only; images regenerated from the encrypted original since pixel redaction is destructive)
- [x] Backend: `GET /api/preview/<file_id>` (auth-gated, per-format rendering per Section 3b)
- [x] New `redaction_formats.py`: per-format mask/unmask/preview helpers, dispatched by file extension (Section 6.1) — replaces the "extract from pipeline" task since duplication (not extraction) was the safer call, see Section 6.1
- [x] Frontend: preview pane rendering (table / paginated page images / single image) after `done` SSE event, auto-loaded per Open Question #11
- [x] Frontend: reference table rendering alongside preview, with a file tab-switcher for multi-file batches (Open Question #15)
- [x] Frontend: "Add missing value" form (text input + free-text category input + submit) per file
- [x] Frontend: edit/delete controls on manually-added rows (edit pre-fills the form; delete asks for confirmation)
- [x] Frontend: refresh preview + table + Download link in place after add/edit/delete, without full page reload
- [x] Sanity-checked: `py_compile`/AST parse clean on `app.py`/`db.py`/`redaction_formats.py`; all 6 new routes register correctly; unauthenticated requests correctly get 403 (matching `/rehydrate`'s gate); live DB smoke test against the dev DB caught and fixed a real bug (`get_files_for_process()` ordered by a nonexistent `pii_files.id` column — fixed to `created_at`)
- [ ] **Apply `migrations/0004_pii_redaction_map_source.sql` to the dev DB** (172.16.29.11/timedb) — needed before the `source`-column-dependent endpoints (add/edit/delete) will work; not run yet since it's a shared DB, not local — see Section 9
- [ ] End-to-end test against a real upload once Word/Tesseract/MySQL are all reachable in one environment: manual add on a fresh run — table shows new pair, preview reflects the mask, downloaded file has the value masked
- [ ] Test: manual add works for each output format (`excel`, and each `as_is` type: xlsx, xls, docx, pdf, image)
- [ ] Test: `/rehydrate` on a file with a manually-added row restores the original value correctly
- [ ] Test: duplicate add attempt shows the error per Open Question #5
- [ ] Test: edit/delete of a manual row round-trips correctly; pipeline-generated rows cannot be edited/deleted
- [ ] Update [wiki/architecture.md](../../architecture.md) once implemented (new endpoints, new DB column/migration, updated data-flow diagram)

---

## 8. Implementation status (2026-07-09)

Code for the full flow is written: `migrations/0004_pii_redaction_map_source.sql`,
`db.py` additions, `redaction_formats.py` (new), and `app.py` (new imports, 5 new
routes, extended frontend). Verified without touching production data: Python
syntax/AST clean, `redaction_formats.py` imports cleanly standalone, all new Flask
routes register, and a live smoke test against the app's own dev DB (`172.16.29.11
/timedb`, from `.env`) confirmed the auth gate and DB round-trip both work — that
smoke test is what caught the `get_files_for_process()` bug fixed above.

**Migration 0004 has been applied** to the dev DB (`pii_redaction_map.source` confirmed
present) — applied by the user directly, not by the assistant, per their choice when asked.

**Confirmed working against a real run:** the user ran an actual redaction
(`Super_Fund.pdf`, `PRC-AFF-000034`, `as_is` PDF output) and hit the auth-gate issue below;
after the fix, `GET /api/redaction-map/<process_id>` and `GET /api/preview/<file_id>`
were re-verified against that same real process/file and returned the correct 4
pipeline-generated rows (`<*ABN_1*>`, `<*TFN_1*>`, `<*TFN_2*>`, `<*PERSON_1*>`) and a
rendered PDF page preview.

**Fixed:** the reference table/preview silently showed nothing when `session["uid"]`
wasn't set (i.e. opening the app by URL instead of through the signed `?uid=...` link) —
see the "Auth-gate UX" note in Section 4.2. The endpoints were always correct; only the
frontend's handling of a 403 was misleading.

**Still not verified end-to-end:** an actual "Add missing value" click-through (`POST
/api/redaction-map/add`) producing a re-masked output file, and `.doc`/`.docx`
mask/preview via Word COM automation — those need a live click-through in the browser,
which hasn't happened yet in this conversation.

---

## 9. Out of Scope (for this plan)

- Ad-hoc CLI runs with no `--manifest` (they use `secrets/*.xlsx`, not `pii_redaction_map`, and have no `file_id`/`process_id` to key a table or preview on).
- Editing or deleting *pipeline-generated* (`source='pipeline'`) rows — this plan only allows edit/delete on manually-added (`source='manual'`) rows.
- Changing how Tier 0–3 detection works — this is purely an additive manual-correction + visibility layer on top of existing output.
- Pixel-perfect DOCX preview fidelity (see Open Question #13) unless answered otherwise.
- A persistent "past jobs" browsing UI (see Open Question #16) unless answered otherwise — today's app has no job-history list at all, only the just-finished run's results.
