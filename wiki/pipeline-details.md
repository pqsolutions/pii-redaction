# Pipeline Deep Dive

## Phase 1 — PDF Extraction

**Goal:** Convert a PDF into a structured Excel spreadsheet (the "Raw Excel") that Phase 2 can scan cell-by-cell.

### OCR Fallback Chain

The pipeline tries three extraction methods in order, moving to the next only if the previous produces zero rows:

```
Tier 1 (Docling)
    ├─ GPU available? → Granite-Docling 258M VLM (most accurate)
    └─ CPU only?      → Layout Heron OCR + TableFormer (ACCURATE mode)
    │
    ▼ 0 rows extracted?
Tier 2 (PyMuPDF)
    ├─ page.get_text()               → native text extraction
    ├─ page.get_text("ocr")          → inject OCR text layer if no native text
    └─ find_tables(strategy="text")  → geometric table detection
    │
    ▼ 0 rows extracted?
Tier 3 (Tesseract)
    ├─ Renders each page at 300 DPI via PyMuPDF pixmap
    ├─ Runs pytesseract.image_to_data(psm=6) → per-word bounding boxes
    ├─ Groups words into text lines by Y-coordinate (±12 px tolerance)
    ├─ Clusters word X-positions into column anchors (60 px merge threshold)
    └─ Maps each word to its nearest column anchor → preserves empty columns
```

**When Tesseract is needed:** The source PDF is scanned (image-only, no embedded text layer). This is common with pre-filled ATO tax documents received as image scans.

**Tesseract binary path** (set in pipeline):

```python
pytesseract.pytesseract.tesseract_cmd = r"C:\Users\User_Shibu\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"
```

### How Docling Extracts Tables (Tier 1 CPU)

Docling uses **TableFormer** (`TableFormerMode.ACCURATE`) with cell matching enabled. It returns every `TableCell` with precise grid metadata:

- `start_row_offset_idx` — which row the cell belongs to
- `start_col_offset_idx` — which column the cell belongs to
- `column_header` — whether the cell is a header

The function `_docling_grid_to_dataframe()` builds a full `n_rows × n_cols` matrix from these positions (empty cells stay `None`), then promotes the detected header row to DataFrame column names. This avoids the left-packing problem where empty columns would be silently discarded.

A secondary fix (`_fix_docling_table_alignment()`) handles a systematic Docling quirk: when a table has an "Amount" column that is genuinely empty in the source, Docling sometimes places the dollar values one column to the left. The fix detects this pattern and shifts the values back.

### How Tesseract Reads and Interprets Tables (Tier 3)

**Tesseract has no native concept of tables.** It outputs a flat list of recognised words with pixel-level bounding boxes. The pipeline reconstructs table structure entirely in Python from those coordinates.

**Step 1 — Render page as image**

```python
pix = page.get_pixmap(dpi=300)          # 300 DPI for scanned document clarity
img = Image.open(io.BytesIO(pix.tobytes("png")))
```

**Step 2 — Run OCR with per-word output**

```python
tsv = pytesseract.image_to_data(
    img,
    config='--psm 6',                   # PSM 6: treat page as one uniform text block
    output_type=pytesseract.Output.DICT
)
# Returns dicts: tsv['text'], tsv['conf'], tsv['left'], tsv['top'], tsv['width'], tsv['height']
```

`--psm 6` (Page Segmentation Mode 6) is chosen because scanned forms are typically a single text region. Tesseract scans top-to-bottom, left-to-right and returns confidence scores per word.

**Step 3 — Filter low-confidence noise**

```python
if conf > 20 and txt:       # Discard anything below 20% confidence
    cx = left + width // 2  # Horizontal centre of the word
    cy = top + height // 2  # Vertical centre of the word
```

**Step 4 — Group words into text lines by vertical position**

Words whose vertical centres (`cy`) are within **±12 pixels** of each other are merged into the same logical line. The line's `cy` is updated to the running average as more words are added.

