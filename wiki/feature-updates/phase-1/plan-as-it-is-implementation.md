# Plan: "Output Format" Selector — As Is vs Excel

Status: **DRAFT — open questions below need answers before implementation starts**
Owner: TBC
Created: 2026-07-08

---

## 1. Goal

Today, every upload (PDF / XLSX / XLS / DOCX / DOC) is redacted and always downloaded
back as a **flattened `.xlsx`** file, regardless of what format the user uploaded.

We want to add an **output format dropdown** to the upload UI with two choices:

| Option | Behaviour |
|--------|-----------|
| **Excel** | Unchanged — today's behaviour. Redacted output is a flattened `.xlsx` (current default, already working). |
| **As is** | New. The redacted output is downloaded in **the same file format the user uploaded** (PDF in → redacted PDF out, DOCX in → redacted DOCX out, XLSX/XLS in → redacted XLSX/XLS out with original structure/formatting preserved, not flattened). |

---

## 2. Current State (what exists today)

### 2.1 Pipeline is fundamentally "convert everything to Excel first"

Every input format is flattened into an intermediate **"Raw Excel"** (headerless
`DataFrame`), then redacted, then saved as **Redacted Excel** — always `.xlsx`,
no matter the source format:

- [dual_redaction_pipeline.py:2372](dual_redaction_pipeline.py#L2372) `pdf_to_excel_docling()` — PDF → flattened rows/cols
- [dual_redaction_pipeline.py:2800](dual_redaction_pipeline.py#L2800) `docx_to_excel()` — DOCX → flattened rows (paragraphs become single-cell rows, tables keep columns; **all formatting, images, headers/footers layout is discarded**, only text order is kept)
- [dual_redaction_pipeline.py:2769](dual_redaction_pipeline.py#L2769) `spreadsheet_to_excel()` — XLSX/XLS → re-read and re-saved as a single flattened sheet (**multi-sheet structure and cell formatting is discarded**)
- [dual_redaction_pipeline.py:2734](dual_redaction_pipeline.py#L2734) `convert_doc_to_docx()` — legacy `.doc` → `.docx` via Word COM automation first, then goes through `docx_to_excel()`
- [dual_redaction_pipeline.py:2915-3011](dual_redaction_pipeline.py#L2915-L3011) `main()` — Phase 1 branches by extension into the functions above; Phase 2 always writes `{stem}_redacted.xlsx` via `df_redacted.to_excel(...)`

There is **no existing output-writer abstraction** — `.xlsx` output is hardcoded via
direct `pandas.DataFrame.to_excel(...)` calls in `main()`, `unmask_documents()`, and
`unmask_file()`.

### 2.2 Masking is value-based, not position-based (this works in our favour)

`scan_text_for_entities()` ([dual_redaction_pipeline.py:1856](dual_redaction_pipeline.py#L1856)) builds a
`global_entities` dict of **unique value → token** (e.g. `"John Smith" → "[PERSON:001]"`)
by scanning the flattened Raw Excel once. `apply_masking()`
([dual_redaction_pipeline.py:2049](dual_redaction_pipeline.py#L2049)) then does a plain string
substitution of that dict against **any** text passed to it.

This means masking is **not tied to row/column position** — it only needs a chunk of
text and the `global_entities` dict. That's important: to produce an "as is" DOCX or
XLSX, we do **not** need the flattened Raw Excel to map back to original cell
positions. We can re-open the **original uploaded file** and run `apply_masking()`
directly against each paragraph/run/cell's text in place, once `global_entities` has
been computed from the (existing) flatten-and-scan pass.

### 2.3 Frontend

Single-file Flask app — HTML/JS is inlined in [app.py](app.py) (no separate template
files, no build step):

- Upload dropzone + file list: [app.py:342-359](app.py#L342-L359)
- `runPipeline()` — builds `FormData` (`files`, `client_id`, `job_id`) and `POST /upload`: [app.py:524-570](app.py#L524-L570)
- `showResults()` — renders one download link per filename returned by the backend: [app.py:576-598](app.py#L576-L598)
- No format selector exists today.

### 2.4 Backend request/response plumbing

- `POST /upload` ([app.py:730-793](app.py#L730-L793)): validates files, creates a `process_id`/`file_id` per file, writes `files/manifest_{process_id}.json`, **pre-computes and stores `redacted_path` in MySQL as `{stem}_redacted.xlsx` before the pipeline even runs** ([app.py:773](app.py#L773) — hardcoded `.xlsx` extension). Spawns `dual_redaction_pipeline.py --manifest <path>` as a subprocess.
- `_run_pipeline()` ([app.py:805-882](app.py#L805-L882)): diffs `OUTPUT_DIR` before/after the subprocess exits to discover output filenames — **this part is format-agnostic already**, it'll pick up whatever extension the pipeline writes without changes.
- `GET /download/<filename>` ([app.py:910](app.py#L910)): serves whatever file exists in `OUTPUT_DIR` by name — already format-agnostic.
- `/rehydrate` (`POST`) and `--unmask-file` → `unmask_file()` ([dual_redaction_pipeline.py:2149](dual_redaction_pipeline.py#L2149)): looks up `redacted_path` from MySQL (`pii_files.redacted_path`), reads it with `pd.read_excel(...)`, and always writes `{stem}_unmasked.xlsx`. **This assumes Excel today and will break if `redacted_path` points at a `.pdf`/`.docx`.**
- Legacy CLI `--unmask` → `unmask_documents()` ([dual_redaction_pipeline.py:2108](dual_redaction_pipeline.py#L2108)): same Excel assumption, globs `*_redacted.xlsx`.

### 2.5 Database

`pii_files.redacted_path` ([db.py:239-247](db.py#L239-L247), schema in
[wiki/architecture.md](wiki/architecture.md)) stores a single path with no separate
column recording *which format* it is. Today that's fine because it's always `.xlsx`.

---

## 3. Feasibility by format (this drives the plan's phasing)

| Format | "As is" difficulty | Approach |
|--------|--------------------|----------|
| **XLSX / XLS** | 🟢 Low | Re-open the *original* workbook with `openpyxl` (preserves sheets, formatting, merged cells — all the things `spreadsheet_to_excel()` currently throws away), walk every cell, run `apply_masking()` on cell text in place, save as new `.xlsx`/`.xls`. |
| **DOCX** (and `.doc`, via existing COM→DOCX conversion) | 🟡 Medium | Re-open the *original* `.docx` with `python-docx`, walk paragraphs/tables/headers/footers (same traversal `docx_to_excel()` already does), run `apply_masking()` on each run's text in place (careful with run-splitting so formatting isn't lost), save as new `.docx`. Images/styles/layout are untouched since we're only rewriting text runs. |
| **PDF** | 🔴 High | The current PDF pipeline (Docling / PyMuPDF / Tesseract) **discards page/bounding-box coordinates once text is flattened into Raw Excel rows.** To redact a PDF "in place" with visual fidelity (draw over the original PII text at its exact location and burn in the token), Phase 1 would need to be reworked to carry `(page_num, bbox)` alongside every extracted cell, then Phase 2 would need per-occurrence — not just per-unique-value — coordinates to know *where* to redact on the page. That's a substantially bigger change than DOCX/XLSX. See Open Question #1 for a cheaper alternative. |

This is why Open Question #1 below matters a lot — it changes the size of this project
by roughly 3-4x depending on the answer for PDFs.

---

## 4. Proposed Architecture

1. **Frontend**: add a `<select>` next to the Run Redaction button (or per-batch,
   see Open Question #4) with options `Excel` (default, current behaviour) and
   `As is`. Value sent as `output_format` in the `/upload` FormData.
2. **`POST /upload`** ([app.py](app.py)): read `output_format`, write it into
   `manifest_{process_id}.json` alongside the existing `client_id`/`job_id`/`files`
   fields. Stop pre-computing a hardcoded `.xlsx` `redacted_path` at upload time
   ([app.py:773](app.py#L773)) — the true output extension isn't known until Phase 1
   determines the source format (for "As is") or is always `.xlsx` (for "Excel").
   Instead, either compute the correct extension synchronously (we already know the
   uploaded file's own extension) or have the pipeline report the final path back
   for the app to persist to MySQL after it finishes.
3. **`dual_redaction_pipeline.py`**: `main()` reads `output_format` from the
   manifest (default `"excel"` for ad-hoc CLI runs with no manifest — add a
   `--output-format` CLI flag for parity). Phase 2 branches:
   - `"excel"` → unchanged flatten-and-write path (today's code, untouched).
   - `"as_is"` → new per-format writer, keyed off the *original* input extension:
     - `write_redacted_xlsx_as_is(input_path, output_path, global_entities)`
     - `write_redacted_docx_as_is(input_path, output_path, global_entities)`
     - `write_redacted_pdf_as_is(input_path, output_path, ...)` — scope pending Open Question #1
   - The Raw Excel flatten-and-scan step (Phase 1 + `scan_text_for_entities()`) still
     runs unconditionally either way, since that's how `global_entities` is built
     and it also produces the Raw Excel used for audit/debugging today.
4. **Rehydration**: `unmask_file()` / `unmask_documents()` need the mirror-image
   change — a format-aware reader/writer that reverses `apply_masking()` via the
   existing `_apply_token_map()`-style substitution, applied to whichever format the
   redacted file is actually in. Requires knowing the format at rehydrate time — see
   Open Question #2.
5. **Database**: add a column (e.g. `pii_files.output_format` or just infer from
   `redacted_path`'s suffix) so `/rehydrate` and `--unmask-file` know how to read the
   file before doing the token substitution.

---

## 5. Open Questions (need your answers before work starts)

1. **PDF fidelity — how faithful does "As is" need to be for PDFs?**
   - **Option A (high fidelity):** Visually redact the original PDF — black-box or
     white-out the exact PII text location and overlay the token, preserving the
     original layout/images/fonts exactly. Requires reworking PDF Phase 1 to carry
     bounding-box + page metadata through to Phase 2 (all three tiers: Docling,
     PyMuPDF, Tesseract). Larger effort, and Tesseract-tier (scanned/image-only
     PDFs) redaction means drawing over a rendered image, not text.
   - **Option B (format-only):** Generate a **new** PDF from the redacted text/table
     content (e.g. via `reportlab`/`fpdf2`), so the download is a `.pdf` but does
     **not** preserve the original visual layout — same limitation as today's
     flattened Excel, just wrapped in a PDF container. Much smaller effort, reuses
     the existing flatten pipeline almost as-is.
   - **Option C:** Ship XLSX/DOCX "as is" first (Section 3, low/medium effort) and
     treat PDF as a separate follow-up phase once we know if Option A is actually
     needed.
     answer : i know its complictaed but i need the option a 

2. **Does rehydration (`/rehydrate`, `--unmask-file`) need to restore the *original*
   format too**, or is it acceptable for the unmasked/rehydrated file to always come
   back as `.xlsx` regardless of what format was downloaded? (This affects DB schema
   and how much of the rehydration code needs to change.)

Answer: need to restore the *original* format too*

3. **Should the existing "Excel" option also be upgraded** to preserve multi-sheet
   structure/formatting for XLSX/XLS uploads (today it flattens those too, discarding
   formatting — see [dual_redaction_pipeline.py:2769](dual_redaction_pipeline.py#L2769)), or is "Excel" intentionally
   the generic flattened format and only "As is" needs to preserve original
   structure?
Answer:  Keep "Excel" as Generic Flattened Format
4. **Is the format selection per-upload-batch or per-file?** The UI supports
   uploading multiple files of different types in one batch. Is one dropdown
   applying to the whole batch acceptable, or does each file need its own selector?
   (Recommendation: one dropdown per batch — simpler UX, and "As is" already means
   "whatever format each file originally was," so mixed-format batches still work
   correctly even with a single toggle.)
   answer:One Dropdown Per Batch

5. **Fallback behaviour** — if "As is" is selected but a format can't be redacted
   in place for some reason (e.g. corrupt DOCX structure, a `.doc` that fails Word
   COM conversion), should the pipeline fall back to Excel output with a warning, or
   fail the file outright?
answer:Fallback to Excel (with warning)
6. **Output filename convention** — keep `{stem}_redacted.{original_ext}` (e.g.
   `MyDoc_redacted.docx`), or something else?
Answer: keep it   MyDoc_redacted.docx  
---

## 6. Task List

- [x] Get answers to Open Questions 1-6
- [x] Frontend: add output format dropdown to upload UI, wire into `FormData` ([app.py](app.py) — `#format-select`, `.selector-card`)
- [x] `POST /upload`: accept `output_format`, persist to manifest, patch DB post-run via a `manifest_{process_id}_result.json` sidecar (needed because "as_is" can fall back to Excel per-file at runtime)
- [x] DB: `migrations/0002_pii_output_format.sql` adds `pii_files.output_format`; `db.py` — `record_file()`, `get_file()`, new `update_file_output()`
- [x] Pipeline: `--output-format` CLI flag + manifest `output_format` field, `main()` Phase 2 restructured with as_is→excel fallback + result sidecar write
- [x] Pipeline: `write_redacted_xlsx_as_is()` + `write_redacted_xls_as_is()` (openpyxl / xlrd+xlwt, mask in place, preserve sheets/formatting)
- [x] Pipeline: `write_redacted_docx_as_is()` + `convert_docx_to_doc()` (python-docx, mask paragraph/table-cell runs in place, preserve formatting/images; `.doc` round-trips via Word COM both directions)
- [x] Pipeline: PDF "as is" writer — **implemented Option A** (visual redaction). `write_redacted_pdf_as_is()` locates each PII value's bbox via the native text layer (`page.get_text("words")`) or Tesseract OCR for scanned pages, then uses PyMuPDF redaction annotations (`add_redact_annot` + `apply_redactions`) to draw over the original text and burn in the token — a self-contained pass over the *original* PDF, independent of the existing Docling/PyMuPDF/Tesseract Raw-Excel extraction code (see architecture note below).
- [x] Rehydration: format-aware `unmask_file()` (DB-backed, dispatches on `pii_files.output_format`) and `unmask_documents()` (legacy folder-scan — `.xlsx` always reads as flattened "Excel" since ad-hoc CLI runs have no DB row to disambiguate; `.docx`/`.doc`/`.pdf`/`.xls` are unambiguous and dispatch to `unmask_as_is()`). New `unmask_xlsx_as_is()` / `unmask_xls_as_is()` / `unmask_docx_as_is()` / `unmask_pdf_as_is()`.
- [x] `app.py` download/result-list handling — confirmed already format-agnostic (folder diff + Content-Disposition header), only `/rehydrate`'s hardcoded `.xlsx` unmasked-filename guess needed a fix (now derived from `pii_files.output_format`)
- [x] Smoke-tested `write_redacted_xlsx_as_is/docx_as_is/pdf_as_is` + their `unmask_*_as_is` counterparts against synthetic files (multi-sheet xlsx, multi-run docx with a table, a real PDF with `add_redact_annot`) — 21/21 checks passed, including that PDF redaction actually removes the original text (not just visually covers it) and the token round-trips back to the original value.
- [ ] Update wiki docs (`usage.md`, `output-files.md`, `architecture.md`) to describe the new option — in progress
- [ ] **Not yet run**: a real end-to-end `/upload` → live pipeline (Ollama + GLiNER + MySQL) → download → `/rehydrate` pass in the actual running app. Everything above was verified by (a) direct unit-level smoke tests of the new writer/unmasker functions and (b) code review of the wiring — not by driving the browser UI.
- [ ] **Not tested**: the `.doc` round-trip (`convert_doc_to_docx` / `convert_docx_to_doc` via Word COM) — mirrors the pre-existing, already-working `convert_doc_to_docx` pattern, but needs a real `.doc` file and MS Word installed to verify.
- [ ] **Environment gap found, unrelated to this feature**: this venv (`pii_env`) has neither `xlrd` nor `xlwt` installed. `write_redacted_xls_as_is()`/`unmask_xls_as_is()` will always raise ImportError and fall back to Excel output as designed — but note the *existing* `.xls` upload path (`spreadsheet_to_excel()`, uses `engine='xlrd'`) likely already fails today for the same reason, pre-dating this feature. `pip install xlrd xlwt` into `pii_env` would fix both; ask before installing.
- [ ] Manual test matrix: each input format × {Excel, As is} × single file and mixed-format batch, run through the real app
- [ ] Verify rehydration round-trips correctly for both output formats, through the real `/rehydrate` endpoint

### Architecture note (deviation from Section 4's original wording)

Section 4 originally proposed reworking Phase 1 PDF extraction (Docling/PyMuPDF/Tesseract) to carry `(page, bbox)` through to Phase 2. That code has a lot of fragile, table-alignment-specific quirk-handling (`_fix_docling_table_alignment`, `_fix_raw_excel_alignment`) that only makes sense in flattened-DataFrame space — retrofitting bbox-tracking through it would have been high-risk. Instead, `write_redacted_pdf_as_is()` is a **separate, self-contained pass**: it re-opens the *original* PDF fresh with PyMuPDF, does its own word/line extraction independent of Docling, and only reuses the already-computed `global_entities` (value→token) map for *what* to redact. The existing Raw-Excel/"Excel" output path is completely untouched. Net effect is the same (exact visual-fidelity redaction), lower risk to the existing pipeline.

---

## 7. Out of Scope (for this phase, unless an open question answer says otherwise)

- Changing anything about *what* gets redacted (Tier 0-3 detection logic) — this
  feature only changes the **output container format**, not the redaction rules.
- Changing the "Excel" option's current flattened behaviour (unless Open Question #3
  says otherwise).
