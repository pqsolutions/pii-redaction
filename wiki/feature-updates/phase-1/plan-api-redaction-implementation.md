# Plan: Redaction Request API (bulk, caller-supplied PII values)

Status: **IMPLEMENTED 2026-07-23.** All Open Questions in Section 5 answered
and built: Option A (files must already exist, looked up by this app's own
`file_id`), category auto-detected via lightweight regex/checksum patterns
with a generic `"PII"` fallback, silent-skip on already-masked values, a new
`"too_short"` status for values under 3 characters, and processing runs async
via a background thread + `GET /stream/redact/<process_id>` SSE stream
mirroring `/upload` + `/stream/<job_id>`. See Section 7 for what shipped.
Owner: TBC
Created: 2026-07-23
Updated: 2026-07-23 — implemented.

---

## 1. Goal

Add a new API endpoint that lets an external caller submit a batch redaction
request shaped like:

```json
{
    "process_id": "PRC-TES-001",
    "files": [
        {
            "file_id": "FIL-001",
            "file_name": "WP file 01.pdf",
            "source_values": ["John", "kumar", "Covid-19"]
        },
        {
            "file_id": "FIL-002",
            "file_name": "WP file 01.xlsx",
            "source_values": ["kumar", "Covid-19"]
        }
    ]
}
```

For each file, mask every occurrence of every listed `source_values` string,
and respond with an identifier per file that the caller can later use to
download the redacted output.

This is deliberately different from the two redaction mechanisms that exist
today (Section 2): the caller already knows exactly which strings are PII —
there's no automated detection step. This is closest in shape to the existing
"add one manual value" endpoint, but bulk (many files, many values per file,
one request) rather than one value at a time from the reference-table UI.

---

## 2. Current State (what exists today)

### 2.1 Two existing redaction mechanisms, neither of which fits this shape

- **Automated pipeline** (`dual_redaction_pipeline.py`, triggered only by
  `POST /upload`) — runs OCR/regex/NER/LLM detection over an entire uploaded
  file to *discover* PII values itself, then masks whatever it finds. The
  caller never supplies which values to redact, and it always requires actual
  file bytes to be uploaded in that same request.
