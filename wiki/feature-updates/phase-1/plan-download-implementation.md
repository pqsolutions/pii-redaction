# Plan: file_id-based Download (disambiguating same-named files)

Status: **IMPLEMENTED 2026-07-22.** All Open Questions in Section 5 resolved:
additive `GET /download/id/<file_id>` route (Option A), auth-gated with
`_require_uid()`, existing `/download/<filename>` kept permanently (not
deprecated — both are supported long-term), root-cause storage-collision fix
(Q4) and download-filename disambiguation (Q5) explicitly declined as out of
scope for this plan. See Section 5 for each answer and Section 7 for what
shipped.
Owner: TBC
Created: 2026-07-22
Updated: 2026-07-22 — implemented.

---

## 1. Goal

Today, `GET /download/<filename>` identifies the file to send purely by its
on-disk **filename**. If two different uploads happen to produce a redacted
output with the same filename — e.g. two different clients each upload a file
called `Invoice.pdf` — there is no way to ask for "the one from *this*
process/client/job" through this route; whichever file currently sits at that
path is what gets served.

The fix is to make **`file_id`** — which already exists as the unique
per-file primary key everywhere else in this app (`pii_files.file_id`, used by
`/api/preview/<file_id>`, `POST /rehydrate`, `/api/redaction-map/add`, etc.) —
the thing `/download` identifies a file by, instead of (or in addition to) its
filename.

This plan covers *only* the download-identity question. Section 2.2 below
surfaces a related, deeper finding from investigating this — same-name
collisions aren't only a download-lookup problem, they can affect which bytes
get redacted in the first place — that was raised as Open Question #4 and
explicitly declined (Section 5, Q4/Q5): the new endpoint's purpose is simply
to give API callers a way to fetch a redacted file by `file_id`, on the
understanding that two files won't collide on the same `redacted_path` in
practice. The root-cause namespacing fix stays out of scope for this plan.

---

## 2. Current State (what exists today)

### 2.1 `/download/<filename>` — filename is the only key, and there's no auth