```python
Y_TOL = 12
for w in words:
    for tl in text_lines:
        if abs(w['cy'] - tl['cy']) <= Y_TOL:
            tl['words'].append(w)
            tl['cy'] = average(cy values in tl)  # Keep median stable
```

**Step 5 — Discover column anchors from all horizontal positions**

All word centres (`cx`) across every line are sorted and scanned. If two consecutive `cx` values are within **60 pixels** of each other they are merged into a single anchor (midpoint). Each anchor represents one logical column.

```python
COL_MERGE_PX = 60
col_anchors = []
for cx in sorted(all word cx values):
    if col_anchors and cx - col_anchors[-1] <= COL_MERGE_PX:
        col_anchors[-1] = (col_anchors[-1] + cx) // 2  # merge
    else:
        col_anchors.append(cx)                          # new column
```

**Step 6 — Assign each word to its nearest column**

```python
def nearest_col(cx):
    return min(range(len(col_anchors)), key=lambda i: abs(col_anchors[i] - cx))
```

Words in the same line that map to the same column index are concatenated with a space. Lines where no word maps to a particular column simply have no entry for that column index — meaning **empty columns are preserved** naturally.

**Step 7 — Emit structured rows**

```python
row_dict = {col_index: "cell text", ...}   # Only populated columns
pymupdf_rows.append(row_dict)
```

These row dicts feed into a `pd.DataFrame` where integer column keys directly correspond to Excel column positions.

### Docling Chunking

Docling processes PDFs in **3-page chunks** to stay within GPU memory limits. Each chunk is appended to the same output DataFrame. For a 10-page PDF, that means 4 Docling calls (pages 1-3, 4-6, 7-9, 10).

### Output Validation

Before saving the raw Excel, the pipeline checks that at least one cell in the DataFrame contains non-whitespace text. Phantom rows (only empty strings or NaN) are discarded. If the result is still empty after validation, the fallback tier is triggered.

---

## Phase 2 — PII Detection and Masking

**Goal:** Scan every cell of the Raw Excel, detect PII, and replace it with a reversible token like `[PERSON:001]`.

### Tier 0 — Regex + Checksum (Deterministic)

These patterns auto-redact with no LLM review needed because they include mathematical validation:

| Pattern | Validation | Token |
|---------|-----------|-------|
| TFN (9-digit) | Luhn-style weighted checksum | `[GOVERNMENT_ID]` |
| Medicare (10-digit + IRN) | Weighted digit checksum | `[GOVERNMENT_ID]` |
| ABN (11-digit) | ATO weighted checksum algorithm | `[BUSINESS_ID]` |
| ACN (9-digit) | ASIC weighted checksum | `[BUSINESS_ID]` |
| Email address | RFC-style regex | `[EMAIL]` |
| AU phone number | +61, 02-09, 04xx, 13xx, 18xx | `[PHONE]` |
| BSB + account | `NNN-NNN` + 6-10 digits | `[BANK_DETAILS]` |

Tier 0 decisions are marked `DETERMINISTIC_PATTERN` in the forensic log and are never overridden by the LLM.

### Tier 1 — Structural Label Detection

Scans for field-label → value patterns using `STRUCTURAL_LABELS` from `pii_rules.yaml`. Examples:

```
"Given Name:    John"        → value "John"  → [PERSON]
"Date of Birth: 12/03/1985"  → value "12/03/1985" → [DOB]
"TFN: 123 456 789"           → value caught by Tier 0 already
```

The detector also uses **grid context** — if the cell to the left is a known label, the cell to the right is treated as its value even without a colon separator. This handles tabular forms where labels and values are in adjacent columns.

Cells already matched by Tier 0 are excluded from Tier 1.

### Tier 2 — GLiNER Named Entity Recognition

GLiNER is a contextual ML model that detects entities based on surrounding text, not just pattern matching.

**Model:** `urchade/gliner_multi_pii-v1`  
**Threshold:** 0.85 confidence  
**Labels:** `PERSON`, `ORGANIZATION`, `FINANCIAL`, `COMPANY`