- **Manual single-value add** (`POST /api/redaction-map/add`,
  [app.py:2614-2669](../../../app.py#L2614-L2669)) — the closest existing
  building block. Given `{file_id, source_text, category}`:
  1. `db.get_file(file_id)` ([db.py:295](../../../db.py#L295)) looks up the
     `pii_files` row — **404 if `file_id` is unknown or has no
     `redacted_path` on disk yet.** This only succeeds for a file that has
     already been through `POST /upload` and finished pipeline processing.
  2. Checks the value isn't already masked for this file (409 if so).
  3. Mints a token via `redaction_formats.next_token_for_category()`
     ([redaction_formats.py:51](../../../redaction_formats.py#L51)) — this
     needs a **category** (e.g. `PERSON`) to build the token, e.g.
     `<*PERSON_3*>`. There is no category-free path today.
  4. `redaction_formats.mask_single_value()`
     ([redaction_formats.py:1100](../../../redaction_formats.py#L1100)) does
     a case-insensitive, whole-word find/replace of that one value in the
     already-redacted output file, dispatching by extension
     (xlsx/xls/docx/pdf/doc/image). Values under 3 characters are silently
     skipped (`apply_masking()`,
     [redaction_formats.py:81](../../../redaction_formats.py#L81)). Returns
     no match at all → 400.
  5. `db.insert_redaction_row(..., source="manual")`
     ([db.py:537](../../../db.py#L537)) records one `pii_redaction_map` row.

  **Nothing in the codebase today loops this per-value logic over a list of
  values, or over multiple files in one request.** Your new endpoint is
  essentially this operation, bulked up — Section 3.3 reuses it directly.

### 2.2 What a `file_id` requires to already exist

`pii_files` ([migrations/0001_pii_redaction_tables.sql:28-37](../../../migrations/0001_pii_redaction_tables.sql#L28-L37)):

```sql
CREATE TABLE IF NOT EXISTS pii_files (
    file_id       VARCHAR(20) NOT NULL PRIMARY KEY,
    process_id    VARCHAR(20) NOT NULL,
    client_id     INT NOT NULL,
    job_id        INT NOT NULL,
    filename      VARCHAR(255) NOT NULL,
    redacted_path VARCHAR(500),
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX (process_id)
);
```

(later migrations added `process_name`, `output_format`,
`original_encrypted_path` — see [wiki/architecture.md:367-378](../../architecture.md#L367-L378)).

Two things matter a lot for this plan:

- `client_id` and `job_id` are `NOT NULL` — every row is tied to a real
  `project.Pid` / `activity.Aid`. Your example payload has **no client/job
  info at all.**
- `file_id` and `process_id` today are **always minted by this server**, via
  `db.next_id("FIL"|"PRC", client_code)`
  ([db.py:243](../../../db.py#L243)), only from inside `POST /upload`. The
  format is always `{PREFIX}-{3-LETTER-CODE}-{6-digit-number}`, e.g.
  `PRC-ABC-000120` / `FIL-ABC-100001` — never caller-chosen, and never as
  short as your example's `PRC-TES-001` / `FIL-001`. This is a real
  divergence from today's ID scheme, flagged in Open Question #2.

### 2.3 The download side is already solved

`GET /download/id/<file_id>` (new, untracked —
[docs/swagger/download_by_id.yml](../../../docs/swagger/download_by_id.yml))
looks up `db.get_file(file_id).redacted_path` and streams it back, gated by
`_require_uid()`. Whatever this new endpoint returns as a file identifier
should be a real `pii_files.file_id` so this existing route can serve it with
zero new code — see Section 3.6.

---

## 3. Proposed Design

### 3.1 The central fork: where do the actual file bytes come from? — **decided: Option A** (Section 5, Q1)

Your example JSON carries **no file content** — only `file_id`, `file_name`,
and `source_values`. That's ambiguous between two real designs:

**Option A — files must already exist in this system.**
The caller first uploads files the normal way (`POST /upload`, or a future
plain "register a file" endpoint), gets back real `file_id`s, and *then*
calls this new endpoint with those `file_id`s + the literal values to mask.
This endpoint becomes a pure bulk-loop over the existing
`/api/redaction-map/add` logic (Section 3.3) — smallest possible change,
reuses the schema and functions as-is, no new upload path.
*Downside:* the caller must already know this app's internally-minted
`file_id`s, which means an extra round trip (upload, read back the response,
then call this endpoint) — doesn't match a caller who wants to hand over
`file_name` + values in one shot without a prior upload step.

**Option B — this endpoint accepts the files itself, in the same request.**
`multipart/form-data`: a JSON field (your payload shape) plus the actual file
attachments alongside it (matched to `files[].file_id` by field name or
`file_name`), similar to how `POST /upload` already takes files
([app.py:2049](../../../app.py#L2049)). The server saves each file, mints
(or accepts, per Open Question #2) IDs, and masks the given `source_values`
directly — no ML pipeline runs at all, this is a "register + directly mask
known values" flow, parallel to but independent from `/upload`'s "register +
auto-detect" flow.
*Downside:* materially more code — new file-save logic, new `pii_files`
row-creation path outside `/upload`, and it needs `client_id`/`job_id` from
somewhere (Section 3.4) since the schema requires them today.

**Decision:** Option A. Files must already exist in this system (already
uploaded via `POST /upload`, already through the pipeline, with a
`redacted_path` on disk) before this new endpoint can be called — it looks
files up by `file_id`, it never accepts raw bytes itself. Sections 3.2
onward are written against this.

### 3.2 ID handling — **decided: this app's own minted IDs** (Section 5, Q2)

`process_id`/`file_id` in the request are simply **looked up**, not minted —
same as `/api/redaction-map/add` does today (`db.get_file(file_id)`). Your
example IDs (`PRC-TES-001`, `FIL-001`) were confirmed to be illustrative
placeholders only — in practice the caller passes back this app's real minted
IDs, e.g. `FIL-AC6-100133` / `PRC-AC6-000122`, matching the
`{PREFIX}-{CODE}-{NNNNNN}` format from `db.next_id()`
([db.py:243](../../../db.py#L243)). **No external-ID mapping columns
needed** — the Section 6 external-ID migration is dropped.

### 3.3 Masking mechanics — reuse the existing single-value flow, looped

Assuming Option A, for each `{file_id, source_values}` in the request:

```python
file_row = db.get_file(file_id)
if not file_row or not file_row.get("redacted_path"):
    # record as failed for this file, continue with the rest of the request
    ...
redacted_path = Path(file_row["redacted_path"])
existing_rows = db.get_redaction_rows(file_id)
results = []
for source_text in source_values:
    if any(r["source_text"].strip().lower() == source_text.strip().lower() for r in existing_rows):
        continue  # already masked — silent skip, no entry in the response (Q5)
    if len(source_text.strip()) < 3:
        results.append({"source_value": source_text, "status": "too_short"})  # Q8
        continue
    category = redaction_formats.guess_category(source_text)  # new — Section 3.4 (Q3)
    token = redaction_formats.next_token_for_category(
        [r["redacted_text"] for r in existing_rows], category
    )
    events = redaction_formats.mask_single_value(redacted_path, source_text, token)
    if not events:
        results.append({"source_value": source_text, "status": "not_found"})
        continue
    db.insert_redaction_row(
        file_row["client_id"], file_row["job_id"], file_row["process_id"], file_id,
        source_text, token, source="api",   # new source value — Section 6
    )
    existing_rows.append({"source_text": source_text, "redacted_text": token})
    results.append({"source_value": source_text, "status": "redacted", "token": token})
```

This is a direct, per-file, per-value reuse of
`app_redaction_map_add`'s body — no new masking code needed, just a loop
around it plus multi-file handling and per-item status collection instead of
failing the whole request on the first miss (Section 3.5).

### 3.4 Category for the token — **decided: lightweight regex auto-detect, generic fallback** (Section 5, Q3)

`next_token_for_category()` needs a `category` string to build
`<*CATEGORY_N*>`. The request never carries one — `guess_category(value)` (new,
`redaction_formats.py`) auto-detects it:

- Reuses this app's existing structured regex/checksum patterns —
  `TFN_PAT`, `ABN_PAT`, `ACN_PAT`, `MEDICARE_PAT`, `PHONE_PAT`, `EMAIL_PAT`,
  `BSB_ACCOUNT_PAT` ([dual_redaction_pipeline.py:980-997](../../../dual_redaction_pipeline.py#L980-L997))
  — **duplicated** into `redaction_formats.py` (a plain regex check, not an
  import of the heavy pipeline module — matches how `redaction_formats.py`'s
  docstring already explains it deliberately avoids Docling/GLiNER/Presidio/cv2
  for exactly this reason), checked in a fixed priority order against the
  value. First match wins → that pattern's category (`TFN`, `ABN`, `MEDICARE`,
  `PHONE`, `EMAIL`, …).
- **Nothing matches → generic fallback category, `"PII"`** (token prefix
  `<*PII_N*>`). This is the common case for free text — confirmed explicitly:
  none of your example values (`John`, `kumar`, `Covid-19`) are structured
  numeric identifiers, so **all three would land in the `PII` fallback
  bucket** under this design. True per-value semantic categories (PERSON vs
  DISEASE vs ORG) for free text needs real NER (GLiNER) — deliberately not
  done here, since loading that into the always-on Flask process adds
  permanent memory/startup cost and per-request inference latency; out of
  scope for this plan (Section 8), can be revisited later if per-value
  category accuracy on free text actually matters downstream.

### 3.5 Partial failures — per-file/per-value status, not all-or-nothing

**Decided: `200` (well, the SSE `"done"` event, per Section 3.7) with a
structured per-item outcome** — `redacted` / `not_found` / `too_short` /
file-level `unknown_file_id`. Already-masked values are not an "outcome" at
all — they're silently skipped, no entry (Section 5, Q5). Nothing about one
bad file or one unmatched value fails the request as a whole (Section 5, Q6).

### 3.6 Response shape (final result payload — carried in the SSE `"done"` event, Section 3.7)

```json
{
    "process_id": "PRC-AC6-000122",
    "files": [
        {
            "file_id": "FIL-AC6-100133",
            "file_name": "WP file 01.pdf",
            "status": "completed",
            "values": [
                {"source_value": "John", "status": "redacted", "category": "PII", "token": "<*PII_1*>"},
                {"source_value": "kumar", "status": "redacted", "category": "PII", "token": "<*PII_2*>"},
                {"source_value": "Covid-19", "status": "not_found"}
            ]
        },
        {
            "file_id": "FIL-AC6-100134",
            "file_name": "WP file 01.xlsx",
            "status": "completed",
            "values": [
                {"source_value": "Covid-19", "status": "redacted", "category": "PII", "token": "<*PII_3*>"}
                // "kumar" already masked from the first file's request above
                // (dedup is per-file, not global — see note below) is omitted
                // entirely here — silent skip, Q5
            ]
        }
    ]
}
```

(Note: dedup against `existing_rows` in Section 3.3 is scoped to one
`file_id` at a time, matching `/api/redaction-map/add`'s existing behavior —
"already masked" only skips a value that was *already redacted in that same
file*, not across the whole process. The comment above is illustrative only,
not a real double-request scenario.)

Each `file_id` returned is a real `pii_files.file_id`, directly usable with
the existing `GET /download/id/<file_id>` (Section 2.3) — no separate
"redacted file id" needed, it's the same id. `category` is included per
redacted value (Section 3.4) so the caller can see what this endpoint
inferred, even though it wasn't supplied in the request.

### 3.7 Async processing + SSE — **decided** (Section 5, Q7)

Mirrors `POST /upload` + `GET /stream/<job_id>` exactly, since the caller's
own tooling already knows how to consume that pattern:

1. `POST /api/redact` validates the request shape, spawns a background
   thread to run the per-file/per-value loop (Section 3.3), and returns
   immediately:
   ```json
   {"process_id": "PRC-AC6-000122", "stream_url": "/stream/redact/PRC-AC6-000122"}
   ```
2. Caller opens `GET /stream/redact/<process_id>` (new SSE route, same
   queue/`Response(..., mimetype="text/event-stream")` mechanism as
   `/stream/<job_id>`). One event is emitted **per file** as its value-loop
   finishes (event `file_done`, payload = that file's entry from Section
   3.6), so the caller gets incremental progress across a multi-file batch
   instead of waiting for all files to finish.
3. A final `done` event carries the complete Section 3.6 payload (all files,
   for callers that only want the aggregate result and don't care about the
   incremental `file_done` events).

No synchronous response body beyond the initial `{process_id, stream_url}` —
the actual redaction result is only available via the stream. This keeps a
single way to consume the result (matches Section 5 Q7's "Background thread +
SSE stream" answer, not the sync-plus-optional-SSE alternative).

### 3.8 Auth

Recommend the same `_require_uid()` gate every other `/api/*` route uses
(session **or** valid `X-API-Key`,
[plan-api-key-implementation.md](plan-api-key-implementation.md)) — this is
explicitly a server-to-server style payload, so in practice it'll always be
called with `X-API-Key`, but there's no reason to special-case it out of the
existing gate.

---

## 4. Data flow diagram (decided design)

```
External caller
    │  POST /api/redact  (JSON body — no file bytes, Option A)
    │  { process_id, files: [{file_id, file_name, source_values}, ...] }
    ▼
_require_uid()  (session OR X-API-Key)
    fail → 401/403
    ▼
validate shape → spawn background thread → respond immediately:
    202 {"process_id": ..., "stream_url": "/stream/redact/<process_id>"}

Caller opens  GET /stream/redact/<process_id>  (SSE, new route)
    │
    ▼                          [background thread, same request lifecycle]
for each file in request.files:
    │
    ├─ db.get_file(file_id)
    │      not found / no redacted_path  → file-level "unknown_file_id"
    │
    ├─ db.get_redaction_rows(file_id)   (existing masked values, for dedup + token numbering)
    │
    ├─ for each source_value:
    │      already masked (case-insensitive)     → silent skip, no entry (Q5)
    │      len(value) < 3                        → "too_short" (Q8)
    │      redaction_formats.guess_category(value)  → category (regex match or "PII" fallback)
    │      next_token_for_category(existing_tokens, category)
    │      redaction_formats.mask_single_value(redacted_path, source_value, token)
    │          no match found in file  → "not_found"
    │          match found  → db.insert_redaction_row(..., source="api")
    │                          → "redacted", category + token returned
    │
    ├─ emit SSE event  "file_done"  { file_id, file_name, status, values: [...] }
    ▼
emit SSE event  "done"  { process_id, files: [ ... all files ... ] }   (Section 3.6)
    │
    ▼  (separately, whenever the caller wants the bytes)
GET /download/id/<file_id>   (existing route, unchanged, Section 2.3)
```

---

## 5. Open Questions (need your answers)

1. **Where do the file bytes come from?** (a) files must already exist in
   this system (already uploaded via `POST /upload` beforehand, this
   endpoint only looks them up by `file_id`) — smaller, reuses existing code
   (my recommendation), or (b) this endpoint accepts the file bytes itself in
   the same request (multipart, files + JSON together) and does its own
   intake, independent of `/upload`? Section 3.1. **This decides the whole
   shape of the feature — please answer first.**
   answer:option A
   **Decision:** Option A — this endpoint only looks files up by `file_id`;
   it never accepts raw file bytes. Files must already exist via
   `POST /upload`.

2. **Are `process_id`/`file_id` this app's own minted IDs, or an external
   system's own IDs** that don't match this app's `{PREFIX}-{CODE}-{NNNNNN}`
   format? If external, we need new mapping columns
   (`pii_files.external_file_id` etc.) — if they're just illustrative
   placeholders for this app's real IDs, no schema change needed here.
   Section 3.2.
   answer: same ids example FIL-AC6-100133 / PRC-AC6-000122 format.
   **Decision:** this app's own minted IDs, `{PREFIX}-{CODE}-{NNNNNN}`. No
   external-ID mapping columns — the request's `process_id`/`file_id` are
   looked up directly via `db.get_file(file_id)`.

3. **Category per redacted value** — should the request optionally carry a
   `category` per `source_value` (falls back to a default if omitted), or
   should every value redacted through this endpoint always use one fixed
   generic category/token prefix? Section 3.4.
   answer: the category not get from request so  do it auto-detect
   **Decision:** auto-detect via lightweight regex/checksum patterns
   (`redaction_formats.guess_category()`, Section 3.4), duplicated from
   `dual_redaction_pipeline.py`'s existing `TFN_PAT`/`ABN_PAT`/etc — no new
   ML dependency. Falls back to a generic `"PII"` category when nothing
   matches, which will be the outcome for most free-text values (confirmed:
   all three of your example values fall into this bucket, since none are
   structured numeric IDs). Full NER-based detection (GLiNER) for
   PERSON/DISEASE/ORG-level accuracy on free text was considered and
   explicitly deferred — see Section 8.

4. **Do `client_id`/`job_id` need to be in this request at all?** Under
   Option A (Section 3.1), they're already stored on the `pii_files` row from
   the original upload, so `db.get_file(file_id)` already returns them — no,
   they shouldn't need to be re-supplied. Confirming this isn't a case where
   the caller expects this endpoint to work independently of the existing
   client/job model. Section 2.2.
   answer:Correct — client_id/job_id not need in request
   **Decision:** confirmed, no change from Section 3.3/3.4 — `client_id`/
   `job_id` come from `db.get_file(file_id)`, never from the request body.

5. **What happens on a repeat call** with the same `file_id` + a
   `source_value` that was already redacted by a previous call (or by the
   original pipeline run)? Section 3.3 treats this as a no-op
   (`"already_masked"`, no error) — confirm that's right, versus e.g.
   wanting it treated as an error, or wanting it silently skipped with no
   entry in the response at all.
   answer:	Silent skip
   **Decision:** silent skip — an already-masked value produces no entry at
   all in that file's `values` list (Section 3.3/3.6), not an
   `"already_masked"` status.

6. **Failure semantics** — should the HTTP response always be `200` with
   per-file/per-value status flags (Section 3.5, my recommendation), or
   should the whole request fail (non-2xx) if *any* file is unknown or *any*
   value isn't found anywhere in its file?
   answer:Option A (	200 + per-item status)
   **Decision:** confirmed — no file/value failure fails the whole request;
   status is always per-item (Section 3.5). (In the async/SSE design that
   followed from Q7, this status now arrives via the `"done"`/`"file_done"`
   SSE events rather than a synchronous `200` body, but the never-hard-fail
   principle is unchanged.)

7. **Expected batch size** — roughly how many files / values per request do
   you expect in practice? Affects whether synchronous handling (Section 3.7)
   is fine or whether this needs the same background-thread + polling/SSE
   pattern `/upload` uses for large batches.
   answer:the external tool already has its own SSE for its own processing and showing responses .. can we use SSE for this endpoint?
   **Decision** (follow-up asked and answered): yes — background thread +
   SSE stream, mirroring `/upload` + `/stream/<job_id>` exactly (Section
   3.7). `POST /api/redact` returns immediately with `{process_id,
   stream_url}`; `GET /stream/redact/<process_id>` emits one `file_done`
   event per completed file plus a final `done` event with the full result.
   This was chosen over a plain synchronous response and over "both sync +
   optional SSE" specifically because the caller's own tooling already
   consumes this exact SSE pattern for `/upload` — reusing the same shape
   means no new integration pattern on their side.

8. **Does a value need to match a whole word only** (today's behavior —
   `\bvalue\b`, case-insensitive, minimum 3 characters — see
   [redaction_formats.py:81-83](../../../redaction_formats.py#L81-L83)), or
   do you need substring/partial matching for any of your `source_values`
   (e.g. would "Covid-19" need to match inside "Covid-19-related" too)? The
   existing whole-word regex behavior is reused as-is unless you say
   otherwise.
   answer:Keep whole-word matching — reuse the existing \b behavior as-is. Substring matching would cause over-redaction (Ann matching inside Annual, Ltd inside Ltda), which is a real risk with the kinds of short values that appear in these documents. It also keeps this endpoint consistent with the pipeline's own auto-detection, which uses the same behavior — having two different matching rules produce tokens in the same file would be confusing.

One related point worth handling explicitly: the existing minimum of 3 characters carries over too, so 2-character values (initials like "JS", state codes like "NT") will never match. Please return that as a distinct per-value status in the response — something like "status": "too_short" — rather than letting it fall through as a generic "not found". Otherwise it's the same silent-failure pattern we've been fixing elsewhere: the caller gets told nothing was found and has no way to know the value was rejected before the search even ran.
   **Decision:** whole-word, case-insensitive matching only (existing
   `apply_masking()` regex behavior, unchanged). Values under 3 characters
   are checked and rejected *before* the search runs, with a dedicated
   `"too_short"` status (Section 3.3/3.6) — not folded into `"not_found"`.

---

## 6. Database changes needed

- **`pii_redaction_map.source` enum** currently `ENUM('pipeline', 'manual')`
  ([migrations/0004_pii_redaction_map_source.sql:7](../../../migrations/0004_pii_redaction_map_source.sql#L7)).
  New migration to add a third value:
  `ALTER TABLE pii_redaction_map MODIFY source ENUM('pipeline','manual','api') NOT NULL DEFAULT 'pipeline';`
  — lets the reference-table UI and any future audit distinguish
  API-originated redactions from pipeline/manual ones.
- No external-ID mapping columns — dropped per Q2's decision (Section 3.2),
  since `process_id`/`file_id` are this app's own minted IDs.
- No other schema changes — `pii_files` and `pii_redaction_map` already carry
  everything else needed; `guess_category()` (Section 3.4) is pure Python,
  no new table.

---

## 7. Task List

- [x] Get answers to Open Questions in Section 5
- [x] Migration: extend `pii_redaction_map.source` enum with `'api'`
      (`migrations/0007_pii_redaction_map_source_api.sql`, Section 6)
- [x] Implement `redaction_formats.guess_category(value)` — duplicated
      regex/checksum patterns (`TFN`, `ABN`, `ACN`, `MEDICARE`, `PHONE`,
      `EMAIL`, `BSB_ACCOUNT`) + `"PII"` generic fallback (Section 3.4)
- [x] Implement `POST /api/redact` in `app.py` — `_require_uid()` gate,
      validates request shape, spawns background thread, returns
      `{process_id, stream_url}` immediately (Section 3.7)
- [x] Background thread (`_run_redact`): per-file/per-value loop reusing
      `redaction_formats.guess_category()` +
      `redaction_formats.next_token_for_category()` +
      `redaction_formats.mask_single_value()` + `db.insert_redaction_row()`,
      with silent-skip on already-masked and `"too_short"` on values under 3
      characters (Section 3.3)
- [x] Implement `GET /stream/redact/<process_id>` SSE route — `file_done`
      event per completed file, final `done` event with the full Section 3.6
      payload (mirrors `/stream/<job_id>`'s queue/`text/event-stream`
      mechanism, kept in a separate `_redact_queues` dict)
- [x] Add `docs/swagger/redact_request.yml` + `docs/swagger/redact_stream.yml`,
      wired `@swag_from` on both new routes, `security: [ApiKeyAuth]`
- [x] Updated [wiki/architecture.md](../../architecture.md) with the new
      endpoints and their place relative to the existing pipeline/manual-add
      flows
- [x] Ran `migrations/0007_pii_redaction_map_source_api.sql` against the
      target MySQL database
- [x] Test: multi-file, multi-value request — all values found and masked,
      `file_done` events arrive per file before the final `done` event
- [x] Test: value not present in file → `"not_found"`, rest of the request
      still succeeds
- [x] Test: value under 3 characters → `"too_short"`, not `"not_found"`
- [x] Test: unknown `file_id` → file-level `"unknown_file_id"`, rest of the
      request still succeeds
- [x] Test: repeat call with an already-masked value → no entry in the
      response, no duplicate `pii_redaction_map` row (silent skip)
- [x] Test: a TFN/ABN/email/phone-shaped value gets its matching category;
      a free-text value (e.g. "John") gets the `"PII"` fallback
- [x] Test: redacted values show up correctly in the existing
      `GET /api/redaction-map/<process_id>` and `GET /api/preview/<file_id>`
      views (since this reuses the same table/rows those already read)
- [x] Test: file downloadable afterwards via existing
      `GET /download/id/<file_id>`
- [x] Test: no session/no API key → 403; wrong API key → 401

---

## 8. Out of Scope (for this plan)

- Any automated PII *detection* of new values — this endpoint only masks
  values the caller already knows about, by exact string match. No OCR/NER/
  LLM involved in finding *what* to redact.
- **Full NER-based category detection** (GLiNER) for free-text values —
  explicitly considered and declined for this plan (Section 3.4/5 Q3).
  `guess_category()` only recognizes structured, regex-matchable identifiers;
  everything else gets the generic `"PII"` category. Revisit only if
  per-value category accuracy on free text becomes a real downstream need.
- Accepting file bytes directly in this endpoint (Option B, Section 3.1) —
  declined; files must already exist via `POST /upload`.
- External-system ID mapping (Section 3.2) — declined; this app's own minted
  IDs are used throughout.
- Rehydration-specific work — since this reuses `pii_redaction_map` rows the
  same way `/api/redaction-map/add` does, the existing `/rehydrate` /
  `/rehydrate/upload` flows should pick these rows up automatically with no
  extra code, but confirming that end-to-end is a test task (Section 7), not
  new design.
- Bulk/zip download of multiple redacted files in one response — the caller
  downloads each `file_id` individually via the existing
  `GET /download/id/<file_id>`, matching today's single-file-per-request
  download behavior.
- Rate limiting / abuse protection on the new endpoint.
- Partial/substring matching, fuzzy matching, or regex-based `source_values`
  — plain literal whole-word matching only, per today's
  `redaction_formats.apply_masking()` behavior (Open Question #8).
