# Output Files

For every file processed, the pipeline produces up to five output files.

---

## Output Format: Excel vs As is

The **Redacted Output** file's format depends on the **Output Format** dropdown
selected at upload time (see [usage.md](usage.md)):

- **Excel** (default): always `files/output/redacted/{name}_redacted.xlsx`, flattened — this is the file described in section 2 below.
- **As is**: `files/output/redacted/{name}_redacted.{original_ext}` — same extension as the upload (`.pdf`, `.docx`, `.doc`, `.xlsx`, `.xls`), with PII redacted in place and the original layout/formatting preserved. If "As is" fails for a given file, it silently falls back to the Excel output above, with a warning in the log.

The **Raw Excel**, **Audit Log**, **Secret Log**, and **Forensic Log** files
described below are produced the same way regardless of which output format was
selected — only the main redacted deliverable's format changes.

---

## 1. Raw Excel — `files/output/raw/{name}_raw.xlsx`

The unredacted extraction of the PDF — the output of Phase 1.

- Contains everything extracted by Docling / PyMuPDF / Tesseract
- Structured as rows and columns matching the original table layout
- Used internally by Phase 2 and useful for verifying extraction quality
- **Do not distribute** — contains original unredacted PII

If extraction fails (all three tiers produce 0 rows), this file will be empty. Check the live log for which tier triggered and what the error was.

---

## 2. Redacted Excel — `files/output/redacted/{name}_redacted.xlsx`

The main deliverable. Same structure as the Raw Excel but with PII replaced by tokens.

Example transformation:

| Raw value | Redacted value |
|-----------|----------------|
| `John Smith` | `[PERSON:001]` |
| `john@email.com` | `[EMAIL:001]` |
| `123 456 789` (TFN) | `[GOVERNMENT_ID:001]` |
| `04 1234 5678` | `[PHONE:001]` |
| `12/03/1985` (DOB) | `[DOB:001]` |
| `Commonwealth Bank` | `Commonwealth Bank` (allowlisted) |

**Safe to share.** All PII has been replaced. The original values can be restored using the secret log if needed.

---

## 3. Audit Log — `audit/pii_audit_log_{name}.xlsx`

A compliance record of every redaction made. One row per redacted value.

Columns:

| Column | Example | Description |
|--------|---------|-------------|
| `timestamp` | `2025-06-29 10:23:11` | When the redaction was applied |
| `filename` | `MyDoc.pdf` | Source document |
| `page_num` | `2` | Page in source PDF |
| `excel_row` | `14` | Row in raw Excel |
| `excel_col` | `2` | Column in raw Excel |
| `token_type` | `PERSON` | PII category |
| `token` | `[PERSON:001]` | The replacement token |
| `original_text` | `John Smith` | Original value (store securely) |

This file contains original PII — **treat as sensitive**.

---

## 4. Secret Log — `secrets/pii_secret_log_{name}.xlsx`

Maps every token back to its original value. Required for unmasking.

Columns:

| Column | Example |
|--------|---------|
| `Candidate` | `[PERSON:001]` |
| `Original Value` | `John Smith` |

- Keep this file in a **secure, access-controlled location**
- Without it, redacted documents cannot be restored
- One file per source document

---

## 5. Forensic Log — `secrets/pii_forensic_log.jsonl`

Appended to (not replaced) across all pipeline runs. One JSONL entry per candidate evaluated, whether redacted or skipped.

```jsonl
{"timestamp":"2025-06-29T10:23:11","filename":"MyDoc.pdf","excel_row":14,"excel_col":2,"entity_type":"PERSON","original_value":"John Smith","token":"[PERSON:001]","decision":"REDACT","reason":"LLM_DECISION","confidence":0.91,"context":"Taxpayer: John Smith  DOB: 12/03/1985"}
{"timestamp":"2025-06-29T10:23:11","filename":"MyDoc.pdf","excel_row":5,"excel_col":0,"entity_type":"ORGANIZATION","original_value":"Commonwealth Bank","token":null,"decision":"SKIP","reason":"SKIP_ALLOWLIST","confidence":null,"context":"Bank: Commonwealth Bank BSB 062-000"}
```

Use this file to:
- Audit why a specific value was or wasn't redacted
- Tune the allowlist or YAML rules
- Prove compliance decisions to reviewers

---

## Audit Summary (Console Output)

At the end of every run the pipeline prints a summary:

```
[Audit Summary] Operations Report:
  - Total Candidates Evaluated: 18
  - SKIP (Allowlist):            3
  - SKIP (LLM Decision):         2
  - REDACT (LLM Decision):      13
  - Detailed forensic log saved to SECRETS_FOLDER / pii_forensic_log.jsonl
Saved secret log:  pii_secret_log_MyDoc.xlsx (13 entries)
Done.
```

If `Total Candidates Evaluated: 0`, the PDF extraction failed — check the live log for Docling / PyMuPDF / Tesseract errors.
