# Workflows

*Every major user-facing and system-to-system flow, reverse-engineered from
`app.py`'s route handlers and the modules they call into.*

## 1. Upload → Detect → Mask → Download (the core flow)

```mermaid
sequenceDiagram
    actor User
    participant Browser
    participant App as app.py (Flask)
    participant DB as MySQL
    participant Pipe as dual_redaction_pipeline.py (subprocess)

    User->>Browser: Open pre-authenticated link (?uid=...)
    Browser->>App: GET /  (index)
    App->>App: crypto_utils.decrypt(uid) → session["uid"]
    App->>DB: GET /api/clients, /api/jobs (typeahead)
    User->>Browser: Pick Client + Job + Process Name,\nchoose output format (Excel or As is),\nselect files
    Browser->>App: POST /upload (multipart)
    App->>DB: get_or_create_client_code() → 3-letter code
    App->>DB: next_id("PRC"/"FIL") → process_id, file_id(s)
    App->>DB: record_file() → pii_files placeholder rows
    App->>App: write manifest_{process_id}.json
    App->>DB: log_upload() per file → PII_redation_upload_log
    App-->>Browser: {process_id, stream_url}
    App->>Pipe: subprocess.Popen(--manifest ...)
    Browser->>App: GET /stream/<process_id> (SSE)
    activate Pipe
    Pipe->>Pipe: Phase 1 — extract to Raw Excel\n(Docling → PyMuPDF → Tesseract fallback chain)
    Pipe->>Pipe: Phase 2 — Tier 0-3 detection + masking
    Pipe->>DB: insert_redaction_rows() (source='pipeline')
    Pipe-->>App: stdout lines (progress)
    App-->>Browser: SSE progress events
    deactivate Pipe
    App->>App: diff output dir → discover result files
    App->>DB: update_upload_status(), update_file_output()
    App-->>Browser: SSE "done" event (file list, file_ids)
    Browser->>App: GET /api/redaction-map/<process_id>\n(auto-load reference table)
    Browser->>App: GET /api/preview/<file_id>\n(auto-load preview)
    User->>Browser: Click Download
    Browser->>App: GET /download/<filename> or /download/id/<file_id>
    App-->>Browser: Redacted file
```

**Notes:**
- The `Client` and `Job` are not new concepts this app owns — they are read live
  from the firm's existing `project`/`activity` tables. Nothing is written back to
  those tables.
- The reference table and preview load automatically as soon as the run finishes —
  no extra click needed.
- If "As is" output fails for a particular file (rare, format-specific), that file
  silently falls back to the flattened Excel format, and the database is patched
  afterward to record what actually happened.

## 2. Manual correction — "Add a missed value" / edit / delete

```mermaid
sequenceDiagram
    actor Reviewer
    participant Browser
    participant App as app.py
    participant Fmt as redaction_formats.py
    participant DB as MySQL

    Reviewer->>Browser: Spots a PII value the pipeline missed\n(viewing the reference table + preview)
    Reviewer->>Browser: Types value + category, clicks Add
    Browser->>App: POST /api/redaction-map/add {file_id, value, category}
    App->>DB: get_redaction_rows(file_id) — existing tokens
    App->>Fmt: next_token_for_category() — next free counter
    App->>Fmt: mask_single_value(redacted_path, value, token)
    Fmt->>Fmt: In-place find/replace in the already-redacted\nfile (no full pipeline re-run)
    App->>DB: insert_redaction_row(source='manual')
    App-->>Browser: Updated table row + refreshed preview
    Reviewer->>Browser: (Optionally) edit/delete a manual row
    Browser->>App: PUT or DELETE /api/redaction-map/<row_id>
    App->>Fmt: unmask_single_value() → restore original,\nthen re-mask if edited
    App->>DB: delete_redaction_row() / new insert
```

**Key point:** this is a *targeted* patch, not a pipeline re-run. It edits the
already-redacted file directly and adds exactly one row — cheap and instant,
compared to minutes for a full Docling/GLiNER/Ollama pass. Only rows with
`source='manual'` can be edited or deleted this way; pipeline-generated rows are
read-only through this endpoint.

## 3. Rehydration — restoring the pipeline's own stored output (legacy path)

```mermaid
sequenceDiagram
    actor User
    participant Browser
    participant App as app.py
    participant Pipe as dual_redaction_pipeline.py
    participant DB as MySQL

    User->>Browser: POST /rehydrate {file_id}
    Browser->>App: (requires session uid or X-API-Key)
    App->>DB: get_file(file_id) → redacted_path, output_format
    App->>Pipe: subprocess --unmask-file <file_id>
    Pipe->>DB: get_redaction_map(file_id) → {token: value}, conflicts
    Pipe->>Pipe: Reverse-substitute tokens back to\noriginal values, per output_format
    Pipe-->>App: unmasked/{stem}_unmasked{ext}
    App-->>Browser: File download
```

Kept in the code but no longer linked from the UI as of 2026-07-13 — superseded by
Workflow 4 below, which handles the more realistic case of a *derived* document.

## 4. Rehydration — restoring values into a derived document (current UI flow)

This is the workflow that matters in practice: several source documents get
redacted, then someone builds a **new** document elsewhere (e.g. a tax return
draft) by copying values across from those redacted outputs — including the
literal token text. That new document is what needs its original values restored.

