# Plan: Rehydrate an Uploaded (Derived) File, Not Just the Stored Redacted Output

Status: **IMPLEMENTED 2026-07-13** — all Open Questions in Section 5, 5c, and
5d answered; Section 3.4's Process Name picker + Section 3.0/3.1's
process_id-scoped, conflict-aware rehydration are both built. One answer
(Q3) is implemented with a caveat — see the note under Q3 in Section 5 for
why images pass the upload allowlist but are rejected with a specific error
at request time rather than silently mis-rehydrating.
Owner: TBC
Created: 2026-07-09
Updated: 2026-07-13 — added **Section 3.0**: the real-world trigger for this
plan turns out to be a multi-file batch (e.g. 4 redacted source files) merged
by an external tool into **one** downstream document, not a single derived
file from a single `file_id`. Section 3.0 resolves this with a `process_id`-scoped,
conflict-aware token merge. Sections 1–8 below are otherwise unchanged from 2026-07-09
except where explicitly marked "(updated)".
Updated: 2026-07-13 (later same day) — added **Section 3.4**: replaces manual
`process_id`/`file_id` entry with a **Process Name picker** — a dropdown/list
populated from the new `pii_files.process_name` column
([migrations/0005_pii_files_process_name.sql](../../../migrations/0005_pii_files_process_name.sql),
the free-text label users now enter on the redact-upload page). Selecting a
name resolves its `process_id` and shows a table of the files redacted under
it (reusing the existing `/api/redaction-map/<process_id>` endpoint), then the
user uploads the derived document to rehydrate. This is a **discovery-UI
layer on top of the already-designed Section 3.0 backend** — it does not
change the `process_id`-scoped, conflict-aware merge design, it just replaces
"the user must know/paste an ID" with "the user picks a name from a list".
Updated: 2026-07-14 — added **Section 3.5**: restored values are now
highlighted yellow in the rehydrated output (`redaction_formats.py`'s
`unmask_xlsx_as_is`/`unmask_xls_as_is`/`unmask_docx_as_is`/`unmask_pdf_as_is`),
so a reviewer can see at a glance what got rehydrated.

---

## 1. Goal

Today, `/rehydrate` only accepts a `file_id` and rehydrates the **exact redacted
output file the pipeline itself produced and stored on disk**
(`pii_files.redacted_path`). It cannot rehydrate anything else.

In practice, the redacted file is often not the end of the road — it's an
**intermediate artifact**. Example: a redacted superannuation statement
(tokens like `<*PERSON_1*>`, `<*TFN_1*>` burned into the text) is fed into a
third-party tool to prepare an **ITR (Income Tax Return)** filing. That tool
produces a **brand-new document** — different filename, different layout,
possibly a different file format entirely — that still contains the same
literal token strings, because they were copied/typed across from the
redacted source. **That new document is what the end user actually needs
rehydrated**, and today's `/rehydrate` has no way to accept it — it only
knows how to re-open the file *it* originally wrote to disk.

We want the `/rehydrate` page to support:

1. The user provides the **file_id** of the original redaction (same as
   today — this is what scopes which `pii_redaction_map` rows / token
   vocabulary apply).
2. The user **uploads a file** — the new, derived document (e.g. the
   generated ITR file) that contains some or all of that file_id's tokens.
3. The backend loads `{token: source_text}` for that `file_id` from
   `pii_redaction_map` (via `pii_files` to confirm the `file_id` exists),
   and performs a **token → original-value substitution directly against
   the uploaded file's own content** — not against the stored
   `redacted_path`.
4. The rehydrated version of the **uploaded file** (same format, tokens
   swapped back to plaintext) is returned to the user for download.

The existing "just rehydrate the file I redacted" use case (no separate
downstream document exists) should keep working — see Open Question #1 for
whether that stays as a second mode on the same page or is dropped.

### 1.1 The real trigger for this plan: one batch of files, merged into one document (NEW)

The scenario above generalizes to the actual workflow this plan needs to support —
it's not a single-file edge case, it's the primary case:

