# PII Redaction Pipeline Setup & Guide

Welcome! This guide is written for **complete beginners** who have never used Python or run code before. By following the step-by-step instructions below, you will set up a private virtual environment, install the required tools, configure your redaction rules, and run the PII Redaction Pipeline successfully.

---

## 📋 Table of Contents
1. [Overview](#-overview)
2. [Prerequisites Setup (Step-by-Step)](#-prerequisites-setup-step-by-step)
3. [How to Run the Redaction Pipeline](#%EF%B8%8F-how-to-run-the-redaction-pipeline)
4. [File Structure Guide](#-file-structure-guide)
5. [How to Customize Rules & Tweak Settings](#-how-to-customize-rules--tweak-settings)

---

## 🔍 Overview

The **PII Redaction Pipeline** is an automated tool designed to scan documents (PDFs, images, and Excel spreadsheets) and mask **Personally Identifiable Information (PII)** like Names, Tax File Numbers (TFNs), Australian Business Numbers (ABNs), Dates of Birth (DOBs), and Addresses.

It uses a dual-engine architecture:
- **Rule-based & Regex Engines:** Instantly redacts exact patterns (like TFNs and emails) and structural labels in tables.
- **Local AI (SLM):** Uses a local small language model running on your computer to check context and verify whether an entity should be masked (REDACT) or kept (SKIP). **All processing is local and secure; no data leaves your machine.**

---

## 🛠️ Prerequisites Setup (Step-by-Step)

Follow these steps in order to set up your environment.

### Step 1: Install Python (Version 3.11.x)
Python is the programming language that runs this pipeline.
- **Windows:** Download Python 3.11.9 from the official website: [Python 3.11.9 Downloads](https://www.python.org/downloads/release/python-3119/). Run the installer and **CRITICAL:** Check the box that says **"Add python.exe to PATH"** at the bottom of the installer window before clicking Install.
- **macOS (Homebrew):** Install via terminal:
  ```bash
  brew install python@3.11
  ```
- **Linux (Ubuntu/Debian):** Install via terminal:
  ```bash
  sudo apt update && sudo apt install python3.11 python3.11-venv python3.11-dev
  ```

### Step 2: Install Tesseract OCR (Required for PDF Fallback Scanner)
The pipeline fallback scanner uses Tesseract OCR to read scanned/non-selectable PDFs. Tesseract must be installed on your computer.
- **Windows:**
  1. Download the installer (e.g. from UB Mannheim: [Tesseract for Windows](https://github.com/UB-Mannheim/tesseract/wiki)).
  2. Run the installer and note the install path (usually `C:\Program Files\Tesseract-OCR`).
  3. Add the install folder path to your system's Environment Variable `PATH` so Python can call it.
- **macOS:** Install via Homebrew:
  ```bash
  brew install tesseract
  ```
- **Linux (Ubuntu/Debian):** Install via package manager:
  ```bash
  sudo apt update && sudo apt install tesseract-ocr libtesseract-dev
  ```

### Step 3: Install Ollama (Local AI Engine)
Ollama runs the local AI model used to evaluate context.
- **Windows / macOS:** Download and run the installer from [ollama.com](https://ollama.com).
- **Linux:** Run the installation script:
  ```bash
  curl -fsSL https://ollama.com/install.sh | sh
  ```
- **All Operating Systems:** Once installed, open your command line terminal and pull the model:
  ```bash
  ollama run gemma3:4b
  ```
  *(Keep the terminal open until the download completes. Ollama will run in the background.)*

### Step 4: Create a Python Virtual Environment
A virtual environment is an isolated container on your computer where the pipeline's tools are installed.
- **Windows:** Open Command Prompt or PowerShell in the project folder and run:
  ```cmd
  python -m venv pii_env
  ```
- **macOS / Linux / Unix:** Open terminal in the project folder and run:
  ```bash
  python3 -m venv pii_env
  ```

### Step 5: Activate the Virtual Environment
Every time you open your terminal to run the pipeline, activate the environment first:
- **Windows (Command Prompt):**
  ```cmd
  pii_env\Scripts\activate
  ```
- **Windows (PowerShell):**
  ```powershell
  .\pii_env\Scripts\Activate.ps1
  ```
- **macOS / Linux / Unix (Terminal):**
  ```bash
  source pii_env/bin/activate
  ```
*(Once activated, you will see `(pii_env)` printed at the very beginning of your terminal prompt line.)*

### Step 6: Install Required Packages
With the environment activated, install the libraries listed in `requirements.txt`:
- **All Operating Systems:**
  ```bash
  pip install -r src/requirements.txt
  ```
*Wait a few minutes for the installation to download and setup all components.*

---

## 🏃‍♂️ How to Run the Redaction Pipeline

Follow these simple steps whenever you want to redact files:

1. **Place your input files** (PDFs or Excel spreadsheets) inside the `files/` folder (see [File Structure Guide](#-file-structure-guide)).
2. Make sure the **Ollama** application is running in the system tray.
3. Open your terminal in the project folder and activate the environment:
   ```cmd
   pii_env\Scripts\activate
   ```
4. Run the pipeline script:
   ```cmd
   python dual_redaction_pipeline.py
   ```
5. Find your results:
   - Redacted files are saved in `files/output/redacted/`.
   - Raw extracted data is saved in `files/output/raw/`.
   - Operations summaries are logged in the `audit/` folder.

---

## 📁 File Structure Guide

Here is where everything is located in the project directory:

```text
d:\pii_redact\
├── pii_env\                        <-- Virtual environment folder (do not touch)
├── src\
│   ├── requirements.txt            <-- List of Python packages required
│   └── readme.md                   <-- Setup instructions (this file)
├── files\                          <-- Put the files you want to redact here!
│   └── output\
│       ├── raw\                    <-- Intermediate raw excel text files
│       └── redacted\               <-- Your finalized masked/redacted output files!
├── audit\                          <-- Operations logs and spreadsheets generated
├── secrets\                        <-- Forensic logs containing compliance details
│   ├── pii_forensic_log.jsonl      <-- Tracks all masking decisions (SKIP/REDACT)
│   └── unmatched_structural_labels_debug.log  <-- Debug log for structural headers
├── pii_rules.yaml                  <-- Tweak configuration rules here!
└── dual_redaction_pipeline.py      <-- Main executable script
```

### Where to Keep Files:
- **Input Files:** Place your target documents (e.g. `document.pdf` or `prefill.xlsx`) directly in the `files/` folder.
- **Redacted Output:** Once the pipeline runs, check `files/output/redacted/` for files with `_redacted` appended to their names.
- **Unmasked Verification:** If you ever need to restore a redacted spreadsheet back to its original values for debugging, the unmasked output will appear in the `unmasked/` directory.

---

## ⚙️ How to Customize Rules & Tweak Settings

You can adjust how strictly the pipeline redacts data without changing any python code.

### Option A: Editing `pii_rules.yaml`
This configuration file governs structural extraction rules, trigger settings, allowlists, and regular expressions.

| Parameter | Location in YAML | What It Does / Tweak Effect |
| :--- | :--- | :--- |
| **`max_lookahead`** | `STRUCTURAL_SETTINGS` | **Scan Distance:** Defines how many empty columns the structural scanner will jump to find a value next to a header (e.g. searching 5 columns right of "Given name" for "John"). Increase it (e.g. to `5` or `6`) for poorly formatted PDFs/tables, or decrease it (e.g. to `2`) if columns are close together. |
| **`STRUCTURAL_LABELS`** | `STRUCTURAL_LABELS` | **Headers to Redact:** Add text headers here under `PERSON`, `DATE`, `ADDRESS`, or `COMPANY`. If a cell in an Excel sheet matches these headers, its adjacent values are automatically redacted. Add common document labels here to catch new headers. |
| **`ALLOWLIST`** | `ALLOWLIST` | **Whitelist Exceptions:** Add specific organization names or words (e.g., `"ASIC"`, `"ATO"`, `"Commonwealth Bank"`) that should **never** be redacted, even if they match structural headers or AI scans. |
| **`TIER_1_REGEX`** | `TIER_1_REGEX` | **Pattern Rules:** Define regular expressions for strict matches like Tax File Numbers (TFN), Australian Business Numbers (ABN), or phone numbers. If a value matches, it is masked instantly. |

### Option B: Tweak Settings in `dual_redaction_pipeline.py`
Open `dual_redaction_pipeline.py` in an editor to adjust engine performance parameters at the top under `0. Configuration`:

- **`SLM_OLLAMA_MODEL` (Line 57):** Set the AI model name. Defaults to `"gemma3:4b"`.
- **`SLM_NUM_THREAD` (Line 61):** Set how many processor cores the AI engine can use. Increase it for faster processing, or decrease it if your computer becomes laggy while running the script.
- **`GLINER_CONFIDENCE_THRESHOLD` (Line 68):** Set how sure the secondary scanner must be before tagging a candidate. Default is `0.85`. Lowering it (e.g. to `0.75`) will extract more items but may increase false-positive detections.