```mermaid
sequenceDiagram
    actor User
    participant Browser
    participant App as app.py
    participant DB as MySQL
    participant Fmt as redaction_formats.py

    User->>Browser: Open /rehydrate
    Browser->>App: GET /api/clients, /api/jobs\n(pick Client + Job)
    Browser->>App: GET /api/processes?client_id=&job_id=
    App->>DB: list_processes() → named batches, newest first
    User->>Browser: Pick a Process Name
    Browser->>App: GET /api/redaction-map/<process_id>\n(files table: filename + redacted-value count only)
    User->>Browser: Upload the derived document
    Browser->>App: POST /rehydrate/upload {process_id, file}
    App->>DB: get_files_for_process(process_id)
    App->>DB: get_redaction_map_for_process(process_id)\n→ {token: {value: [file_ids]}}
    App->>Fmt: rehydrate_uploaded_file()
    Fmt->>Fmt: Scan the upload for token strings actually present
    Fmt->>Fmt: Classify: safe (1 distinct value) vs conflicting\n(2+ distinct values across the batch's files)
    Fmt->>Fmt: Substitute only the safe tokens;\nleave conflicting tokens as literal text
    Fmt-->>App: rehydrated file + list of unresolved conflicts
    App-->>Browser: File download + X-Rehydrate-Warnings header\n(if any conflicts were left unresolved)
```

**Why "conflicts" can happen at all:** token numbering restarts at 1 for every
file (`<*PERSON_1*>` in file A and file B are almost certainly different people).
Merging multiple files' token maps into one document can therefore produce a
genuine ambiguity. The system's chosen behaviour is to **never guess** — an
ambiguous token is left as visible literal text with a warning, rather than
silently substituting a possibly-wrong value.

## 5. Bulk API redaction — caller-supplied values, no automatic detection

For server-to-server callers who already know exactly which strings in a file are
PII (no ML detection needed) and just want them masked in bulk, across many files
in one call.

```mermaid
sequenceDiagram
    actor Caller as External system
    participant App as app.py
    participant DB as MySQL
    participant Fmt as redaction_formats.py

    Caller->>App: POST /api/redact\n{process_id, files: [{file_id, source_values: [...]}]}
    App->>App: _require_uid() — session or X-API-Key
    App-->>Caller: 202 {process_id, stream_url} (immediate)
    App->>App: Spawn background thread
    Caller->>App: GET /stream/redact/<process_id> (SSE)
    loop per file
        App->>DB: get_file(file_id), get_redaction_rows(file_id)
        loop per source_value
            App->>App: Already masked? → silent skip
            App->>App: len < 3 chars? → status "too_short"
            App->>Fmt: guess_category(value)\n(regex/checksum match, else generic "PII")
            App->>Fmt: mask_single_value() — whole-word,\ncase-insensitive
            App->>DB: insert_redaction_row(source='api')
        end
        App-->>Caller: SSE "file_done" event
    end
    App-->>Caller: SSE "done" event (full result)
    Caller->>App: GET /download/id/<file_id> (whenever ready)
```

**This is deliberately not automated PII detection** — the caller already knows
what to redact by exact string. It reuses the same building blocks as the manual
"add a missed value" flow (Workflow 2), just looped across many files and values,
run asynchronously so a large batch doesn't block the caller's request.

## 6. Format-specific redaction: Excel vs "As is"

One dropdown, applied to the whole upload batch:

```mermaid
flowchart TD
    Start["Upload batch"] --> Choice{"Output Format\nselected?"}
    Choice -- Excel --> Flatten["Flatten to a generic .xlsx\n(layout/formatting discarded,\nsame for every input type)"]
    Choice -- "As is" --> Native["Redact in the ORIGINAL file's own\nformat, preserving layout"]
    Native --> PdfCheck{"Original format?"}
    PdfCheck -- PDF --> PdfRedact["Locate each PII value's exact\nposition (native text layer or\nTesseract OCR for scanned pages),\ndraw over it, burn in the token —\nPyMuPDF redaction annotations"]
    PdfCheck -- DOCX/DOC --> DocxRedact["Rewrite paragraph/table runs\nin place, preserving styles/images"]
    PdfCheck -- XLSX/XLS --> XlsxRedact["Rewrite cells in place,\npreserving sheets/formatting"]
    PdfRedact --> Fail{"Writer failed?"}
    DocxRedact --> Fail
    XlsxRedact --> Fail
    Fail -- yes --> Flatten
    Fail -- no --> Done["Redacted output, original format"]
    Flatten --> Done2["Redacted output, .xlsx"]
```

The PDF "as-is" path is a **separate, self-contained pass** over the original PDF
— it doesn't reuse the flattened Raw-Excel extraction used for the Excel output
path; it re-reads the original PDF fresh with PyMuPDF specifically to get precise
page/bounding-box coordinates for visual redaction.

## 7. Authentication decision (applies to most gated routes)

```mermaid
flowchart TD
    Req["Incoming request to a gated route"] --> HasSession{"session[\"uid\"]\nset (browser)?"}
    HasSession -- yes --> Allow["Proceed"]
    HasSession -- no --> HasKey{"X-API-Key header\npresent?"}
    HasKey -- no --> Deny403["403 Not authorized"]
    HasKey -- yes --> Valid{"hmac.compare_digest\nmatches API_KEY?"}
    Valid -- yes --> Allow
    Valid -- no --> Deny401["401 Invalid API key"]
```

See [06-security-and-auth.md](06-security-and-auth.md) for exactly which routes
this gate applies to, and which routes (e.g. `POST /upload`) have no hard gate at
all.
