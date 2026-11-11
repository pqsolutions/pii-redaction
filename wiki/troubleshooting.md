# Troubleshooting

## ModuleNotFoundError: No module named 'docling'

**Cause:** The script is running with the system Python, not the `pii_env` virtual environment.

**Fix:** Always use the venv Python explicitly:

```powershell
# Option A — Use venv Python directly
& "c:\Users\User_Shibu\Downloads\script_pii\pii_env\Scripts\python.exe" app.py

# Option B — Activate first
& "c:\Users\User_Shibu\Downloads\script_pii\pii_env\Scripts\Activate.ps1"
python app.py
```

---

## TesseractNotFoundError: tesseract is not installed or it's not in your PATH

**Cause:** The Tesseract OCR binary is not installed, or its path isn't set in the pipeline.

**Fix:**

1. Install Tesseract:
   ```powershell
   winget install UB-Mannheim.TesseractOCR
   ```
   Installs to `C:\Users\<you>\AppData\Local\Programs\Tesseract-OCR\`

2. The pipeline sets the path explicitly at line ~2340 of `dual_redaction_pipeline.py`:
   ```python
   pytesseract.pytesseract.tesseract_cmd = r"C:\Users\User_Shibu\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"
   ```
   Update this path if your install location differs.

---

## No output files found — check the log above

**Cause:** The pipeline completed but produced 0 redacted candidates. This is always because PDF extraction failed — the raw Excel is empty.

**Diagnose:** Look in the live log for one of:
- `Saved Empty Raw Excel:` — all three extraction tiers returned 0 rows
- `[ERROR] pytesseract Tier 3 failed:` — Tesseract crash (usually missing binary)
- `[ERROR] PyMuPDF Tier 2 failed:` — corrupt PDF
- `[ERROR] Docling Tier 1 failed:` — Docling model not downloaded

**Common fixes:**

| Log message | Fix |
|-------------|-----|
| `TesseractNotFoundError` | Install Tesseract binary (see above) |
| `Saved Empty Raw Excel` after all tiers | PDF may be password-protected; remove password first |
| `Docling extracted 0 tables, 0 text lines` | Check Docling model files are present |
| `Total Candidates Evaluated: 0` | Raw Excel was empty; fix extraction first |

---

## Ollama connection refused / Tier 3 disabled

**Cause:** Ollama is not running.

**Effect:** Only Tiers 0–2 run. Contextual entities (names without structural labels) may not be redacted.

**Fix:**

```powershell
ollama serve
# Then retry the upload
```

Check Ollama is up:

```powershell
Invoke-WebRequest http://localhost:11434 -UseBasicParsing
```

---

## Pipeline very slow (10+ minutes per page)

**Cause:** Docling is running in CPU mode without CUDA. On CPU, the VLM model is slow.

**Check:**

```python
import torch
print(torch.cuda.is_available())  # False = CPU mode
```

**Options:**
- Install CUDA toolkit + PyTorch with CUDA support for GPU acceleration
- Reduce `DPI` from 150 to 96 in `dual_redaction_pipeline.py` (faster but lower accuracy)
- Use a machine with a GPU

---

## Redacted output is missing some PII

**Possible causes:**

1. **Tier 3 (Ollama) not running** — names detected by GLiNER won't be redacted
2. **GLiNER confidence too high** — lower `GLINER_CONFIDENCE_THRESHOLD` from 0.85 to 0.75
3. **Value is on the allowlist** — check `pii_rules.yaml` ALLOWLIST section
4. **Value is in the NER blocklist** — check `NER_BLOCKLIST` in the pipeline script
5. **Extraction missed that cell** — check the raw Excel to see if the value was even extracted

**Debugging:** Check `secrets/pii_forensic_log.jsonl` for the value. Search by the original text — if it appears with `"decision": "SKIP"`, the reason code will tell you why it was not redacted.

---

## Port 5009 already in use

```powershell
# Find what's using port 5009
netstat -ano | findstr :5009

# Kill it (replace 1234 with the actual PID)
Stop-Process -Id 1234 -Force
```

---

## Permission denied writing to files/output

**Cause:** The `files/output/redacted/` or `audit/` directories don't exist and can't be created, or another process has a lock on the file.

**Fix:**

```powershell
New-Item -ItemType Directory -Force "c:\Users\User_Shibu\Downloads\script_pii\files\output\raw"
New-Item -ItemType Directory -Force "c:\Users\User_Shibu\Downloads\script_pii\files\output\redacted"
New-Item -ItemType Directory -Force "c:\Users\User_Shibu\Downloads\script_pii\audit"
New-Item -ItemType Directory -Force "c:\Users\User_Shibu\Downloads\script_pii\secrets"
```

Close Excel if you have the raw/redacted file open — Excel locks files for writing.

---

## Checking Which Python Is Being Used

```powershell
# From within the venv (after activation):
python -c "import sys; print(sys.executable)"
# Should show: c:\Users\User_Shibu\Downloads\script_pii\pii_env\Scripts\python.exe

# Check installed packages:
pip list | Select-String "docling"
```

---

## Re-running After a Failed Job

The pipeline skips files that already have output. If a run failed mid-way, delete the partial output before retrying:

```powershell
$name = "MyDocument"
Remove-Item "files\output\raw\${name}_raw.xlsx"      -ErrorAction SilentlyContinue
Remove-Item "files\output\redacted\${name}_redacted.xlsx" -ErrorAction SilentlyContinue
```
