# Installation

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Windows | 10 / 11 | Tested on Windows 11 Pro |
| Python | 3.11.9 | Use exactly 3.11.x for compatibility |
| Tesseract OCR | 5.4+ | Required for scanned/image PDFs |
| Ollama | latest | Required for Phase 2 LLM decisions |
| CUDA (optional) | 11.8+ | Enables Docling GPU VLM mode (faster) |

---

## Step 1 — Python Virtual Environment

```powershell
cd c:\Users\User_Shibu\Downloads\script_pii

# Create the venv (already exists as pii_env)
python -m venv pii_env

# Activate
.\pii_env\Scripts\Activate.ps1
```

## Step 2 — Install Python Dependencies

```powershell
pip install -r src/requirements.txt

# Additional runtime deps (not in requirements.txt)
pip install flask werkzeug waitress pywin32
```

Full dependency list:

```
docling              # Document layout analysis + OCR
pyyaml               # YAML rules file parsing
requests             # HTTP client for Ollama API
presidio-analyzer    # PII pattern matching framework
gliner               # Named entity recognition
pillow               # Image processing
pandas               # DataFrame manipulation
opencv-python        # Image operations
pytesseract          # Tesseract OCR Python wrapper
numpy                # Numerical operations
pymupdf              # PDF reading (fitz)
beautifulsoup4       # HTML/XML parsing
openpyxl             # Excel file read/write
flask                # Web framework
werkzeug             # WSGI utilities
waitress             # Production WSGI server
pywin32              # Windows Service support
```

## Step 3 — Install Tesseract OCR Binary

Tesseract is a system-level binary — the Python `pytesseract` package is only a wrapper.

```powershell
# Via winget (recommended)
winget install UB-Mannheim.TesseractOCR

# Default install location after install:
# C:\Users\<you>\AppData\Local\Programs\Tesseract-OCR\tesseract.exe
```

The pipeline already has the path hard-coded. If your install path differs, update line ~2340 in `dual_redaction_pipeline.py`:

```python
pytesseract.pytesseract.tesseract_cmd = r"C:\Users\User_Shibu\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"
```

Verify it works:

```powershell
& "C:\Users\User_Shibu\AppData\Local\Programs\Tesseract-OCR\tesseract.exe" --version
# tesseract v5.4.0.20240606
```

## Step 4 — Install and Start Ollama

Ollama runs the local `gemma3:4b` language model used in Phase 2 decision-making.

```powershell
# 1. Download from https://ollama.com/download and install

# 2. Pull the model
ollama pull gemma3:4b

# 3. Start the server (runs on http://localhost:11434 by default)
ollama serve
```

Verify:

```powershell
Invoke-WebRequest http://localhost:11434 -UseBasicParsing
# Should return 200
```

> **Note:** The pipeline is non-fatal if Ollama is unavailable — Tier 3 LLM decisions will be skipped and low-confidence candidates will not be redacted.

## Step 5 — Verify the Setup

```powershell
# Activate venv
.\pii_env\Scripts\Activate.ps1

# Quick import check
python -c "import docling, gliner, pytesseract, fitz, flask; print('All OK')"

# Start the app
python app.py
# Open http://127.0.0.1:5009
```

---

## Directory Initialization

The pipeline auto-creates required folders on first run:

```
files/
files/output/raw/
files/output/redacted/
audit/
secrets/
unmasked/
```

No manual setup needed.

---

## MySQL Setup (Client/Job selection + DB-backed rehydration)

The app reads Client/Job data from the existing `project` / `activity` tables in the
same MySQL DB used for `PII_redation_upload_log` (see `.env.example` for connection
vars), and needs a few new tables of its own for the redaction map. Run once against
that DB before using Client/Job selection or `/rehydrate`:

```powershell
mysql -h $env:DB_HOST -u $env:DB_USER -p $env:DB_NAME < migrations\0001_pii_redaction_tables.sql
```

The script only creates new tables (`pii_client_codes`, `pii_id_sequences`,
`pii_files`, `pii_redaction_map`) via `CREATE TABLE IF NOT EXISTS` — it does not
modify `project`, `activity`, or `PII_redation_upload_log`. See
`wiki/architecture.md` → "DB-backed Redaction Map & Rehydration" for the schema and
data flow.

`PII_redation_upload_log` itself is expected to already exist and isn't managed by
the `migrations/` scripts — see the `CREATE TABLE` / `ALTER TABLE` statements in
`db.py`'s module docstring (also documented in `wiki/architecture.md` → "Upload
Logging (MySQL)") for the current schema, including the `input_file`, `output_file`,
and `completed_on` columns.

---

## GPU / CUDA (Optional)

If a CUDA-capable GPU is present, Docling will automatically use the Granite-Docling 258M VLM for faster and more accurate PDF extraction. No extra configuration is needed — Docling detects CUDA availability at runtime.

To check:

```python
import torch
print(torch.cuda.is_available())
```

Without CUDA, Docling falls back to the CPU Layout Heron pipeline (slower but correct).
