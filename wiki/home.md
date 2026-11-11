# PII Redaction Pipeline — Wiki

Automated detection and redaction of Personally Identifiable Information (PII) from PDF and Excel documents using a multi-tier AI pipeline.

## Pages

| Page | Description |
|------|-------------|
| [Architecture](architecture.md) | System design, pipeline phases, data flow |
| [Installation](installation.md) | Prerequisites, setup, and environment configuration |
| [Configuration](configuration.md) | All config parameters, YAML rules, model settings |
| [Usage Guide](usage.md) | Running the app, web UI walkthrough, CLI reference |
| [Pipeline Deep Dive](pipeline-details.md) | Per-tier detection logic, OCR fallback chain |
| [Output Files](output-files.md) | What files are produced and what they contain |
| [Deployment](deployment.md) | Production server and Windows Service setup |
| [Troubleshooting](troubleshooting.md) | Common errors and fixes |

## Quick Start

```powershell
# Activate the virtual environment
& "c:\Users\User_Shibu\Downloads\script_pii\pii_env\Scripts\Activate.ps1"

# Start the web UI
python app.py
# Open http://127.0.0.1:5009 in your browser
```

## What It Does

1. **Upload** a PDF or Excel file via the browser UI
2. **Phase 1** — Docling extracts the document into a structured Raw Excel
3. **Phase 2** — A 4-tier engine detects PII and replaces it with tokens like `[PERSON:001]`
4. **Download** the redacted Excel from the browser

The pipeline is designed around Australian Privacy Principles (APP) and the Privacy Act 1988, targeting tax and financial documents.