1. A user redacts a **batch of files** in one upload — e.g. 4 source documents
   (a payslip, a bank statement, a super statement, a private health statement) for
   one client's tax return. All 4 share one `process_id` (the batch id minted at
   upload time — [app.py:1172-1173](app.py#L1172-L1173)); each file also gets its
   own `file_id` and its own token vocabulary (Section 2.3 below — token counters
   reset **per file**, so `<*PERSON_1*>` means a different person in each of the 4
   files, `<*TFN_1*>` a different TFN, etc).
2. The user takes all 4 redacted outputs and prepares an **ITR (Income Tax Return)
   filing** in a separate, third-party application, copying values across from
   whichever of the 4 redacted files they came from.
3. That ITR preparation produces **one new document** — a single tax-return draft —
   that can contain tokens copied from **any or all of the 4 source `file_id`s**,
   mixed together in one file with no record of which token came from which
   original file.
4. The user's last step is to rehydrate *that one ITR document*. Today's design
   (Section 2.3 below) explicitly limits rehydration to a single `file_id`'s token
   map — which cannot work here, because the ITR document is a many-to-one merge
   of 4 files' token vocabularies, and those vocabularies are **not guaranteed to
   be collision-free** with each other (see Section 2.3, and Section 3.0.1 for why
   that collision risk is concrete, not theoretical).

This means Open Question #2 (Section 5, originally left open/ambiguous) is no
longer a hypothetical "what if" — it's the primary case this plan must solve.
**See Section 3.0 for the resolution design.**

---

## 2. Current State (what exists today)

### 2.1 The `/rehydrate` page and route

- `REHYDRATE_HTML` ([app.py:965-1055](app.py#L965-L1055)) — single-field
  form: a text input for `file_id` ([app.py:1011-1015](app.py#L1011-L1015)),
  POSTs `{file_id}` as form-urlencoded data to `/rehydrate`
  ([app.py:1030](app.py#L1030)), and downloads whatever blob comes back.
  **No file upload input exists on this page today.**
- `GET /rehydrate` ([app.py:1313-1315](app.py#L1313-L1315)) — serves the
  page, no auth check on page load itself.
- `POST /rehydrate` ([app.py:1318-1362](app.py#L1318-L1362)):
  1. Auth-gated on `session["uid"]` ([app.py:1320-1321](app.py#L1320-L1321))
     — same precedent as the reference-table/preview endpoints
     ([wiki/feature-updates/phase-1/plan-table-implementation.md](plan-table-implementation.md), Section 2.3 / Open Question #8).
  2. Looks up `db.get_file(file_id)` ([db.py:273-292](db.py#L273-L292)) —
     404 if unknown.
  3. Shells out to `dual_redaction_pipeline.py --unmask-file <file_id>` as a
     subprocess ([app.py:1331-1343](app.py#L1331-L1343), 120s timeout).
  4. That subprocess runs `unmask_file(file_id)`
     ([dual_redaction_pipeline.py:2170-2212](dual_redaction_pipeline.py#L2170-L2212)),
     which reads `file_row["redacted_path"]` **from disk** (the pipeline's
     own stored output — [dual_redaction_pipeline.py:2184-2187](dual_redaction_pipeline.py#L2184-L2187)),
     builds `mapping = db.get_redaction_map(file_id=file_id)`
     ([db.py:327-346](db.py#L327-L346), returns `{redacted_text:
     source_text}`), and dispatches on `pii_files.output_format` to unmask
     that specific stored file — `unmask_as_is()`
     ([dual_redaction_pipeline.py:3622-3646](dual_redaction_pipeline.py#L3622-L3646))
     for "as is" formats, or a flattened-Excel token-map pass for `"excel"`.
  5. Streams the resulting `unmasked/{stem}_unmasked{ext}` file back.

**The critical constraint:** every step above is anchored to
`pii_files.redacted_path` — a path the *pipeline itself* wrote. There is no
code path today that takes an arbitrary uploaded file and applies a
file_id's token map to it.

### 2.2 The per-format unmask helpers are already generic on `(path, mapping)`

This is the key reuse opportunity. Two near-identical sets of per-format
unmask functions already exist, and **neither cares whether the input path
is the original `redacted_path` — they just open whatever file is at the
path they're given and substring-replace every `{token: value}` pair in
`mapping`**:

- `dual_redaction_pipeline.py`: `unmask_xlsx_as_is` (3523),
  `unmask_xls_as_is` (3540), `unmask_docx_as_is` (3560), `unmask_pdf_as_is`
  (3590), dispatcher `unmask_as_is` (3622-3646) — heavy module (eagerly
  imports Docling/GLiNER/Presidio/cv2 at import time, per
  [plan-table-implementation.md Section 6.1](plan-table-implementation.md#L287-L304)).
- `redaction_formats.py` (new, lightweight module added for the reference-table/preview
  feature): duplicate versions of the same functions —
  `unmask_xlsx_as_is` (544), `unmask_xls_as_is` (561), `unmask_docx_as_is`
  (581), `unmask_pdf_as_is` (613) — plus `_unmask_doc` for legacy `.doc`.
  These are already imported synchronously by `app.py` with no heavy ML
  dependencies.
- `redaction_formats.unmask_single_value(redacted_path, token, value)`
  ([redaction_formats.py:717-742](redaction_formats.py#L717-L742)) shows the
  exact dispatch pattern already in production use (by the manual
  edit/delete-row flow, [app.py:1377-1392](app.py#L1377-L1392)): pick the
  right per-format function by `redacted_path.suffix.lower()`, write to a
  temp file, swap it in. It builds a **one-pair** `mapping = {token:
  value}` — but the underlying functions it calls already accept a
  **multi-pair** dict natively (that's literally what `mapping` is for).

**Implication:** rehydrating an arbitrary uploaded file against a full
file_id token map is *not* new format-handling logic — it's the same
`unmask_xlsx_as_is` / `unmask_xls_as_is` / `unmask_docx_as_is` /
`_unmask_doc` / `unmask_pdf_as_is` functions in `redaction_formats.py`,
called with **the uploaded file's own path** and **the full mapping** for
`file_id`, dispatched on **the uploaded file's own extension** (not
`pii_files.output_format`, which describes the *original* redacted output,
not whatever new document the user is uploading now).

### 2.3 Token counters reset per file — mapping must stay scoped to one `file_id`

`secret_map = {}` / `candidate_counters = {}` are re-initialized **inside
the per-file loop** in `main()`
([dual_redaction_pipeline.py:3743-3744](dual_redaction_pipeline.py#L3743-L3744)),
i.e. every file starts its token numbering back at `_1`. So `<*PERSON_1*>`
in file `FIL-ABC-100001` and `<*PERSON_1*>` in `FIL-ABC-100002` (even from
the same batch/`process_id`) are almost certainly **different people**.

`db.get_redaction_map(process_id=...)`
([db.py:327-346](db.py#L327-L346)) exists and would merge every file in a
batch into one `{token: value}` dict — but doing that for rehydration would
silently collide same-named tokens across files (Python dict construction
keeps whichever row the SQL happened to return last). **This plan originally assumed
rehydration stays scoped to a single `file_id` per request** — Section 3.0
below replaces that assumption with a `process_id`-scoped, conflict-aware
merge, since Section 1.1's real-world workflow requires pulling tokens from
more than one `file_id` at once.

### 2.4 Upload handling conventions (for the new file-upload UI)

- `ALLOWED_EXTENSIONS = {".pdf", ".xlsx", ".xls", ".docx", ".doc", ".png",
  ".jpg", ".jpeg"}` ([app.py:31](app.py#L31)),
  `MAX_CONTENT_LENGTH = 200 MB` ([app.py:34](app.py#L34)) — the existing
  `/upload` route's conventions ([app.py:1091-1119](app.py#L1091-L1119)):
  `request.files.getlist(...)`, `secure_filename()`, extension allowlist
  check, save under a working directory.
- Image formats (`.png/.jpg/.jpeg`) are a dead end for this feature as
  written: pixel "as is" redaction burns a solid box + label into the
  image with **no underlying searchable text layer**
  ([redaction_formats.py](redaction_formats.py), `remask_image_from_original`
  docstring, ~line 745-750) — there is no token *text* in an image to find
  and replace. A downstream document that's itself an image/scanned PDF
  would need OCR just to locate the token strings before any replacement
  could happen — a materially bigger feature (see Open Question #3).

### 2.5 Database

No schema gaps identified — `pii_files` (file_id → filename/format/paths)
and `pii_redaction_map` (file_id → token↔value pairs, including
`source='manual'` rows from the reference-table feature) already contain
everything this feature needs to look up. **No new migration is currently
anticipated** (see Section 6).

### 2.6 `pii_files.process_name` now exists (NEW, 2026-07-13)

Separately from this plan, the redact-upload page ([app.py:438-444](app.py#L438-L444))
now has a **required "Process Name" textbox** above the Client dropdown — a
free-text label the user types per upload batch (e.g. "Monthly payroll
batch"), sent as `process_name` on `POST /upload` and stored on every
`pii_files` row for that batch
([migrations/0005_pii_files_process_name.sql](../../../migrations/0005_pii_files_process_name.sql),
`db.record_file()` in [db.py](../../../db.py)). It's nullable (existing rows
from before this migration have `process_name IS NULL`) and duplicated across
every file in a batch, matching how `process_id`/`client_id`/`job_id` are
already denormalized onto every `pii_files` row.

This is the missing piece that makes a **name-based** picker possible on the
rehydrate page instead of requiring the user to know/paste a `FIL-...` or
`PRC-...` id — see Section 3.4.

### 2.7 The "files in this process" table already has a data source (NEW)

`GET /api/redaction-map/<process_id>` ([app.py:1448-1468](app.py#L1448-L1468))
already exists (built for the reference-table/preview feature,
[plan-table-implementation.md](plan-table-implementation.md)) and already
returns, per file in a `process_id`: `file_id`, `filename`,
`redacted_filename`, `output_format`, and `rows` (every
`{id, source_text, redacted_text, source}` from `pii_redaction_map` for that
file, via `db.get_redaction_rows()`). This is **exactly** the shape needed to
render "the files in this process, and what was redacted in each" on the
rehydrate page (Section 3.4) — no new read endpoint needed for that part, only
a new endpoint to list processes **by name** (Section 3.4.1) to populate the
picker itself. It is, however, auth-gated (`_require_uid()`) and returns raw
`source_text` (the original plaintext PII) — see Open Question NEW-5 on
whether that's appropriate to expose before the user has uploaded anything.

---

## 3. Proposed Design

### 3.0 Design fork: rehydration scope is `process_id`, not `file_id` (NEW)

**Recommended (needs your confirmation — see Open Question NEW-3 below):** the
upload+rehydrate flow should always be scoped by **`process_id`** (the batch a
`file_id` belongs to — already a column on both `pii_files` and
`pii_redaction_map`, [migrations/0001_pii_redaction_tables.sql](../../../migrations/0001_pii_redaction_tables.sql#L28-L54)),
not by a single `file_id`.

Why this subsumes the single-file case instead of needing two separate modes:

- A batch of **one** file is just a `process_id` with one `file_id` under it —
  no possible token collision, behaves identically to the original single-file
  design in Sections 3.1–3.3 below.
- A batch of **four** files (the ITR scenario, Section 1.1) is the *same* code
  path, just with more files under one `process_id` — the only new logic needed
  is **conflict detection** across those files' token maps (Section 3.0.2), not
  a second mode or a second endpoint.
- The user only ever pastes **one ID**. Per your answer to the original Open
  Question #2 ("not by tak[ing] the file_id [manually entered] ... take it
  from [the batch]"), the UI should accept whichever ID the user has to hand:
  if they paste a `file_id`, the backend resolves its `process_id` via
  `db.get_file(file_id)["process_id"]` and scopes to the whole batch anyway —
  see Open Question NEW-3.

#### 3.0.1 The collision problem, concretely

`db.get_redaction_map(process_id=...)` ([db.py:327-346](db.py#L327-L346))
already exists and already merges every file under a `process_id` into one
`{redacted_text: source_text}` dict — but a plain Python dict silently keeps
whichever row MySQL happens to return last for a given key. Concretely: if
file A's map has `<*PERSON_1*> → "John Smith"` and file B's map (same
`process_id`) has `<*PERSON_1*> → "Mary Jones"`, naively merging gives you
**one** of those two answers for **every** occurrence of `<*PERSON_1*>` in the
uploaded ITR document — silently rehydrating the wrong person's name into the
output for whichever of A/B lost the merge, with no error or warning that it
happened.

This is a real risk in the 4-file ITR scenario, not a rare edge case: token
counters restart at `_1` per file (Section 2.3), so the *first* `PERSON`,
first `TFN`, first `ACCOUNT_NUMBER`, etc. in every file in a batch all collide
on the same token string by construction — a 4-file batch is close to
guaranteed to have at least one colliding token pair somewhere.

#### 3.0.2 Conflict-aware merge (new logic)

Replace the naive merge with a new function — e.g.
`db.get_redaction_map_for_process(process_id) -> dict` — that returns, per
distinct `redacted_text` (token) across every `file_id` in the batch, **the
full set of distinct `source_text` values** it maps to (not just the last one
MySQL happened to return):

```
{
  "<*PERSON_1*>":  {"John Smith": ["FIL-ABC-100001"]},                          # no conflict
  "<*TFN_1*>":     {"123456782": ["FIL-ABC-100001"], "987654321": ["FIL-ABC-100002"]},  # CONFLICT
  "<*COMPANY_1*>": {"Acme Pty Ltd": ["FIL-ABC-100001", "FIL-ABC-100003"]},        # same value from 2 files — no conflict
}
```

Then, before substituting against the uploaded document:

1. **Scan the uploaded document for every token-looking string actually
   present** — reuse `TOKEN_RE` ([redaction_formats.py:31](redaction_formats.py#L31))
   against the document's own extracted text, per format (cell values for
   xlsx/xls, paragraph/table text for docx, page word-text for pdf — the same
   per-format text access the `write_redacted_*_as_is` / `unmask_*_as_is`
   functions already do). This is the same capability the original Open
   Question #4 flagged as "would need a text-scan per format" — it now
   becomes **required**, not optional, because it's how conflict-checking is
   scoped to only the tokens that actually matter for *this* document.
2. For every token found in the document:
   - **Not in the process's map at all** → left as-is (today's existing
     behavior for unrecognized tokens — see Open Questions #4/#5, still open).
   - **In the map with exactly one distinct `source_text`** (whether from one
     file, or the same value repeated identically across several files) →
     safe, substitute normally.
   - **In the map with 2+ distinct `source_text` values** → a genuine
     conflict, **and it actually matters for this document** because the
     token is present in it. This must not be auto-resolved by silently
     picking one value — see Open Question NEW-1.

This scan-then-classify approach means a batch can contain any number of files
with token collisions across them, and rehydration still works perfectly for
any uploaded document that only actually draws on non-colliding tokens (or
only on files whose colliding categories were never copied into this
particular document) — a conflict only blocks rehydration of the *specific*
token(s) that are both (a) ambiguous across the batch and (b) actually present
in the uploaded file.

#### 3.0.3 Updated data flow (multi-file case)

```
4 files redacted in one batch upload
    │
    ▼
process_id (1) ── file_id A, file_id B, file_id C, file_id D
    │                each with its own pii_redaction_map rows
    │
    │   (user prepares an ITR filing in a separate tool from all 4
    │    redacted outputs — producing ONE new document mixing tokens
    │    from any/all of A–D)
    ▼
User opens /rehydrate, enters process_id (or any one file_id — see
Open Question NEW-3), uploads the ITR document
    │
    ▼
POST /rehydrate  {process_id, file: <upload>}
    │
    ├─ db.get_files_for_process(process_id)         — resolve all file_ids in the batch
    ├─ db.get_redaction_map_for_process(process_id)  — token → {value: [file_ids]}, conflicts preserved
    ├─ scan uploaded doc for every token string present (per format, TOKEN_RE)
    ├─ classify: safe (0 or 1 distinct value) vs conflicting (2+ distinct values)
    ├─ substitute safe tokens; handle conflicting tokens per Open Question NEW-1
    ▼
Rehydrated ITR document (+ conflict warnings, if any) streamed back for download
```

### 3.1 Backend: rehydrate an uploaded file against a batch's token map (updated per Section 3.0)

New logic (exact endpoint shape depends on Open Question #6), replacing the
subprocess shell-out with an in-process call — same rationale
[plan-table-implementation.md Section 6.1](plan-table-implementation.md#L287-L304)
already established for the preview/add-value endpoints (fast, no
Docling/GLiNER/Presidio/cv2 load, feels instant):

1. Require `session["uid"]` — same gate as today's `/rehydrate` and every
   endpoint in the reference-table family.
2. Read `process_id` from the form (always resolved client-side from the
   Section 3.4 picker selection, never hand-typed — Open Question NEW-3
   superseded) and the uploaded file from `request.files`.
3. Resolve and validate the batch — `db.get_files_for_process(process_id)`
   ([db.py:354-370](db.py#L354-L370)) — 404 if empty/unknown (updated from the
   original single-`file_id` `db.get_file()` check).
4. `mapping = db.get_redaction_map_for_process(process_id)` — **new**
   conflict-aware function (Section 3.0.2), replacing the flat
   `db.get_redaction_map(file_id=file_id)` call this step used before scope
   moved from `file_id` to `process_id`.
5. Save the upload to a scratch path (`secure_filename()`, same extension
   allowlist as redact-upload per Q3's answer — images pass the allowlist but
   are rejected with a specific error before scanning, since there's no text
   layer to substitute against), under a new working directory
   (`rehydrate_uploads/`, mirroring `FILES_DIR` for the redact-upload flow).
6. Scan the upload for every token string present (Section 3.0.2 step 1),
   split `mapping` into a **safe** `{token: value}` subset (0/1 distinct
   value) and a list of **conflicting** tokens (2+ distinct values) that
   were actually found in this document. Dispatch on **the uploaded file's
   own extension** to the matching `redaction_formats.py` function
   (`unmask_xlsx_as_is`, `unmask_xls_as_is`, `unmask_docx_as_is`,
   `_unmask_doc`, `unmask_pdf_as_is`), passing the **safe** mapping only —
   this is a thin new dispatcher function, essentially `unmask_as_is()`
   ([dual_redaction_pipeline.py:3622-3646](dual_redaction_pipeline.py#L3622-L3646))
   pointed at `redaction_formats.py`'s copies instead, operating on the
   upload's path instead of `pii_files.redacted_path`. Handle the
   conflicting-token list per Open Question NEW-1.
7. Return the rehydrated file (new filename, e.g.
   `{uploaded_stem}_rehydrated{ext}`) via `send_file(...,
   as_attachment=True)`.
8. No cleanup — Q7's answer was to keep both the scratch upload and the
   generated output around (not a sensitivity concern per the answer), so
   `rehydrate_uploads/`/`rehydrated/` simply accumulate.

### 3.2 Frontend: add a file-upload field to `REHYDRATE_HTML`

> **Superseded 2026-07-13 — see Section 3.4.** The id is no longer typed by
> hand at all (a `file_id` text input doesn't feature in the new design), so
> this section's specific steps no longer apply as written. Left in place for
> history/rationale (the `FormData` reasoning below still holds); Section
> 3.4.2 is the current frontend design.

- Add a `<input type="file">` field to the existing form
  ([app.py:1011-1018](app.py#L1011-L1018)), alongside the `file_id` text
  input.
- Change the submit handler ([app.py:1021-1052](app.py#L1021-L1052)) from
  `URLSearchParams` (form-urlencoded) to `FormData` (needed for a file
  part), `POST` to the (possibly new — Open Question #6) endpoint.
- Same blob-download pattern as today (`Content-Disposition` filename,
  synthetic `<a download>` click) — no change needed there.
- Error states to add beyond today's generic error box: "no tokens from
  this file_id were found in the uploaded file" (Open Question #5),
  "unsupported file type" (Open Question #3), and — if Open Question #4
  says to surface it — "N token-looking strings in this file didn't match
  any known value for this file_id" as a non-fatal warning alongside a
  successful download.

### 3.3 Data flow diagram

```
Run Redaction (existing, unchanged)
    │
    ▼
pii_files row (file_id) + pii_redaction_map rows (token ↔ source_text) for that file_id
    │
    │   (user takes the redacted output elsewhere — e.g. prepares an ITR
    │    filing from it in a separate tool — producing a NEW document that
    │    still contains the literal token strings)
    ▼
User opens /rehydrate, enters file_id, uploads the NEW document
    │
    ▼
POST /rehydrate  {file_id, file: <upload>}
    │
    ├─ db.get_file(file_id)                     — validate file_id exists
    ├─ db.get_redaction_map(file_id=file_id)     — {token: source_text}
    ├─ dispatch on the UPLOAD's own extension    — NOT pii_files.output_format
    ├─ token → source_text substitution against the upload's own content
    ▼
Rehydrated version of the uploaded file, streamed back for download
```

### 3.4 Frontend: Process Name picker replaces manual ID entry (NEW)

**Goal:** the user should never need to know or type a `file_id`/`process_id`.
Instead, the rehydrate page shows a list of **Process Names** (the free-text
label from Section 2.6); picking one resolves its `process_id`, shows a table
of the files redacted under it, and only then does the user upload the
derived document to rehydrate.

#### 3.4.1 New read endpoint: list processes by name

`GET /api/processes?client_id=&job_id=` (new) — auth-gated like every other
endpoint in this family. Backed by a new `db.list_processes(client_id,
job_id, limit=50)` helper that groups `pii_files` by `process_id`:

```sql
SELECT process_id, process_name, MIN(created_at) AS created_at, COUNT(*) AS file_count
FROM pii_files
WHERE process_name IS NOT NULL AND client_id=%s AND job_id=%s
GROUP BY process_id, process_name
ORDER BY created_at DESC
LIMIT %s
```

**Implemented 2026-07-13, per Open Questions NEW-4/NEW-6/NEW-7's answers:**

- **Scoped to one client + job** (NEW-4: "only the process name will show" —
  read as the rehydrate page gaining its own Client/Job selectors, mirroring
  the redact-upload page's [app.py:438-452](app.py#L438-L452), *before* the
  Process Name picker loads — not a system-wide list). This also means no
  `project`/`activity` join is needed for disambiguation: within one
  client+job, `process_name` alone is enough, since the scope narrowing
  already happened via the two selectors above it.
- Excludes rows where `process_name IS NULL` (NEW-6: "don't show" — legacy
  batches from before migration 0005 are simply absent from the picker).
- Returns newest-first, capped at `limit=50` (NEW-7: no search-as-you-type
  built now since a client+job-scoped list is small; add a `q` param
  mirroring `GET /api/jobs?q=` later if any client's list grows past the cap).

#### 3.4.2 Rehydrate page redesign

Replace the single "File ID" text input in `REHYDRATE_HTML`
([app.py:1060-1067](app.py#L1060-L1067)) with a three-stage form:

1. **Process Name picker** — a `<select>` (or a searchable/type-ahead list if
   Open Question NEW-7 favors that over a plain dropdown), populated on page
   load from `GET /api/processes`. Each option shows `process_name` plus
   enough of client/job to disambiguate (Section 3.4.1).
2. **Files table** — on selecting a process, call the existing
   `GET /api/redaction-map/<process_id>` (Section 2.7) and render a
   read-only table: one row per file (`filename`, a count of redacted
   values) — i.e. "this is what we redacted" for the selected batch, no
   plaintext (NEW-5: option A).
3. **Upload + Rehydrate** — a `<input type="file">` plus the existing
   "Rehydrate & Download" button, now submitting `{process_id, file}` as
   `FormData` to the Section 3.1 upload+rehydrate endpoint. The submit
   handler changes from `URLSearchParams` (form-urlencoded) to `FormData`
   for the same reason Section 3.2 already called out.

This subsumes Section 3.2 (superseded — 3.2 is now folded into this section's
step 3) and **resolves Open Question NEW-3** in Section 5c a different way
than either of its original two options: the page never asks for a
`file_id` *or* a `process_id` typed by hand — the id is resolved entirely
from the Process Name selection, so the ambiguity that question raised no
longer applies.

#### 3.4.3 Updated data flow (adds the picker in front of Section 3.0.3)

```
User opens /rehydrate
    │
    ▼
GET /api/processes  →  [{process_id, process_name, client, job, file_count}, ...]
    │
    ├─ user picks a Process Name
    ▼
GET /api/redaction-map/<process_id>  →  files[] (filename, redacted rows) — Section 2.7
    │
    ├─ table renders: "files in this process" / what was redacted
    ▼
user uploads the derived document (e.g. the ITR draft)
    │
    ▼
POST /rehydrate  {process_id, file: <upload>}     — Section 3.0.3 / 3.1, unchanged
    │
    ▼
Rehydrated document streamed back for download
```

### 3.5 Highlight restored values in the output (NEW, 2026-07-14)

**Implemented 2026-07-14.** Not part of the original plan/Open Questions —
added afterwards on request, so a reviewer opening the rehydrated download
can immediately see which values were restored, instead of them reading as
indistinguishable plain text. Yellow background everywhere, scoped only to
this upload-a-derived-document flow (not the single-file `/rehydrate` button,
which shells out to `dual_redaction_pipeline.py --unmask-file` — a
deliberately separate, duplicated implementation per that module's own
docstring, and out of scope here):

- **XLSX** (`unmask_xlsx_as_is`) / **XLS** (`unmask_xls_as_is`) — the cell's
  fill is set to solid yellow (`openpyxl.styles.PatternFill` /
  `xlwt.easyxf('pattern: pattern solid, fore_colour yellow;')`) whenever that
  cell's value changed.
- **PDF** (`unmask_pdf_as_is`) — the white-out box drawn behind the restored
  text before re-inserting it is now yellow (`fill=(1, 1, 0)`) instead of
  white (`fill=(1, 1, 1)`); the restored text itself is still drawn in black
  on top, unchanged.
- **DOCX/DOC** (`unmask_docx_as_is`, and `.doc` via the existing
  `_unmask_doc` Word-COM round-trip) — precise, substring-level highlighting
  via two new helpers:
  - `_replace_with_spans(text, mapping)` — a single left-to-right scan that
    substitutes every mapping key and returns the new text plus the
    `(start, end)` span of each inserted value, replacing the previous naive
    per-key `.replace()` loop (which could also mis-order overlapping
    replacements across mapping keys).
  - `_rewrite_paragraph_text_highlighted(paragraph, transform_fn)` — an
    unmask-only sibling of the existing `_rewrite_paragraph_text` (which
    masking still uses unchanged). Instead of collapsing a paragraph into one
    run, it rebuilds the paragraph as several runs and sets
    `run.font.highlight_color = WD_COLOR_INDEX.YELLOW` only on the runs that
    fall inside a returned span — so only the restored value is highlighted,
    not the whole paragraph.
  - Known trade-off: new runs copy font formatting (bold/italic/underline/
    name/size/color) from the paragraph's *first* run, not from whichever
    run originally contained the token — so a token that was, say, bold in a
    differently-formatted run loses that distinction after rehydration (it
    still gets the yellow highlight correctly). Acceptable for now; would
    need per-original-run offset tracking to fix.
- `unmask_single_value()` (the reference table's manual single-row restore,
  [plan-table-implementation.md](plan-table-implementation.md)) calls these
  same per-format functions, so it gets the highlight for free — no separate
  change needed there.

Verified manually (no automated tests cover this module): generated sample
`.xlsx`/`.docx`/`.pdf` files with a token embedded, ran each unmask function
directly, and confirmed programmatically — cell fill / run
`highlight_color` / rendered pixel color at the restored text's location all
came back solid yellow, with untouched cells/paragraphs/pixels elsewhere
unaffected. `.xls` was not runtime-verified — `xlwt`/`xlrd` aren't installed
in this project's venv (pre-existing gap, unrelated to this change); the
`unmask_xls_as_is` change mirrors the XLSX logic and was reviewed by
inspection only.

---

## 4. What does *not* need to change

- `db.get_redaction_map()`, `db.get_file()` — reused as-is, no changes.
- The per-format substitution logic itself
  (`unmask_xlsx_as_is`/`unmask_xls_as_is`/`unmask_docx_as_is`/`_unmask_doc`/`unmask_pdf_as_is`
  in `redaction_formats.py`) — reused as-is; they already accept an
  arbitrary path and an arbitrary multi-pair mapping. No new
  format-handling code, just a new dispatcher and a new caller.
- The redaction/upload pipeline (`dual_redaction_pipeline.py` main flow) —
  completely unaffected; this plan only touches rehydration.
- The reference-table/preview/manual-add feature
  ([plan-table-implementation.md](plan-table-implementation.md)) — unrelated,
  no overlap other than both reading `pii_redaction_map`.
- `GET /api/redaction-map/<process_id>` ([app.py:1448-1468](app.py#L1448-L1468))
  — reused as-is for the Section 3.4.2 files table; it already returns
  everything that table needs (Section 2.7).

---

## 5. Open Questions (need your answers)

1. **Does the old "just give me a file_id, rehydrate the file the pipeline
   produced" flow still need to exist**, for cases where no separate
   downstream document was created? If yes: two modes on the same page
   (e.g. "Rehydrate the original output" vs "Rehydrate a different file I
   made from it"), or two separate pages?
   answer: not need the old rehydrate  workflow

2. **Scope: strictly one `file_id` per rehydration request** (this plan's
   working assumption, per Section 2.3's token-collision risk), or does
   the uploaded document sometimes need tokens from **multiple** source
   files merged together (e.g. an ITR built from a payslip *and* a bank
   statement, each with its own file_id)? If multiple, how does the user
   specify them (comma-separated file_ids? all files under one
   `process_id`, accepting the collision risk?), and how should the app
   handle a token that maps to two different values across the given
   file_ids?
   answer: not by takeing the fileid taken from
   **[RESOLVED 2026-07-13 — see Section 3.0]**: confirmed this is the real
   (not hypothetical) case — see Section 1.1. Read as "not by [manually]
   taking [multiple] file_ids [entered by hand] — take [the file list] from
   [the process/batch]" — i.e. scope is the `process_id` the files were
   uploaded under, not a manually-typed list. The collision-handling half of
   this question is now Open Question NEW-1 below, since it needs its own
   answer (auto-merge was never safe — Section 3.0.1).

3. **Supported upload formats for the new document**: same list as today's
   redact-upload allowlist (`.pdf/.xlsx/.xls/.docx/.doc/.png/.jpg/.jpeg`),
   or a different set suited to what an ITR-style tool actually outputs
   (e.g. add `.csv`/`.txt`)? And per Section 2.4 — is it acceptable that
   image/scanned formats **cannot** be rehydrated this way (no searchable
   token text), i.e. should the app just reject those extensions with a
   clear error, or is OCR-based token detection actually needed here?
   answer: yes it ok `.pdf/.xlsx/.xls/.docx/.doc/.png/.jpg/.jpeg
   **Implemented 2026-07-13**: `REHYDRATE_ALLOWED_EXTENSIONS` in
   [app.py](../../../app.py) is the same full list (reuses
   `ALLOWED_EXTENSIONS`) — images pass the upload allowlist as answered.
   However, Section 2.4's technical constraint still holds: pixel "as is"
   redaction leaves no searchable text in an image, so
   `redaction_formats.find_tokens_in_file()` can't scan one for tokens no
   matter what the allowlist says. Rather than let that surface as a generic
   crash, `POST /rehydrate/upload` checks for `redaction_formats.IMAGE_EXTENSIONS`
   specifically and returns a clear 400 explaining why images can't be
   rehydrated this way (OCR-based token detection is still out of scope —
   Section 8). Net effect: the answer is honored at the allowlist level: the
   file picker accepts images, but attempting to actually rehydrate one fails
   with an explicit, specific error instead of either silently doing nothing
   or crashing.

4. **Unmatched token-looking strings** — if the uploaded file contains
   something that looks like a token (`<*CATEGORY_N*>`) but isn't in this
   file_id's map (wrong file_id picked, or the document actually mixes in
   tokens from another file), should the app detect and warn about it
   (needs a text-scan per format), or is silently leaving anything
   unmatched exactly as-is acceptable for v1?
   answer:silently leave

5. **Zero matches** — if none of the file_id's tokens are found anywhere
   in the uploaded document at all (e.g. wrong file uploaded), should that
   be a hard error instead of silently returning the file unchanged?
   answer:. Zero matches almost always means the wrong file (or wrong job) was selected. Returning the file unchanged looks like success but silently did nothing — worse than failing. Error message should say which client+job map was used and how many tokens it contained.

6. **Endpoint shape** — extend the existing `POST /rehydrate` route to
   accept an optional file upload (same URL, new capability), or add a
   distinct endpoint (e.g. `POST /rehydrate/upload`) and leave `/rehydrate`
   untouched? Depends on the answer to #1.
   answer:Separate endpoint: POST /rehydrate/upload. The two flows differ enough (uploaded file + client/job scope vs. an existing file_id) that branching inside one route would tangle the validation and error paths. Leaves the existing /rehydrate untouched — no regression risk.

7. **Retention** — once the rehydrated file has been streamed back to the
   browser, should the server immediately delete both the uploaded scratch
   file and the generated rehydrated output (matching the sensitivity of
   plaintext PII), or keep them around briefly (e.g. in case of a dropped
   download)?
   answer: don't delete it's not a problem 

8. **Auth gate** — confirm `session["uid"]` (same as today's `/rehydrate`
   and the reference-table endpoints) is the right and sufficient gate for
   this new upload+download path too?
   answer:Yes — session["uid"] is the right gate, consistent with /rehydrate and the reference-table endpoints, and this path is no more sensitive than those.
One caveat worth confirming: session["uid"] only proves someone is logged in, not that this user is authorised for the requested client_id/job_id. If access is meant to be scoped per client, this endpoint (and the existing ones) needs an ownership check on top. If any logged-in user may rehydrate any client's data today, then this is consistent and sufficient — but let's make that an explicit decision rather than an accident.

### 5c. Open Questions — multi-file / process_id scope (NEW, needs your answers)

**NEW-1. Conflict resolution UX** — when a token is both ambiguous (2+
distinct source values across the files in the batch, Section 3.0.1) *and*
actually found in the uploaded document, how should the app respond?

- (a) **Hard-fail the whole request** — return an error listing every
  conflicting token and which files/values it could mean; the user must
  resolve it outside the app before rehydration can proceed at all. Simplest
  to build, safest (never guesses), but blocks the *entire* document over one
  ambiguous field.
- (b) **Interactive resolution step** — rehydration first returns the list of
  conflicts (token, candidate values, source file_ids); the user picks the
  correct value for each conflicting token in a small form; a second request
  finishes the substitution with those choices applied. Best UX, but needs a
  two-step flow and a way to carry the pending upload between the two
  requests — meaningfully more work.
- (c) **Best-effort with a warning** — leave conflicting tokens exactly as
  `<*TOKEN*>` in the output (never guess), download proceeds anyway, and a
  warning banner lists which tokens were left unresolved plus their candidate
  values so the user can hand-fix the download.

**My recommendation: (c) for v1** — it never silently corrupts a value (the
failure mode we're specifically trying to avoid), never blocks the whole
document over one ambiguous field, and is less work than (b). (b) is a
natural v2 once real usage shows how often conflicts actually occur.
answer: option c
**Implemented 2026-07-13**: `redaction_formats.rehydrate_uploaded_file()`
substitutes only unambiguous tokens and returns the conflicting
token/candidate list; `POST /rehydrate/upload` surfaces it via an
`X-Rehydrate-Warnings` response header (JSON, URL-encoded) alongside the
successful download, and the rehydrate page shows it as a warning banner.

**NEW-2. Does one ITR always map to exactly one `process_id`?** This design
(Section 3.0) assumes the N source files feeding one ITR filing were
**uploaded together in one batch**, so they share one `process_id`. Is that
guaranteed in your workflow — i.e. do you always redact all of a client's
documents for a given job in a single upload — or can the same ITR filing
draw on files redacted **days apart, in separate uploads** (separate
`process_id`s)? If the latter can happen, the scope needs to be "all files
under one `job_id`" (or an explicit multi-`process_id` list) rather than a
single `process_id` — the same conflict-detection problem, just one level up.
answer: No, not guaranteed.

**NEW-3. Accept a `file_id` or a `process_id` on the rehydrate page?** Should
the upload page's single ID field accept **either** a `file_id` or a
`process_id` (auto-detected by prefix, `FIL-...` vs `PRC-...`, and resolved to
the owning `process_id` either way via `db.get_file(file_id)["process_id"]`),
or should it require the `process_id` specifically (simpler backend, but the
user needs to know/find the batch id rather than any one file's id, which may
be easier to have on hand if they only remember one of the 4 files)?
answer: **SUPERSEDED 2026-07-13 — see Section 3.4**. The user asked for a
Process Name picker instead of any manually-typed id, so this question no
longer applies — the page resolves `process_id` from the picker selection,
never from hand-typed text.

### 5d. Open Questions — Process Name picker UI (NEW, needs your answers)

**NEW-4. Scope of the process list — all clients, or filtered?** Should
`GET /api/processes` (Section 3.4.1) return every process across every
client system-wide, or should the rehydrate page first ask the user to pick
a **Client** (and maybe **Job**, mirroring the redact-upload page's existing
Client/Job selectors, [app.py:438-452](app.py#L438-L452)) and only then list
that client's processes? System-wide is simpler (one dropdown, no
dependency chain) but means any signed-in user can see every other client's
process names (and, once a process is selected, that batch's file names and
redacted values — Section 2.7) in one list. Client-scoped matches the
redact page's existing mental model and narrows exposure, at the cost of an
extra selection step before the process picker even loads.
answer: only the process name will show click means the table show the deatiled data
**Implemented 2026-07-13**: the rehydrate page now has its own Client + Job
selectors (mirroring the redact-upload page), and only once both are chosen
does the Process Name `<select>` load — showing just the name (+ file count)
per Section 3.4.1. Selecting a name is what reveals the files table.

**NEW-5. How much redaction detail to show before the user uploads
anything?** Once a process is selected (Section 3.4.2 step 2), should the
files table show:

- (a) Just `filename` + a count of redacted values per file — no plaintext.
- (b) The full reference table (`source_text` ↔ `redacted_text`, reusing
  `renderTable()` from the main page) — i.e. the *original* PII values are
  visible on screen as soon as a process is picked, before any file is
  uploaded or any rehydration actually happens.

(b) is more useful for the user to confirm "yes, this is the right batch"
before uploading, but it means `GET /api/redaction-map/<process_id>`
(Section 2.7) — already auth-gated only on a valid session, with no
check that *this* user's session should see *this* client's data — becomes
reachable straight from the process picker, not just after the existing
reference-table feature's own flow. Worth confirming that's acceptable
before reusing it here as-is.
answer: option A
**Implemented 2026-07-13**: the files table (populated from the existing
`GET /api/redaction-map/<process_id>`, Section 2.7) renders only `filename`
and `(f.rows || []).length` — the count of redacted values — never the
`rows` themselves, so no `source_text`/`redacted_text` plaintext reaches the
page before rehydration.

**NEW-6. Legacy processes with no `process_name`** — batches uploaded before
migration 0005 have `process_name IS NULL`. Section 3.4.1's default query
excludes them entirely from the picker. Is that acceptable (old batches
become unreachable via this UI, though `POST /rehydrate` with a raw id would
still work if that path is kept per Open Question #1), or should they still
appear in the list somehow — e.g. labelled `"(unnamed) — PRC-ABC-000012"` so
they remain pickable?
answer: don't show
**Implemented 2026-07-13**: `db.list_processes()`'s `WHERE process_name IS
NOT NULL` excludes them unconditionally — no fallback path added.

**NEW-7. Search / pagination / ordering** — as the number of processes grows
over time, should `GET /api/processes` (Section 3.4.1):

- Return everything, newest-first, in one response (simplest, fine at low
  volume)?
- Cap to the most recent N (e.g. 50) with a search-as-you-type box that
  queries by `process_name` substring — mirroring the existing job-search
  UX (`GET /api/jobs?client_id=&q=`, [app.py:1127-1137](app.py#L1127-L1137))?

Depends partly on NEW-4's answer (a client-scoped list is naturally much
smaller than a system-wide one).
answer:Since NEW-4 scopes the list to one client + job, it will be small — return everything, newest-first, for now. Add a limit (e.g. 50) as a guard rail, and if any client's list grows past that in practice, add the same search-as-you-type UX already used for jobs (GET /api/jobs?client_id=&q=). No need to build search up front.
**Implemented 2026-07-13**: `db.list_processes(..., limit=50)` — no `q` param
built yet; add one later if a client+job's process list grows past 50 in
practice.

---

## 6. Database changes needed

None identified for the original single-`file_id` design — `pii_files` and
`pii_redaction_map` already carry everything it reads. **Updated 2026-07-13**:
Section 3.0's `process_id`-scoped, conflict-aware merge still needs **no new
migration** — `pii_redaction_map` already has both `process_id` and `file_id`
columns — but does need a new **read helper**:

- `db.get_redaction_map_for_process(process_id) -> dict` — like
  `db.get_files_for_process()` ([db.py:354-370](db.py#L354-L370)), groups by
  `process_id`, but returns `{token: {source_text: [file_id, ...]}}`
  (Section 3.0.2) instead of a flattened `{token: source_text}` dict, so the
  caller can detect and report conflicts instead of silently merging them.
- A generic "find every token-looking string actually present in this
  document" scanner, one per format (xlsx/xls cell values, docx paragraph/table
  text, pdf page word-text), reusing `TOKEN_RE`
  ([redaction_formats.py:31](redaction_formats.py#L31)) — needed for Section
  3.0.2 step 1. This is a new function in `redaction_formats.py`, not a DB
  change, but it's a prerequisite for the conflict-aware merge to be usable.

This section will be further updated if Open Question NEW-1's answer (e.g.
option (b), which needs to persist "pending conflict resolution" state
between two requests) turns out to need new state to persist.

**Added 2026-07-13 for Section 3.4** — also no new migration (`process_name`
already exists as of migration 0005), but one more new read helper:

- `db.list_processes(client_id, job_id, limit=50) -> list` — **implemented**
  in [db.py](../../../db.py), groups `pii_files` by `process_id`, returns
  `{process_id, process_name, created_at, file_count}` per distinct process
  for that client+job, newest first, excluding `process_name IS NULL` rows
  (Section 3.4.1).

---

## 7. Task List

- [x] Get answers to Open Questions in Section 5, 5c, 5d, and 3.0
- [x] `db.get_redaction_map_for_process(process_id)` — conflict-preserving `{token: {source_text: [file_id,...]}}` (Section 3.0.2 / Section 6) — [db.py](../../../db.py)
- [x] Per-format "find every token-looking string present in this document" scanner (`redaction_formats.find_tokens_in_file()`), reusing `TOKEN_RE` (Section 3.0.2 step 1) — xlsx/xls cell values, docx paragraph/table text, pdf page text, `.doc` via Word-COM conversion then the same docx path
- [x] Conflict classifier: `redaction_formats.rehydrate_uploaded_file()` splits scanned+matched tokens into safe `{token: value}` vs conflicting-and-present (Section 3.0.2 step 2)
- [x] New dispatcher `redaction_formats.unmask_uploaded_file(path, output_path, mapping)`, mirroring `unmask_as_is()` but keyed off the upload's own extension, reusing the existing `unmask_xlsx_as_is`/`unmask_xls_as_is`/`unmask_docx_as_is`/`_unmask_doc`/`unmask_pdf_as_is` functions — called with the **safe** mapping only (Section 3.1 step 6)
- [x] Implement conflict handling per NEW-1's answer (option c, best-effort with warning) — conflicting tokens left as literal text; warnings returned to the caller
- [x] Backend route `POST /rehydrate/upload` (Q6: separate endpoint): accepts `{process_id, file}` multipart upload, auth-gated, resolves the batch via `db.get_files_for_process()`, builds the conflict-aware mapping, scans + classifies, calls the new dispatcher, returns the rehydrated file + `X-Rehydrate-Warnings` header if conflicts were left unresolved — [app.py](../../../app.py)
- [x] Existing file_id-only flow (Q1: not needed) — `REHYDRATE_HTML`/`POST /rehydrate` code left untouched per Q6, just no longer linked from the redesigned page
- [x] `db.list_processes(client_id, job_id, limit=50)` — grouped, newest-first, excludes `process_name IS NULL` rows (Section 6)
- [x] `GET /api/processes?client_id=&job_id=` endpoint — auth-gated, calls `db.list_processes()`
- [x] Replaced `REHYDRATE_HTML`'s single "File ID" text input with Client → Job → Process Name selectors (populated from `/api/clients`, `/api/jobs`, `/api/processes`), a files table on process selection (reusing `/api/redaction-map/<process_id>`, filename + redacted-value count only per NEW-5 option A), and the file-upload + "Rehydrate & Download" control — submit handler uses `FormData`, posts `{process_id, file}` to `/rehydrate/upload`
- [x] Client/Job scoping wired per NEW-4's answer
- [x] Legacy (`process_name IS NULL`) batches excluded per NEW-6's answer (no fallback path)
- [ ] Test: picker lists processes newest-first, correctly resolves `process_id` on selection, and the files table matches what `/api/redaction-map/<process_id>` already returns on the main results page for the same batch — **not yet manually verified end-to-end (no DB/browser access in this session)**
- [ ] Test: a process with `process_name IS NULL` (pre-migration-0005 data) simply doesn't appear in the picker — **not yet manually verified**
- [x] Unmatched-token / zero-match handling: Q4 (silently leave) and Q5 (hard error, naming client+job+token-count) both implemented in `rehydrate_uploaded_file()` / `rehydrate_upload()`
- [x] Extension allowlist enforced per Q3's answer (full list); images pass the allowlist but get a specific rejection error before scanning (see Q3's implementation note in Section 5) since there's no OCR path
- [x] No scratch-file cleanup, per Q7's answer (retention isn't a concern) — `rehydrate_uploads/`/`rehydrated/` simply accumulate
- [ ] Test: upload a document containing a subset of one file_id's tokens (not all of them) — only those present get replaced, no error — **not yet manually verified**
- [ ] Test: upload against the wrong process_id — zero-match error fires per Q5 — **not yet manually verified**
- [ ] Test: **multi-file batch, no collisions** — a process_id with several files, uploaded document draws tokens from more than one file_id, all resolve correctly — **not yet manually verified**
- [ ] Test: **multi-file batch, real collision, colliding token present in the document** — left unresolved + warned per NEW-1(c), value never silently wrong — **not yet manually verified**
- [ ] Test: **multi-file batch, real collision, colliding token NOT present in the document** — rehydration succeeds normally, unaffected by the collision elsewhere in the batch — **not yet manually verified**
- [ ] Test: each supported format round-trips correctly (xlsx, xls, docx, doc, pdf) — **not yet manually verified**
- [ ] Test: a manually-added row (`source='manual'`, from the reference-table feature) rehydrates correctly through this new path too — **not yet manually verified**
- [ ] Update [wiki/architecture.md](../../architecture.md) to document the new endpoints/tables
- [ ] Apply `migrations/0005_pii_files_process_name.sql` against the live DB if not already done, before this feature is usable

---

## 8. Out of Scope (for this plan)

- Combining files across **multiple `process_id`s** (or scoping by `job_id`
  instead) for one rehydration request — only in scope if Open Question NEW-2
  says the one-batch assumption doesn't hold.
- OCR-based token detection in scanned/image documents (see Open Question
  #3) — only in scope if explicitly requested.
- Any change to how redaction itself works (Tier 0-3 detection, token
  minting) — this plan is purely about the rehydration/download side.
- The reference-table, preview, and manual "add missing value" feature
  ([plan-table-implementation.md](plan-table-implementation.md)) — separate,
  already-implemented plan; this one only reads the same
  `pii_redaction_map` table it writes to.
- A persistent history/audit of rehydration requests (who uploaded what,
  when) — not mentioned in the request; flag if needed.
