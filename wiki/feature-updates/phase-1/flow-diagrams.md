# Process & Approach Flow Diagrams

Two diagrams, both reflecting the current codebase (including the untracked
`redaction_formats.py`, the `as_is` output path, image handling, and the
manual "reference table" correction flow):

1. **Process Flow** — what happens, step by step, from a user's point of view (upload → detect → mask → review/correct → rehydrate).
2. **Approach Flow** — the technical/architectural view: which module does what and how data moves between them.

> Rendering note: these are Mermaid diagrams. GitHub, Azure DevOps, and the
> VS Code "Markdown Preview Mermaid Support" extension render them inline.

---

## 1. Process Flow Diagram

End-to-end journey of a file, from upload to (optional) rehydration.

```mermaid
flowchart TD
    A[User opens upload link\nuid decrypted into session] --> B[Select Client + Job\nchoose output format: excel or as_is]
    B --> C[Upload one or more files]
    C --> D{Validate client_id / job_id\nagainst project / activity tables}
    D -- invalid --> D1[Reject upload]
    D -- valid --> E[Mint process_id + file_id per file\nwrite pii_files placeholder rows\nwrite manifest_ processid .json]
    E --> F[Launch pipeline subprocess\ndual_redaction_pipeline.py --manifest]
    F --> G[Stream progress to browser\nvia SSE /stream/job_id]

    G --> H[Phase 1: Extract to Raw Excel\nby file type: pdf / docx / doc / xlsx / xls / image]
    H --> I[Phase 2: Detect PII\nTier0 regex+checksum -> Tier0 structural labels\n-> GLiNER NER -> Ollama LLM review]
    I --> J{output_format?}
    J -- excel --> K[Flatten + mask into new .xlsx]
    J -- as_is --> L[Mask in place, same format\nxlsx/xls/docx/doc/pdf/image]
    L -- writer fails --> K

    K --> M[Write token to pii_redaction_map\nMySQL, source=pipeline]
    L --> M
    M --> N[Patch pii_files with real\noutput path + format]
    N --> O[SSE done event\nbrowser shows result]

    O --> P[Auto-load reference table\nGET /api/redaction-map/process_id]
    O --> Q[Auto-load preview\nGET /api/preview/file_id]
    O --> R[Download redacted file\nGET /download/filename]

    P --> S{User reviews table}
    S -- found a missed value --> T[POST /api/redaction-map/add\ntargeted re-mask, patches file in place\ninsert row source=manual]
    S -- edits/deletes a manual row --> U[PUT or DELETE /api/redaction-map/row_id\nrestore original, then re-mask if edited]
    T --> P
    U --> P

    R --> V{Need original back?}
    V -- yes --> W[POST /rehydrate  file_id\nauth: session uid required]
    W --> X[Look up token map in\npii_redaction_map for file_id]
    X --> Y{output_format?}
    Y -- excel --> Z[Replace tokens in flattened xlsx]
    Y -- as_is, text formats --> Z2[Search+replace tokens in original format]
    Y -- as_is, image --> Z3[Decrypt stored original\nno text tokens to search]
    Z --> AA[Download restored file]
    Z2 --> AA
    Z3 --> AA
    V -- no --> AB[Done]
```

**Notes on the process:**
- Detection is tiered and short-circuits: Tier 0 (regex + checksum: TFN/ABN/ACN/Medicare/email/phone/BSB) auto-redacts deterministically; only ambiguous PERSON/ORGANIZATION/DATE/FINANCIAL candidates reach the local Ollama LLM.
- `as_is` mode preserves the original file's look, but silently falls back to the flattened Excel output per-file if the format-specific writer fails — the *actual* format used is only known after the pipeline runs, so `app.py` patches the DB afterward.
- Images are a special case: pixel redaction is destructive, so the original is encrypted and stored separately rather than relying on token search for rehydration.
- The manual "add missing value" correction is a **targeted patch**, not a full pipeline re-run — it edits the already-redacted file in place and adds one `source='manual'` row, which is the only kind of row that can later be edited/deleted.

---

## 2. Approach / Architecture Flow Diagram

How the modules are structured and why (technical approach), including
the data flow between them.