**Chunking strategy** to handle long text:
- Window: 200 words
- Overlap: 50 words (prevents entities at chunk boundaries being missed)
- Deduplication: overlapping spans from adjacent chunks are merged

**False-positive filters applied after GLiNER:**
- NER blocklist (60+ terms): ASX, ATO, common AU abbreviations, state names, months
- Minimum length: ≥ 3 characters
- Minimum alpha content: at least 1 letter
- Form label regex: skips field headings like "Given Name"
- Legal boilerplate regex: skips standard disclaimer phrases
- Generic date filter: skips financial year references ("FY2024", "1 July to 30 June")

### Tier 3 — Ollama LLM Decision

Candidates from Tiers 1 and 2 that passed filters are batched and sent to Ollama for a final MASK/KEEP decision.

**Model:** `gemma3:4b` (local, runs on CPU or GPU)

**Prompt structure:**
1. System prompt: Australian Privacy Principles context + rules from `pii_rules.yaml`
2. User message: List of candidates with entity type, value, and surrounding cell context
3. Response: JSON list of UUIDs to MASK

**Batching:** Multiple candidates are sent in a single API call to reduce round-trips. Each candidate has a UUID so the LLM's response can be matched back.

**Fallback behaviour:**
- If Ollama is unreachable → Tier 3 skipped entirely
- If a single batch call times out (600s) → that batch is skipped, others continue
- If LLM returns malformed JSON → fallback to per-entity calls

### Token Assignment

Every detected PII value is assigned a unique token:

```python
get_candidate_token(base_token="PERSON", value="John Smith", secret_map)
# Returns: "[PERSON:001]" on first occurrence
# Returns: "[PERSON:001]" again if "John Smith" appears elsewhere (consistent)
# Returns: "[PERSON:002]" for a different name "Jane Doe"
```

The `secret_map` dictionary (`value → token`) is built up during scanning and saved to `pii_secret_log_{name}.xlsx` at the end.

### apply_masking()

After all tiers have run, `apply_masking()` iterates every cell and performs string replacement:

```
"Contact: John Smith (john@email.com) TFN 123456789"
→ "Contact: [PERSON:001] ([EMAIL:001]) TFN [GOVERNMENT_ID:001]"
```

Replacements are sorted by length (longest first) to prevent partial matches corrupting longer values.

---

## Decision Audit Trail

Every candidate evaluated in Phase 2 generates a forensic log entry:

```jsonl
{
  "timestamp": "2025-06-29T10:23:11",
  "filename": "MyDoc.pdf",
  "excel_row": 14,
  "excel_col": 2,
  "entity_type": "PERSON",
  "original_value": "John Smith",
  "token": "[PERSON:001]",
  "decision": "REDACT",
  "reason": "LLM_DECISION",
  "confidence": 0.91,
  "context": "Taxpayer: John Smith  DOB: 12/03/1985"
}
```

Reason codes:

| Code | Meaning |
|------|---------|
| `DETERMINISTIC_PATTERN` | Tier 0 regex with checksum validation |
| `STRUCTURAL_LABEL` | Tier 1 field label match |
| `GLINER_SCORE` | Tier 2 NER above threshold |
| `LLM_DECISION` | Tier 3 Ollama returned MASK |
| `SKIP_ALLOWLIST` | Matched an allowlist entry, not redacted |
| `SKIP_LLM_DECISION` | Tier 3 Ollama returned KEEP |
| `SKIP_BLOCKLIST` | Matched the NER blocklist |

---

## Unmasking Flow

```
Redacted Excel + Secret Log
         │
         ▼
unmask_documents()
    ├─ Load secret log: token → original value
    ├─ For each cell in redacted Excel:
    │       Replace "[PERSON:001]" → "John Smith"
    │       Replace "[EMAIL:001]"  → "john@email.com"
    └─ Save to unmasked/{name}_unmasked.xlsx
```

The unmasked file is byte-for-byte equivalent to the original Raw Excel content. Structural formatting may differ from the original PDF.
