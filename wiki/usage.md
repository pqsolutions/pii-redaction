# Usage Guide

## Web UI (Recommended)

### Starting the Server

```powershell
# Activate the virtual environment
& "c:\Users\User_Shibu\Downloads\script_pii\pii_env\Scripts\Activate.ps1"

# Start Flask dev server
python app.py
# → PII Redaction UI -> http://127.0.0.1:5009
```

Open **http://127.0.0.1:5009** in your browser.

---

### Uploading and Redacting Files

1. **Drag and drop** PDF, XLSX, XLS, DOCX, or DOC files onto the upload zone — or click **browse** to select files
2. Choose an **Output Format**: `Excel` (default — today's flattened `.xlsx`, same for every input format) or `As is` (redacts the original file in its own format — PDF stays PDF, DOCX stays DOCX, etc. See below.)
3. Files appear in the list with their size; click **×** to remove any file
4. Click **Run Redaction**
5. Watch the **Live Output** console — logs stream in real time as the pipeline runs
6. When the progress bar hits 100% and shows "Redaction complete!", the **Redacted Output** section appears
7. Click **Download** next to each output file

Supported formats: `.pdf`, `.xlsx`, `.xls`, `.docx`, `.doc` — up to **200 MB** per file. `.doc` (legacy binary Word) requires Microsoft Word to be installed on the machine running the pipeline — it's converted to `.docx` via COM automation before extraction (and back to `.doc` again for "As is" output).

---

### Output Format: Excel vs As is

One dropdown applies to the whole upload batch (not per-file).

| | Excel | As is |
|---|---|---|
| Output extension | Always `.xlsx`, flattened | Matches the original upload (`.pdf`→`.pdf`, `.docx`→`.docx`, `.xls`→`.xls`, ...) |
| Layout/formatting | Discarded — plain rows/columns | Preserved — sheets/formatting (xlsx), styles/images (docx), and exact visual position (PDF, via redaction annotations over the original text) |
| PDF fidelity | N/A (converted to Excel) | Redacts the original PDF in place — draws over the exact PII location and burns in the token, using the PDF's native text layer or OCR for scanned pages |
| If "As is" fails for a file | — | Falls back to Excel output for that file, with a warning in the log |

See [wiki/feature-updates/phase-1/plan-as-it-is-implementation.md](feature-updates/phase-1/plan-as-it-is-implementation.md) for the full design.

---

### What Happens During Processing

The log console shows each step as it happens:

```
[INFO] Python : C:\...\pii_env\Scripts\python.exe
[INFO] Script : C:\...\dual_redaction_pipeline.py
[Phase 1] Converting PDF to Excel via Docling...
    Docling extracted 3 tables, 47 text lines
    Saved Raw Excel: MyDoc_raw.xlsx (50 rows)
[Phase 2] Scanning Raw Excel and Applying Masking...
[Phase 2] Running SLM inference...
[Audit Summary] Operations Report:
  - Total Candidates Evaluated: 18
  - SKIP (Allowlist):            3
  - SKIP (LLM Decision):         2
  - REDACT (LLM Decision):      13
  - Detailed forensic log saved to SECRETS_FOLDER / pii_forensic_log.jsonl
Saved secret log: pii_secret_log_MyDoc.xlsx (13 entries)
Done.
```

---

## Command Line

### Redaction (standard)

```powershell
& "c:\Users\User_Shibu\Downloads\script_pii\pii_env\Scripts\python.exe" dual_redaction_pipeline.py
```

Processes **all PDFs** found in `./files/`. Skips any file that already has a corresponding raw or redacted Excel (resume-safe).

### Unmasking

To restore original PII values from a previously redacted Excel:

```powershell
& "c:\Users\User_Shibu\Downloads\script_pii\pii_env\Scripts\python.exe" dual_redaction_pipeline.py --unmask
```

Requires:
- `files/output/redacted/{name}_redacted.xlsx` — the redacted file
- `secrets/pii_secret_log_{name}.xlsx` — the token→value map

Output goes to `unmasked/{name}_unmasked.xlsx`.

---

## Processing a Single File

The pipeline picks up all PDFs in `./files/` automatically. To process only one file, move only that file into `./files/` before running, or temporarily rename others.

---

## Re-running a File

By default the pipeline skips files that already have output (to avoid re-processing). To force a re-run, delete the existing raw and/or redacted Excel:

```powershell
Remove-Item "files\output\raw\MyDoc_raw.xlsx"
Remove-Item "files\output\redacted\MyDoc_redacted.xlsx"
```

Then re-upload via the UI or run the CLI again.

---

## Checking Output

| File | Location | Purpose |
|------|----------|---------|
| `{name}_redacted.xlsx` | `files/output/redacted/` | Main deliverable — share this |
| `{name}_raw.xlsx` | `files/output/raw/` | Verify extraction quality |
| `pii_audit_log_{name}.xlsx` | `audit/` | Compliance record |
| `pii_secret_log_{name}.xlsx` | `secrets/` | PII mapping (keep secure) |
| `pii_forensic_log.jsonl` | `secrets/` | Per-decision audit trail |

---

## Ollama Status

If Ollama is not running, the pipeline continues but Tier 3 (LLM decisions) is skipped. Only Tier 0 (regex), Tier 1 (structural), and Tier 2 (GLiNER) will produce redactions. You may see a warning in the log:

```
[WARNING] Ollama unreachable — Tier 3 LLM disabled
```

To enable Tier 3, start Ollama before running the pipeline:

```powershell
ollama serve
```