```mermaid
flowchart LR
    subgraph Browser
        UI[Inline HTML/JS UI\nno templates or build step]
    end

    subgraph AppLayer["app.py (Flask — thin orchestration)"]
        Upload[POST /upload]
        Stream[GET /stream/job_id\nServer-Sent Events]
        Preview[GET /api/preview/file_id]
        RefTable[GET/POST/PUT/DELETE\n/api/redaction-map/...]
        Rehydrate[POST /rehydrate]
        Download[GET /download/filename]
    end

    subgraph FastPath["redaction_formats.py (lightweight, imported synchronously)"]
        RenderPreview[render_preview]
        MaskSingle[mask_single_value]
        UnmaskSingle[unmask_single_value]
        RemaskImg[remask_image_from_original]
    end

    subgraph HeavyPath["dual_redaction_pipeline.py (subprocess only — loads ML stack)"]
        Extract["Phase 1: Extraction\nDocling / OCR / spreadsheet / docx readers"]
        Detect["Phase 2: Detection\nregex+checksum -> structural labels\n-> GLiNER NER -> Ollama LLM"]
        MaskFull[apply_masking / write_redacted_as_is]
        UnmaskFull[unmask_file / unmask_as_is]
    end

    subgraph Config
        Rules[pii_rules.yaml\nregex patterns, structural labels,\nallowlist, prompt rules]
    end

    subgraph Storage
        Disk[(files/ on disk\nraw, redacted, encrypted originals)]
        DB[(MySQL\nproject, activity — read only\npii_files, pii_redaction_map,\npii_client_codes, pii_id_sequences)]
    end

    UI --> Upload
    Upload -- subprocess.Popen --> Extract
    Extract --> Detect
    Detect -- reads --> Rules
    Detect --> MaskFull
    MaskFull -- writes --> Disk
    MaskFull -- bulk insert\nsource=pipeline --> DB
    HeavyPath -- stdout --> Stream --> UI

    UI --> Preview --> RenderPreview -- reads --> Disk
    UI --> RefTable
    RefTable -- read rows --> DB
    RefTable -- add/edit --> MaskSingle -- patches --> Disk
    RefTable -- delete/edit restore --> UnmaskSingle
    RefTable -- image only --> RemaskImg
    RefTable -- writes row\nsource=manual --> DB

    UI --> Rehydrate -- subprocess.Popen --> UnmaskFull
    UnmaskFull -- reads token map --> DB
    UnmaskFull -- reads redacted file\nor encrypted original --> Disk

    UI --> Download --> Disk
```

**Why it's split this way:**

| Concern | Module | Reason |
|---|---|---|
| Web routes, upload handling, SSE, DB orchestration | `app.py` | Needs to respond fast; must **not** import the ML stack |
| Extraction + tiered detection + full mask/unmask | `dual_redaction_pipeline.py` | Loads Docling, GLiNER, Presidio, cv2 at import — multi-second, GB-scale, so it only runs as an isolated subprocess |
| Single-value mask/unmask, preview rendering | `redaction_formats.py` | A deliberately duplicated, lightweight subset of the same per-format logic, so the reference-table UI (add/edit/delete/preview) can call it synchronously from `app.py` without paying the ML-import cost |
| Detection rules | `pii_rules.yaml` | Hot-reloaded config (regex patterns, structural labels, allowlist, LLM prompt fragments) — no code change needed to tune redaction |
| Redaction map / audit trail | MySQL `pii_redaction_map` | `source ENUM('pipeline','manual')` (migration 0004) keeps pipeline output immutable while allowing manual corrections to be edited/deleted |

**Format dispatch (branches inside both `dual_redaction_pipeline.py` and its
lightweight counterpart):**

```mermaid
flowchart TD
    In[Input file extension] -->|pdf| Pdf[PyMuPDF text layer\n+ Tesseract OCR fallback]
    In -->|docx| Docx[python-docx]
    In -->|doc| Doc[Word COM -> docx -> python-docx]
    In -->|xlsx / xls| Xl[openpyxl / xlrd]
    In -->|png / jpg| Img["OCR word boxes\n(pixel redaction — destructive)"]

    Img --> EncOrig[Encrypt + store original\nseparately, since redaction\ncan't be reversed from pixels]
```

---

## Planned, not yet built

`wiki/feature-updates/phase-1/plan-rehyrdration-implementation.md` describes a
proposed extension: rehydrating a **downstream/derived** document (e.g. a tax
filing built off the redacted output elsewhere) against one `file_id`'s token
map, in-process via `redaction_formats.py` rather than the current
subprocess route. This is still design-stage — a few open questions in that
doc are unresolved — so it's intentionally left out of the diagrams above.