```python
@app.route("/download/<filename>")
@swag_from("docs/swagger/download.yml")
def download(filename: str):
    path = OUTPUT_DIR / secure_filename(filename)
    if not path.exists():
        return "File not found", 404
    return send_file(str(path), as_attachment=True)
```
([app.py:2298-2304](../../../app.py#L2298-L2304))

- `OUTPUT_DIR` is `files/output/redacted/` ([app.py:27-28](../../../app.py#L27-L28))
  — **one flat directory shared by every client, job, and process.** Nothing
  in the path is scoped by `client_id`/`job_id`/`process_id`/`file_id`.
- There is **no auth gate at all** on this route (already flagged as a gap in
  [plan-api-key-implementation.md Section 2.1](plan-api-key-implementation.md#L58-L64)
  — every other file-level route (`/api/preview/<file_id>`, `POST
  /rehydrate`) requires `_require_uid()`; `/download` doesn't).
- The only caller today is this app's own frontend
  ([app.py:947](../../../app.py#L947)):
  `href="/download/${encodeURIComponent(f)}"`, where `f` is a bare filename
  string with no `file_id` attached (see 2.3).

### 2.2 The collision isn't only a download-time problem — found while investigating

Every stage of the pipeline writes into a **flat directory keyed only by the
original filename's stem**, shared across all clients/jobs/processes:

| Stage | Directory | Filename pattern |
|---|---|---|
| Uploaded input | `files/` (`FILES_DIR` / `INPUT_FOLDER`) | `{original_name}` |
| Raw OCR/extract | `files/output/raw/` | `{stem}_raw.xlsx` |
| Redacted output | `files/output/redacted/` | `{stem}_redacted{ext}` |
| Audit log | `audit/` | `pii_audit_log_{stem}.xlsx` |
| Unmasked (rehydrated) | `unmasked/` | `{stem}_unmasked{ext}` |

Two consequences beyond "which one does `/download` serve":

- **Input overwrite risk.** `POST /upload` saves straight to
  `FILES_DIR / name` with no per-process subfolder
  ([app.py:2077](../../../app.py#L2077)). If a second upload with the same
  original filename lands while an earlier one with that name is still being
  processed, the second `.save()` overwrites the first upload's input file
  on disk mid-run.
- **Pipeline "resume-safe" reuse.** `dual_redaction_pipeline.py` treats any
  existing `{stem}_redacted.*` file as proof "this file was already
  processed" and **skips redaction entirely**
  ([dual_redaction_pipeline.py:3863](../../../dual_redaction_pipeline.py#L3863),
  [:3899](../../../dual_redaction_pipeline.py#L3899)). So if Client A's
  process already produced `Invoice_redacted.xlsx`, and Client B later
  uploads a differently-scoped `Invoice.pdf`, the pipeline can silently skip
  producing Client B's own redacted output and leave Client A's file (or
  reuse its already-existing raw/redacted files) in its place.
- Consequently: **two distinct `pii_files.file_id` rows can end up pointing
  at the exact same `redacted_path` string**, because that path is computed
  purely from the filename stem
  ([app.py:2100](../../../app.py#L2100): `redacted_path = str(OUTPUT_DIR /
  f"{stem}_redacted{placeholder_ext}")`) — not from `file_id`. This matters
  for this plan specifically: **switching `/download` to key off `file_id`
  does not, by itself, guarantee two different `file_id`s serve two
  different files**, if their underlying `redacted_path` was already the
  same string on disk. See Open Question #4.

### 2.3 `file_id` already exists and is already used the same way elsewhere

- `db.get_file(file_id)` already returns `redacted_path`, `filename`,
  `output_format`, `process_id`, `client_id`, `job_id` for a given `file_id`
  ([db.py:295](../../../db.py#L295)) — no schema change needed to look a file
  up this way.
- `POST /rehydrate` (not `/rehydrate/upload`) **already** takes `file_id` as
  its identifier, not filename
  ([app.py:2320](../../../app.py#L2320): `file_id =
  request.form.get("file_id", "").strip()`) — this plan's direction matches
  that existing precedent rather than inventing a new one.
- `GET /api/redaction-map/<process_id>` already returns `file_id` alongside
  `filename` for every file in a process
  ([app.py:2519-2528](../../../app.py#L2519-L2528)) — the frontend already
  has this data available via `loadResultDetail(job_id)`
  ([app.py:970-975](../../../app.py#L970-L975)), it's just not wired to the
  download buttons.
- The upload result list (`showResults()`,
  [app.py:930-954](../../../app.py#L930-L954)) currently builds download
  links from **bare filenames only** — the SSE `"done"` event payload
  (`{"success": ..., "output_files": new_files}`,
  [app.py:2262](../../../app.py#L2262)) is a plain before/after directory
  diff with no `file_id`. The server *does* already compute a `file_id →
  redacted_filename` mapping for this exact batch
  (`output_by_file_id`, [app.py:2237-2247](../../../app.py#L2237-L2247)) —
  it's just used only to patch MySQL today, not sent to the browser.

---

## 3. Proposed Design

### 3.1 Route shape — **decided: Option A** (Section 5, Q1)

**Option A — new, additive route: `GET /download/id/<file_id>`.**
Keep `GET /download/<filename>` exactly as it is today (nothing breaks for
any existing caller); add a new route that looks the file up by `file_id`:

```python
@app.route("/download/id/<file_id>")
@swag_from("docs/swagger/download_by_id.yml")
def download_by_id(file_id: str):
    auth_err = _require_uid()          # required — Section 5, Q3
    if auth_err:
        return auth_err
    file_row = db.get_file(file_id)
    if not file_row or not file_row.get("redacted_path"):
        return jsonify({"error": f"Unknown file_id: {file_id}"}), 404
    path = Path(file_row["redacted_path"])
    if not path.exists():
        return jsonify({"error": "Redacted output not found on disk"}), 404
    return send_file(str(path), as_attachment=True, download_name=path.name)
```

Same shape as `/api/preview/<file_id>` and `/rehydrate` POST's existing
`file_id` lookups — no new pattern introduced. Frontend switches to this URL
once `file_id` is threaded through (Section 3.3); `/download/<filename>` is
kept permanently alongside it — not deprecated, both stay supported
long-term (Section 5, Q2).

**Option B — replace `/download/<filename>` outright**, reinterpreting the
existing path segment as `file_id` instead of filename. One route instead of
two, but it's a breaking change for anyone who already has the old
filename-shaped URL saved/scripted (Open Question #6), and the Swagger doc's
meaning changes under the same path.

**Option C — keep filename as primary, add `?file_id=` as an optional
disambiguator/validation** on the existing route. Doesn't actually remove the
ambiguity (still filename-first, `file_id` just double-checks), so **not
recommended** — listed for completeness.

### 3.2 Auth on the new route — **decided: required** (Section 5, Q3)

Every other `file_id`-scoped route (`/api/preview/<file_id>`,
`/api/preview/<file_id>/ocr-region`, `POST /rehydrate`) requires
`_require_uid()` (session **or** valid `X-API-Key`, per
[plan-api-key-implementation.md](plan-api-key-implementation.md)).
`GET /download/id/<file_id>` gets the same gate — session **or** a valid
`X-API-Key` header. `/download/<filename>` is untouched and stays
ungated, exactly as it is today (Q2 — it's being kept, not folded into the
new route's behavior).

### 3.3 Wiring `file_id` into the frontend's download buttons

`output_by_file_id` ([app.py:2237-2247](../../../app.py#L2237-L2247)) is
already computed server-side for this exact batch of files — the smallest
change is to ride it along in the SSE `"done"` event instead of only using it
to patch MySQL:

```python
q.put(("done", {"success": success, "output_files": new_files,
                 "files": [{"file_id": fid, "filename": name}
                           for fid, name in output_by_file_id.items()]}))
```

`showResults()` ([app.py:930-954](../../../app.py#L930-L954)) then builds
`href="/download/id/${file_id}"` instead of
`href="/download/${encodeURIComponent(f)}"`. (`loadResultDetail(job_id)`
already fetches the same `file_id ↔ filename` mapping a moment later via
`/api/redaction-map/<process_id>` — this just makes it available immediately
when the "done" event first renders the list, without waiting for that second
fetch.)

### 3.4 Download filename shown to the user — **decided: no change** (Section 5, Q5)

`send_file(..., download_name=path.name)` uses the redacted file's actual
on-disk name as-is, unmodified. Per Q4/Q5's answers, two files aren't
expected to collide on `redacted_path` in practice, and this endpoint is
purely an API-facing way to fetch a file by `file_id` — no client/process
disambiguation is added to the downloaded filename.

---

## 4. Data flow diagram (proposed, Option A)

```
Upload result list (browser)
    │
    │  SSE "done" event now carries file_id per output file (Section 3.3)
    ▼
showResults() renders  →  href="/download/id/<file_id>"
    │
    ▼
GET /download/id/<file_id>
    │
    ├─ _require_uid()  (session OR X-API-Key — required, Q3)
    │       fail → 401/403
    │
    ▼
db.get_file(file_id)  →  { redacted_path, filename, process_id, client_id, job_id, ... }
    │
    ├─ not found        → 404 {"error": "Unknown file_id: ..."}
    ├─ path missing      → 404 {"error": "Redacted output not found on disk"}
    │
    ▼
send_file(redacted_path, download_name=path.name)   ← exact file for this
                                                        file_id, unmodified
                                                        filename (Q5)

GET /download/<filename>   (unchanged, kept permanently per Q2 — both
                             routes are supported long-term, not a
                             transitional/deprecated pair)
    │
    ▼
OUTPUT_DIR / secure_filename(filename)   ← same as today, ungated, for
                                            whichever caller still uses it
```

---

## 5. Open Questions (need your answers)

1. **Route shape** — (a) new additive `/download/id/<file_id>` route,
   old `/download/<filename>` kept as-is (my recommendation), (b) replace
   `/download/<filename>` outright so the path segment becomes `file_id`
   (breaking change), or (c) keep filename as primary with an optional
   `?file_id=` disambiguator (doesn't really solve the problem)? Section 3.1.
   answer: option a
   **Decision**: new additive route `GET /download/id/<file_id>`
   ([app.py](../../../app.py)), `GET /download/<filename>` left untouched.

2. **Lifetime of the old `/download/<filename>` route**, if (1a) is chosen —
   kept indefinitely as a second supported way in, kept but marked
   deprecated in Swagger, or removed once the frontend (Section 3.3) is
   confirmed switched over to the new route? Section 3.1.
   answer: we don't need to depricate old /download/<filename> endpoint, we need this also in the app.
   **Decision**: both routes are permanent, parallel ways to download a
   redacted file — `/download/<filename>` for the existing browser flow,
   `/download/id/<file_id>` for `file_id`-based/API access. No deprecation,
   no removal task.

3. **Auth on the new route** — should `/download/id/<file_id>` require
   `_require_uid()` (session or API key), matching every other `file_id`
   -scoped route, or stay open with no gate like `/download/<filename>` does
   today? Section 3.2.
   answer: we require auth in new route session or api key
   **Decision**: `GET /download/id/<file_id>` calls `_require_uid()` —
   session-uid or valid `X-API-Key`, same gate as `/api/preview/<file_id>`
   and `POST /rehydrate`. `/download/<filename>` stays ungated (unchanged).

4. **Root-cause storage collision (Section 2.2)** — do you want this plan to
   also fix *why* two files can end up sharing a `redacted_path` in the first
   place (would mean namespacing `files/`, `files/output/raw/`,
   `files/output/redacted/`, `audit/`, `unmasked/` by `file_id` or
   `process_id` instead of by filename stem — touches
   `dual_redaction_pipeline.py`'s resume-safe skip logic and every downstream
   reader of those paths: rehydrate, preview, audit), or should that be its
   own follow-up plan, with *this* plan scoped strictly to disambiguating the
   `/download` API surface on top of whatever `redacted_path` is currently
   stored per `file_id`? **This materially changes how big this effort is** —
   flagging it clearly rather than assuming either answer.
   answer: two files technically cannot be in same path. we need the new endpoint so that we should have a way to download redacted file using id from api
   **Decision**: declined — no change to `dual_redaction_pipeline.py`'s
   path-naming or the flat `files/`/`raw/`/`redacted/`/`audit/`/`unmasked/`
   directories. This plan is scoped to adding the `file_id`-keyed download
   endpoint only (Section 8, Out of Scope).

5. **Download filename shown to the user** — is the redacted file's own name
   fine as the downloaded attachment name even when two files share it, or
   should it be adjusted so two separately-downloaded files are visually
   distinguishable in the user's Downloads folder too? Section 3.4.
   answer: same as abover answer, this new endpoint is only for api way of downloading files. 2 files cannot and will not exists in same path.
   **Decision**: declined — `download_name` stays the redacted file's own
   name, no adjustment (Section 3.4).

6. **Any external caller of today's `/download/<filename>`?** — as far as I
   can find, the only caller is this app's own frontend
   ([app.py:947](../../../app.py#L947)). Can you confirm no external/API
   integration already depends on the filename-shaped URL? This affects how
   safe it is to eventually deprecate/remove it (Question 2).
   answer: as of now no external api integration depends on /download/<filename> endpoint.
   **Decision**: informational only — doesn't change any task, since Q2
   already decided to keep `/download/<filename>` permanently regardless.

---

## 6. Database changes needed

**None.** `pii_files.file_id` and `pii_files.redacted_path` already exist and
are already populated per file
([db.py:264-278](../../../db.py#L264-L278),
[db.py:295](../../../db.py#L295)) — `db.get_file(file_id)` is a straight
lookup, already used the same way by `POST /rehydrate`
([app.py:2324](../../../app.py#L2324)).

If Open Question #4 is answered "yes, fix the root cause" — still no schema
change, but a materially larger code change to
`dual_redaction_pipeline.py`'s path-construction and resume-safe logic (out
of scope for this section to design until Q4's answer is known).

---

## 7. Task List

- [x] Get answers to Open Questions in Section 5
- [x] Implement `GET /download/id/<file_id>` (Section 3.1) — `_require_uid()`
      gate, `db.get_file(file_id)` lookup, 404 on unknown id or missing file
      on disk, `send_file(..., download_name=path.name)`
      ([app.py](../../../app.py))
- [x] Extend the `"done"` SSE payload with `file_id` per output file
      (Section 3.3) — reuses `output_by_file_id`, no new computation needed
- [x] Update `showResults()` to link `/download/id/<file_id>` when a
      `file_id` is available for that output, falling back to
      `/download/<filename>` otherwise (defensive, e.g. if the pipeline
      errored before writing a per-file result)
- [x] Added `docs/swagger/download_by_id.yml`, wired `@swag_from` on the new
      route, `security: [ApiKeyAuth]` matching the other `_require_uid()`
      -gated routes' Swagger spec
- [x] Updated [wiki/architecture.md](../../architecture.md) — new route
      called out in the System Overview diagram and added to the
      `_require_uid()` gate list in the API-key subsection
- [ ] Test: two files with the same original filename, uploaded under
      different processes, each download correctly via their own `file_id`
- [ ] Test: unknown `file_id` → 404
- [ ] Test: no session/no API key on the new route → 403; wrong API key → 401
- [ ] Test: old `/download/<filename>` still works unchanged, ungated

---

## 8. Out of Scope (for this plan)

- Namespacing `files/`, `files/output/raw/`, `files/output/redacted/`,
  `audit/`, `unmasked/` by `file_id`/`process_id` instead of filename stem —
  Section 2.2's deeper finding, explicitly declined in Q4.
- Adjusting the downloaded attachment filename to disambiguate same-named
  files — explicitly declined in Q5.
- Deprecating or removing `/download/<filename>` — explicitly declined in
  Q2; it's a permanent, parallel route alongside the new one.
- Per-`client_id`/`job_id` authorization scoping on downloads (i.e. "this
  session/key may only download files belonging to client X") — same
  pre-existing gap already flagged out-of-scope in
  [plan-api-key-implementation.md Section 8](plan-api-key-implementation.md#L405-L410);
  this plan brings the new route to the same level everything else is
  already at, not further.
- Rate limiting / abuse protection on the download endpoint.
- Bulk/zip download of multiple files in one request — only single-file
  download is in scope here, matching today's behavior.
