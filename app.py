"""
PII Redaction Pipeline — Web Frontend
Flask server: file upload → pipeline execution → live logs → download
"""

import os
import re
import sys
import shutil
import subprocess
import tempfile
import threading
import queue
import json
import hmac
import base64
import io
import zipfile
import httpx
from pathlib import Path
from datetime import timedelta
from docx import Document
from urllib.parse import quote, unquote, parse_qs
from flask import Flask, request, jsonify, send_file, Response, render_template_string, session
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from flasgger import Swagger, swag_from

import crypto_utils
import db
import redaction_formats
from dashboard import dashboard_bp, _is_dashboard_admin

load_dotenv()

BASE_DIR      = Path(__file__).parent
FILES_DIR     = BASE_DIR / "files"
OUTPUT_DIR    = FILES_DIR / "output" / "redacted"
UNMASKED_DIR  = BASE_DIR / "unmasked"
REHYDRATE_UPLOADS_DIR = BASE_DIR / "rehydrate_uploads"
REHYDRATE_OUTPUT_DIR  = BASE_DIR / "rehydrated"
AUDIT_DIR     = BASE_DIR / "audit"
FILES_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
UNMASKED_DIR.mkdir(exist_ok=True)
REHYDRATE_UPLOADS_DIR.mkdir(exist_ok=True)
REHYDRATE_OUTPUT_DIR.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {".pdf", ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".png", ".jpg", ".jpeg", ".eml", ".msg"}
# Same allowlist as the redact-upload page (Open Question #3's answer: reuse
# the existing list as-is). Images pass this check but are rejected with a
# specific error at upload time (see rehydrate_upload()) — pixel "as is"
# redaction burns the box into the pixels with no searchable text layer left
# (redaction_formats.remask_image_from_original), so there's nothing for
# token-substitution to find without OCR (out of scope — Section 8 of the plan).
REHYDRATE_ALLOWED_EXTENSIONS = ALLOWED_EXTENSIONS

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-insecure-key-set-SECRET_KEY-in-.env")
# Session cookie lifetime — see wiki/feature-updates/phase-1/
# plan-uid-link-propagation-implementation.md. Without this, Flask's default
# (non-permanent) session cookie is cleared the moment the browser fully
# closes, so a signed-in uid picked up at / wouldn't survive to the next
# visit even though the cookie already carries it across every internal
# path (session.permanent must also be set True wherever uid is decrypted —
# see index()/api_link_context()/dashboard._dashboard_authorize()).
app.permanent_session_lifetime = timedelta(days=30)
app.register_blueprint(dashboard_bp)

# Shared secret for server-to-server API callers (sent as the X-API-Key header) —
# see wiki/feature-updates/phase-1/plan-api-key-implementation.md. Empty by
# default so an unset API_KEY can never match anything.
API_KEY = os.environ.get("API_KEY", "")

# Review timer (wiki/feature-updates/plan-timer-implementation.md): seconds with
# no mouse/keyboard input before the review timer pauses. The idle stretch is
# removed from the review time and logged separately as idle_seconds.
try:
    REVIEW_IDLE_SECONDS = max(1, int(os.environ.get("REVIEW_IDLE_SECONDS", "15")))
except ValueError:
    REVIEW_IDLE_SECONDS = 15

# DMS navigation target. The navbar button should open this URL with the current
# record uid appended as a query parameter.
DMS_BASE_URL = os.environ.get("DMS_BASE_URL", "http://localhost:5175")
DMS_PAGE_PATH = os.environ.get("DMS_PAGE_PATH", "dms")

# Agate Merger — the review page's "Next" button opens this URL in a new tab
# with the signed-in uid (encrypted) appended as ?uid=...
AGATE_MERGER_URL = os.environ.get("AGATE_MERGER_URL", "http://localhost:5015")
# Shared secret sent as X-Agate-Intake-Key when Next pushes the redacted files
# to Agate's POST /api/intake — must match Agate's AGATE_INTAKE_API_KEY.
AGATE_INTAKE_API_KEY = os.environ.get("AGATE_INTAKE_API_KEY", "")


# DMS backend API — resolves ?token=... handoff links (server-to-server call)
DMS_API_BASE_URL = os.environ.get("DMS_API_BASE_URL", "")
DMS_INTERNAL_API_KEY = os.environ.get("DMS_INTERNAL_API_KEY", "")
DMS_RESOLVE_PATH = os.environ.get("DMS_RESOLVE_PATH", "/api/redaction-handoff/token/{token}")
DMS_AUTH_HEADER = os.environ.get("DMS_AUTH_HEADER", "X-Internal-API-Key")
DMS_AUTH_SCHEME = os.environ.get("DMS_AUTH_SCHEME", "").strip()


def _decrypt_uid_param(raw: str) -> str:
    """Decrypt a uid query-param value, recovering from links where a
    literal '+' in the base64 payload wasn't percent-encoded (%2B) and so
    arrived here as a space per application/x-www-form-urlencoded query
    parsing. Base64 never legitimately contains a space, so this retry is
    safe and unambiguous."""
    try:
        return crypto_utils.decrypt(raw)
    except Exception:
        if " " not in raw:
            raise
        return crypto_utils.decrypt(raw.replace(" ", "+"))


def build_dms_href(uid=None):
    base = (DMS_BASE_URL or "").rstrip("/")
    page = (DMS_PAGE_PATH or "").lstrip("/")
    href = f"{base}/{page}" if base and page else (base or page or "#")
    if uid is not None:
        uid_value = str(uid)
        try:
            int(uid_value)
            uid_value = crypto_utils.encrypt(uid_value)
        except ValueError:
            pass
        href = f"{href}?uid={quote(uid_value, safe='')}"
    return href


def build_agate_href(uid=None):
    """Agate Merger link for the review page's Next button, or "" when no
    uid is signed in (Agate rejects Process All without one)."""
    base = (AGATE_MERGER_URL or "").rstrip("/")
    if not base or uid is None:
        return ""
    return f"{base}/?uid={quote(crypto_utils.encrypt(str(uid)), safe='')}"


def _resolve_handoff_token(token: str) -> dict:
    """Resolve a DMS handoff token into the owning process/file context."""
    if not token:
        return {}
    if not DMS_API_BASE_URL:
        print("[WARN] DMS token resolve skipped: DMS_API_BASE_URL is not configured")
        return {}
    if not DMS_INTERNAL_API_KEY:
        print("[WARN] DMS token resolve skipped: DMS_INTERNAL_API_KEY is not configured")
        return {}

    print(f"[DEBUG] Sending key: '{DMS_INTERNAL_API_KEY}'")
    print(f"[DEBUG] Key length: {len(DMS_INTERNAL_API_KEY)}")

    headers = {"Accept": "application/json"}
    if DMS_AUTH_HEADER:
        if DMS_AUTH_SCHEME:
            headers[DMS_AUTH_HEADER] = f"{DMS_AUTH_SCHEME} {DMS_INTERNAL_API_KEY}".strip()
        else:
            headers[DMS_AUTH_HEADER] = DMS_INTERNAL_API_KEY

    resolve_path = DMS_RESOLVE_PATH.strip()
    if "{token}" in resolve_path:
        resolve_path = resolve_path.format(token=quote(token, safe=""))
    else:
        resolve_path = f"{resolve_path.rstrip('/')}/{quote(token, safe='')}"
    url = f"{DMS_API_BASE_URL.rstrip('/')}{resolve_path}"

    try:
        resp = httpx.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"[WARN] DMS token resolve failed: {resp.status_code} {resp.text}")
            print(f"[WARN] DMS URL: {url}")
            print(f"[WARN] DMS auth header: {DMS_AUTH_HEADER}")
            return {}
        data = resp.json()

        return {
            "process_id": data.get("process_id"),
            "file_id": data.get("file_id"),
            "storage_path": data.get("storage_path"),
            "client_id": data.get("client_id"),
            "job_id": data.get("job_id"),
        }
    except Exception as e:
        print(f"[WARN] DMS token resolve request failed: {e}")
        return {}


_DMS_TOKEN_CATEGORY_RE = re.compile(r"^<\*(?P<category>.+?)_\d+\*>$")


def _remember_dms_storage_path(file_id: str, storage_path: str) -> None:
    """Keep the DMS on-disk path for this redactor file_id in the browser
    session so manual Add & Re-redact can push updates back automatically."""
    if not file_id or not storage_path:
        return
    by_file = session.setdefault("dms_storage_by_file", {})
    by_file[file_id] = storage_path


def _dms_mapping_from_rows(rows: list) -> list:
    mapping = []
    for row in rows:
        redacted_text = row["redacted_text"]
        match = _DMS_TOKEN_CATEGORY_RE.match(redacted_text)
        mapping.append({
            "sourceText": row["source_text"],
            "redactedText": redacted_text,
            "category": match.group("category") if match else None,
        })
    return mapping


def _write_dms_sidecar_exports(dest_file: Path, mapping: list) -> None:
    """Refresh DMS json/ + reference/ exports beside the stored redacted file."""
    redacted_dir = dest_file.parent
    stem_name = dest_file.name
    json_path = redacted_dir / "json" / f"{stem_name}.json"
    ref_path = redacted_dir / "reference" / f"{stem_name}.docx"

    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")

    ref_path.parent.mkdir(parents=True, exist_ok=True)
    document = Document()
    document.add_heading(stem_name, level=2)
    table = document.add_table(rows=1, cols=2)
    table.style = "Light Grid Accent 1"
    header = table.rows[0].cells
    header[0].text = "Original text"
    header[1].text = "Masked value"
    for entry in mapping:
        cells = table.add_row().cells
        cells[0].text = entry["sourceText"]
        cells[1].text = entry["redactedText"]
    document.save(ref_path)


def _sync_redacted_to_dms(file_id: str, redacted_path: Path, rows: list) -> bool:
    """Copy the updated redacted output to the DMS storage path captured at
    handoff time, and refresh the sibling json/reference exports."""
    storage_path_str = session.get("dms_storage_by_file", {}).get(file_id)
    if not storage_path_str:
        return False

    dest = Path(storage_path_str)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(redacted_path, dest)
        _write_dms_sidecar_exports(dest, _dms_mapping_from_rows(rows))
        print(f"[INFO] Synced redacted output to DMS storage: {dest}")
        return True
    except Exception as e:
        print(f"[WARN] DMS storage sync failed for {file_id} -> {dest}: {e}")
        return False


app.config["SWAGGER"] = {
    "title": "PII Redaction Pipeline API",
    "uiversion": 3,
    "specs_route": "/apidocs/",
}
Swagger(app, template={
    "info": {
        "title": "PII Redaction Pipeline API",
        "description": "Upload, redact, review and rehydrate PII in client documents.",
        "version": "1.0.0",
    },
    "securityDefinitions": {
        "ApiKeyAuth": {
            "type": "apiKey",
            "name": "X-API-Key",
            "in": "header",
            "description": "Server-to-server alternative to the browser session — see "
                            "wiki/feature-updates/phase-1/plan-api-key-implementation.md.",
        }
    },
})

_job_queues: dict = {}
_job_lock = threading.Lock()

# Separate from _job_queues (upload/pipeline jobs) even though both are keyed
# by process_id, so a redact-request run never collides with a still-live or
# already-finished upload stream for the same process_id — see
# wiki/feature-updates/phase-1/plan-api-redaction-implementation.md, Section 3.7.
_redact_queues: dict = {}

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PII Redaction Pipeline</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #f3f5fb;
  --surface: #ffffff;
  --surface2: #f1f3f9;
  --border: rgba(23,25,35,0.08);
  --border-hover: rgba(79,70,229,0.4);
  --text: #171923;
  --text-muted: #6b7280;
  --accent: #4f46e5;
  --accent2: #2563eb;
  --accent-glow: rgba(79,70,229,0.10);
  --green: #15803d;
  --green-bg: rgba(21,128,61,0.09);
  --red: #b91c1c;
  --red-bg: rgba(185,28,28,0.08);
  --radius: 14px;
  --radius-sm: 8px;
  --shadow: 0 1px 2px rgba(23,25,35,.04), 0 12px 28px -16px rgba(79,70,229,.16);
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

html, body { height: 100%; }
body {
  font-family: 'Inter', system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  height: 100vh;
  overflow: hidden;
  display: flex; flex-direction: column;
  background-image:
    radial-gradient(ellipse 60% 40% at 12% -10%, rgba(79,70,229,0.10) 0%, transparent 60%),
    radial-gradient(ellipse 50% 40% at 100% 0%, rgba(37,99,235,0.08) 0%, transparent 55%);
  background-attachment: fixed;
}

/* ── Header ── */
header {
  display: flex; align-items: center; gap: 14px;
  padding: 16px 32px;
  border-bottom: 1px solid var(--border);
  background: rgb(191 216 255 / 80%);
  backdrop-filter: blur(12px);
  position: sticky; top: 0; z-index: 10;
}
.logo {
  width: 36px; height: 36px; border-radius: 10px;
  background: linear-gradient(135deg, #4f46e5, #2563eb);
  box-shadow: 0 4px 14px -4px rgba(79,70,229,.5);
  display: flex; align-items: center; justify-content: center; flex-shrink: 0;
}
.logo svg { width: 20px; height: 20px; color: #fff; }
.header-text h1 { font-size: 1rem; font-weight: 600; letter-spacing: -0.01em; }
.header-text p  { font-size: 0.72rem; color: var(--text-muted); margin-top: 1px; }
.pill {
  margin-left: auto; font-size: 0.7rem; font-weight: 500;
  padding: 4px 10px; border-radius: 20px;
  background: var(--green-bg); color: var(--green);
  border: 1px solid rgba(21,128,61,0.2);
  display: flex; align-items: center; gap: 5px;
}
.pill::before { content:''; width:6px; height:6px; border-radius:50%; background:var(--green); }
.header-actions {
  margin-left: auto; display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
}
.nav-link-btn {
  font-size: .78rem; font-weight: 600; color: var(--accent); text-decoration: none;
  padding: 8px 12px; border-radius: 999px; border: 1px solid rgba(79,70,229,.16);
  background: rgba(255,255,255,.75); transition: all .15s ease;
}
.nav-link-btn:hover {
  background: var(--accent-glow); border-color: rgba(79,70,229,.28); transform: translateY(-1px);
}

/* ── Layout ── */
main {
  max-width: 1500px; margin: 40px auto; padding: 0 24px 20px; width: 100%;
  flex: 1 1 auto; min-height: 0;
  display: flex; flex-direction: column;
}
.app-grid {
  display: grid; grid-template-columns: 420px 1fr; gap: 28px; align-items: stretch;
  transition: grid-template-columns .28s ease, gap .28s ease;
  flex: 1 1 auto; min-height: 0;
}
.app-grid.sidebar-collapsed { grid-template-columns: 0px 1fr; gap: 0; }
.col-left, .col-right { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.col-left { position: relative; }
.col-left-content {
  display: flex; flex-direction: column; min-width: 0; min-height: 0; flex: 1 1 auto;
  overflow-y: auto; overflow-x: hidden; padding-right: 4px;
  transition: opacity .2s ease;
}
.app-grid.sidebar-collapsed .col-left-content { opacity: 0; pointer-events: none; }
.col-right { overflow-y: auto; overflow-x: hidden; padding-right: 4px; }
.col-left-content::-webkit-scrollbar, .col-right::-webkit-scrollbar { width: 5px; }
.col-left-content::-webkit-scrollbar-track, .col-right::-webkit-scrollbar-track { background: transparent; }
.col-left-content::-webkit-scrollbar-thumb, .col-right::-webkit-scrollbar-thumb { background: var(--border); border-radius: 99px; }

.sidebar-tab {
  position: absolute; top: 50%; right: -14px; transform: translateY(-50%);
  width: 28px; height: 56px; border-radius: 8px;
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  border: none; box-shadow: 0 4px 14px -4px rgba(79,70,229,.6);
  display: flex; align-items: center; justify-content: center;
  cursor: pointer; z-index: 5; color: #fff;
  transition: transform .15s ease, box-shadow .15s ease;
}
.sidebar-tab:hover { transform: translateY(-50%) scale(1.08); box-shadow: 0 6px 18px -4px rgba(79,70,229,.75); }
.sidebar-tab svg { width: 14px; height: 14px; transition: transform .28s ease; }
.app-grid.sidebar-collapsed .sidebar-tab svg { transform: rotate(180deg); }

@media (max-width: 980px) {
  html, body { height: auto; }
  body { overflow: visible; display: block; }
  main { flex: none; min-height: 0; height: auto; display: block; }
  .app-grid { grid-template-columns: 1fr; flex: none; height: auto; min-height: 0; }
  .app-grid.sidebar-collapsed { grid-template-columns: 1fr; }
  .app-grid.sidebar-collapsed .col-left-content { opacity: 1; pointer-events: auto; }
  .col-left-content, .col-right { height: auto; overflow: visible; }
  .col-right { position: static; }
  .sidebar-tab { display: none; }
}
.right-placeholder {
  background: var(--surface); border: 1px dashed var(--border); border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 48px 28px; text-align: center; color: var(--text-muted);
}
.right-placeholder .ph-icon {
  width: 52px; height: 52px; border-radius: 14px; margin: 0 auto 16px;
  background: var(--accent-glow); color: var(--accent);
  display: flex; align-items: center; justify-content: center;
}
.right-placeholder .ph-icon svg { width: 24px; height: 24px; }
.right-placeholder h3 { font-size: .95rem; font-weight: 600; color: var(--text); margin-bottom: 6px; }
.right-placeholder p { font-size: .82rem; line-height: 1.6; max-width: 320px; margin: 0 auto; }

.section-label {
  font-size: 0.7rem; font-weight: 700; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--text-muted);
  margin-bottom: 10px; display: flex; align-items: center; gap: 8px;
}
.section-label::before {
  content:''; width:7px; height:7px; border-radius:2px; flex-shrink:0;
  background: linear-gradient(135deg, var(--accent), var(--accent2));
}
.section-label::after { content:''; flex:1; height:1px; background:var(--border); }

/* ── Review timer (Output header, right corner) ── */
.output-header { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
.output-header .section-label { flex: 1; margin-bottom: 0; }
.review-timer {
  display: inline-flex; align-items: center; flex-wrap: wrap; justify-content: flex-end;
  gap: 4px 8px; min-width: 0;
  padding: 4px 12px; border-radius: 12px;
  background: var(--surface); border: 1px solid var(--border);
  font-size: .74rem; color: var(--text-muted);
}
.review-timer > span { white-space: nowrap; }
.review-timer[hidden] { display: none; }
.review-timer .rt-value { font-variant-numeric: tabular-nums; font-weight: 600; color: var(--text); }
.review-timer .rt-sep { opacity: .5; }
.review-timer .rt-state { display: inline-flex; align-items: center; gap: 5px; }
.review-timer .rt-state::before {
  content: ''; width: 7px; height: 7px; border-radius: 50%;
  background: var(--text-muted); opacity: .5;
}
.review-timer.running .rt-state { color: var(--green); }
.review-timer.running .rt-state::before { background: var(--green); opacity: 1; }

/* ── Upload zone ── */
.upload-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
}

/* ── Client / Job selection ── */
.selector-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 18px;
  display: flex; flex-direction: column; gap: 14px;
}
.field-group { display: flex; flex-direction: column; gap: 6px; position: relative; }
.field-group label {
  font-size: .75rem; font-weight: 600; color: var(--text-muted);
}
.field-input {
  background: var(--surface2); border: 1px solid var(--border);
  border-radius: var(--radius-sm); color: var(--text);
  padding: 10px 12px; font-size: .85rem; font-family: inherit;
  outline: none; transition: border-color .15s;
}
.field-input:focus { border-color: var(--border-hover); }
.field-input:disabled { opacity: .45; cursor: not-allowed; }
select.field-input { appearance: none; cursor: pointer; }

.drop-zone {
  padding: 52px 24px; text-align: center; cursor: pointer;
  transition: background .2s, border-color .2s;
  border: 2px dashed transparent;
  border-radius: var(--radius);
  position: relative;
}
.drop-zone.dragover {
  background: var(--accent-glow);
  border-color: var(--accent);
}
.drop-zone:hover .upload-icon-wrap { transform: translateY(-3px); }
.upload-icon-wrap {
  width: 64px; height: 64px; border-radius: 16px; margin: 0 auto 18px;
  background: linear-gradient(135deg, rgba(99,179,237,0.15), rgba(99,179,237,0.05));
  border: 1px solid rgba(99,179,237,0.2);
  display: flex; align-items: center; justify-content: center;
  transition: transform .25s cubic-bezier(.34,1.56,.64,1);
}
.upload-icon-wrap svg { width: 28px; height: 28px; color: var(--accent); }
.drop-zone h2 { font-size: 1rem; font-weight: 600; margin-bottom: 6px; }
.drop-zone p  { font-size: 0.82rem; color: var(--text-muted); line-height: 1.5; }
.drop-zone p span { color: var(--accent); }
#file-input { display: none; }

/* ── File list ── */
.file-list { display: flex; flex-direction: column; gap: 1px; }
.file-item {
  display: flex; align-items: center; gap: 12px;
  padding: 11px 18px;
  border-top: 1px solid var(--border);
  font-size: 0.85rem;
  animation: slideIn .18s ease;
}
@keyframes slideIn { from { opacity:0; transform:translateY(-4px) } to { opacity:1; transform:none } }
.file-icon {
  width: 32px; height: 32px; border-radius: 8px; flex-shrink:0;
  display:flex; align-items:center; justify-content:center; font-size:.65rem; font-weight:700;
}
.file-icon.pdf  { background: rgba(197,48,48,.10);  color:#c53030; }
.file-icon.xlsx { background: rgba(47,133,90,.10);   color:#2f855a; }
.file-icon.docx { background: rgba(43,108,176,.10);  color:#2b6cb0; }
.file-icon.img  { background: rgba(128,90,213,.10);  color:#805ad5; }
.file-name { flex:1; font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.file-size { color: var(--text-muted); font-size:.75rem; flex-shrink:0; }
.remove-btn {
  background:none; border:none; cursor:pointer; color:var(--text-muted);
  width:24px; height:24px; border-radius:6px; display:flex; align-items:center; justify-content:center;
  transition: background .15s, color .15s; flex-shrink:0;
}
.remove-btn:hover { background:var(--red-bg); color:var(--red); }

/* ── Actions ── */
.actions { margin-top: 20px; display: flex; gap: 10px; align-items: center; }
.btn {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 10px 22px; border-radius: var(--radius-sm); border: none;
  font-size: 0.875rem; font-weight: 600; cursor: pointer;
  transition: all .18s; white-space:nowrap; font-family:inherit;
}
.btn:disabled { opacity: .4; cursor: not-allowed; pointer-events: none; }
.btn-primary {
  background: linear-gradient(135deg, #2d6fa4, #63b3ed);
  color: #fff; box-shadow: 0 2px 12px rgba(99,179,237,0.25);
}
.btn-primary:not(:disabled):hover {
  box-shadow: 0 4px 20px rgba(99,179,237,0.4); transform: translateY(-1px);
}
.next-step-row { display: flex; justify-content: flex-end; margin-top: 16px; }
.next-step-row .btn { text-decoration: none; }
.btn.is-disabled { opacity: .4; cursor: not-allowed; }
.btn.is-disabled:hover { box-shadow: 0 2px 12px rgba(99,179,237,0.25); transform: none; }
.btn-ghost {
  background: var(--surface2); color: var(--text-muted);
  border: 1px solid var(--border);
}
.btn-ghost:not(:disabled):hover { color: var(--text); border-color: rgba(255,255,255,.15); }
.file-count {
  margin-left:auto; font-size:.78rem; color:var(--text-muted);
}

/* ── Progress ── */
.progress-section { margin-top: 32px; display: none; }
.progress-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:10px; }
.progress-label { font-size:.82rem; font-weight:500; display:flex; align-items:center; gap:8px; }
.spinner {
  width:14px; height:14px; border:2px solid var(--border);
  border-top-color:var(--accent); border-radius:50%;
  animation: spin .7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg) } }
.spinner.done { animation:none; border-color:var(--green); border-top-color:var(--green); }
.progress-pct { font-size:.78rem; color:var(--text-muted); }
.progress-track {
  height: 6px; background: var(--surface2); border-radius: 99px; overflow:hidden;
  border: 1px solid var(--border);
}
.progress-fill {
  height:100%; width:0%; border-radius:99px;
  background: linear-gradient(90deg, #2d6fa4, #63b3ed, #90cdf4);
  background-size: 200% 100%;
  animation: shimmer 1.5s linear infinite;
  transition: width .4s cubic-bezier(.4,0,.2,1);
}
@keyframes shimmer { to { background-position: -200% 0 } }
.progress-fill.done { animation:none; background:#48bb78; }

/* ── Log console ── */
.console-section { margin-top: 20px; display: none; }
.console-header {
  display:flex; align-items:center; gap:8px; margin-bottom:8px;
  font-size:.75rem; color:var(--text-muted);
}
.console-dots { display:flex; gap:5px; }
.console-dots span {
  width:10px; height:10px; border-radius:50%;
}
.console-dots .d1{background:#fc8181;} .console-dots .d2{background:#f6e05e;} .console-dots .d3{background:#68d391;}
.console-title { font-weight:600; font-size:.72rem; letter-spacing:.04em; }
pre#log {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 16px 18px;
  font-size: 0.76rem; line-height: 1.7;
  max-height: 320px; overflow-y: auto;
  white-space: pre-wrap; word-break: break-all;
  color: #4338ca;
  font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
}
pre#log::-webkit-scrollbar { width:4px; }
pre#log::-webkit-scrollbar-track { background:transparent; }
pre#log::-webkit-scrollbar-thumb { background:var(--border); border-radius:99px; }

/* ── Results ── */
.results-section { margin-top: 0; display: none; }
.result-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  overflow: hidden;
}
.result-item {
  display:flex; align-items:center; gap:12px;
  padding: 13px 18px; font-size:.85rem;
  border-bottom: 1px solid var(--border);
  animation: slideIn .2s ease;
}
.result-item:last-child { border-bottom:none; }
.result-file-icon {
  width:34px; height:34px; border-radius:8px; flex-shrink:0;
  background: var(--green-bg); color:var(--green);
  display:flex; align-items:center; justify-content:center;
}
.result-file-icon svg { width:16px; height:16px; }
.result-name { flex:1; font-weight:500; }
.badge {
  font-size:.67rem; font-weight:700; padding:3px 9px; border-radius:20px;
  letter-spacing:.05em; text-transform:uppercase;
}
.badge-ok  { background:var(--green-bg); color:var(--green); border:1px solid rgba(72,187,120,.2); }
.dl-btn {
  display:inline-flex; align-items:center; gap:6px;
  padding:7px 14px; border-radius:var(--radius-sm);
  background:var(--surface2); border:1px solid var(--border);
  color:var(--accent); font-size:.78rem; font-weight:600;
  text-decoration:none; transition:all .15s;
}
.dl-btn:hover { background:var(--accent-glow); border-color:var(--border-hover); }
.dl-btn svg { width:13px; height:13px; }
.dl-btn.disabled { opacity:.45; pointer-events:none; cursor:not-allowed; }

.ack-check {
  display:flex; align-items:flex-start; gap:10px;
  padding:14px 18px; border-radius:var(--radius);
  background:rgba(217,119,6,0.08); border:1px solid rgba(217,119,6,0.3);
  font-size:.85rem; font-weight:500; color:#92400e; cursor:pointer; user-select:none;
}
.ack-check input[type="checkbox"] {
  width:17px; height:17px; margin-top:1px; flex-shrink:0; accent-color:#d97706; cursor:pointer;
}

.empty-state { padding:28px; text-align:center; color:var(--text-muted); font-size:.85rem; }

/* ── Preview + reference table ── */
.file-tabs { display:flex; gap:6px; margin-bottom:14px; flex-wrap:wrap; }
.file-tab {
  background:var(--surface2); border:1px solid var(--border); color:var(--text-muted);
  padding:7px 14px; border-radius:99px; font-size:.78rem; font-weight:600;
  cursor:pointer; font-family:inherit; transition:all .15s;
}
.file-tab:hover { color:var(--text); }
.file-tab.active {
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  border-color: transparent; color:#fff;
  box-shadow: 0 4px 12px -4px rgba(79,70,229,.45);
}
.file-tab-more-wrap { position:relative; }
.file-tab-more { display:flex; align-items:center; gap:5px; }
.file-tab-more::after {
  content:''; width:0; height:0;
  border-left:4px solid transparent; border-right:4px solid transparent;
  border-top:5px solid currentColor;
}
.file-tab-dropdown {
  position:absolute; top:calc(100% + 8px); left:0; z-index:20;
  width:240px; max-width:80vw;
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-sm);
  box-shadow:0 12px 28px -8px rgba(23,25,35,.18);
  padding:8px; display:flex; flex-direction:column; gap:6px;
}
.file-tab-search {
  background:var(--surface2); border:1px solid var(--border); border-radius:var(--radius-sm);
  color:var(--text); font-size:.78rem; font-family:inherit; padding:7px 10px; width:100%;
}
.file-tab-search:focus { border-color:var(--border-hover); }
.file-tab-dropdown-list { max-height:220px; overflow-y:auto; display:flex; flex-direction:column; gap:2px; }
.file-tab-dropdown-list::-webkit-scrollbar { width:6px; }
.file-tab-dropdown-list::-webkit-scrollbar-thumb { background:var(--border); border-radius:99px; }
.file-tab-dropdown-item {
  background:none; border:none; text-align:left; padding:7px 8px; border-radius:var(--radius-sm);
  font-size:.78rem; color:var(--text); cursor:pointer; font-family:inherit;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
.file-tab-dropdown-item:hover, .file-tab-dropdown-item.active { background:var(--surface2); }
.file-tab-dropdown-empty { padding:10px 8px; font-size:.78rem; color:var(--text-muted); text-align:center; }

.detail-grid { display:grid; grid-template-columns: 7fr 3fr; gap:20px; align-items:start; }
@media (max-width: 760px) { .detail-grid { grid-template-columns: 1fr; } }

.preview-col { display:flex; flex-direction:column; gap:10px; min-width:0; }
.preview-toolbar {
  display:flex; align-items:center; gap:10px; flex-wrap:wrap;
  position: sticky; top: 0; z-index: 4;
  background: var(--bg); padding: 8px 0; margin: -8px 0 0;
}
.tool-btn {
  display:inline-flex; align-items:center; gap:6px;
  padding:6px 12px; border-radius:var(--radius-sm); border:1px solid var(--border);
  background:var(--surface2); color:var(--text-muted);
  font-size:.78rem; font-weight:600; cursor:pointer; font-family:inherit;
  transition:all .15s;
}
.tool-btn svg { width:13px; height:13px; }
.tool-btn:hover { color:var(--text); border-color:var(--border-hover); }
.tool-btn.active {
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  border-color: transparent; color:#fff;
}
.preview-hint { font-size:.72rem; color:var(--text-muted); }
.preview-page-indicator {
  margin-left:auto; display:inline-flex; align-items:center; gap:6px; min-width:0;
  font-size:.74rem; color:var(--text-muted);
}
.preview-page-indicator[hidden] { display:none; }
.preview-page-indicator .ppi-name {
  max-width:260px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  font-weight:600; color:var(--text);
}
.preview-page-indicator .ppi-page {
  white-space:nowrap; font-variant-numeric:tabular-nums;
  padding:2px 8px; border-radius:99px; background:var(--accent-glow); color:var(--accent); font-weight:600;
}

.preview-pane {
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
  box-shadow: var(--shadow);
  padding:14px; max-height:640px; overflow:auto; display:flex; flex-direction:column; gap:12px;
  position:relative;
}
.preview-page { width:100%; border-radius:6px; border:1px solid var(--border); display:block; }
.preview-page-wrap { position:relative; width:100%; }
.preview-page-wrap .preview-page { pointer-events:none; -webkit-user-drag:none; user-select:none; }
.preview-pane.select-mode .preview-page-wrap { cursor:crosshair; }
.marquee-box {
  position:absolute; pointer-events:none; z-index:3; border-radius:2px;
  border:1.5px dashed var(--accent); background:rgba(79,70,229,.15);
}
.preview-sheet-name { font-size:.72rem; font-weight:700; color:var(--text-muted); text-transform:uppercase; letter-spacing:.05em; }
.preview-tabs { display:flex; flex-wrap:wrap; gap:4px; margin-bottom:10px; border-bottom:1px solid var(--border); padding-bottom:6px; position:sticky; top:0; background:var(--bg); z-index:2; }
.preview-tab { font-size:.72rem; font-weight:600; padding:5px 12px; border:1px solid var(--border); border-bottom:none; border-radius:6px 6px 0 0; background:transparent; color:var(--text-muted); cursor:pointer; white-space:nowrap; transition:background .12s, color .12s; }
.preview-tab:hover { background:var(--accent-glow); color:var(--text); }
.preview-tab.active { background:var(--accent-glow); color:var(--text); border-color:var(--accent); }
.preview-table { border-collapse:collapse; font-size:.75rem; width:100%; margin-bottom:10px; }
.preview-table td { border:1px solid var(--border); padding:5px 8px; white-space:nowrap; cursor:pointer; transition:background .12s; }
.preview-table td:hover { background:var(--accent-glow); }

.table-pane {
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
  box-shadow: var(--shadow);
  padding:14px; display:flex; flex-direction:column; gap:12px;
}
.ref-table-scroll {
  max-height: 500px; overflow-y: auto; overflow-x: hidden;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
}
.ref-table-scroll::-webkit-scrollbar { width: 5px; }
.ref-table-scroll::-webkit-scrollbar-track { background: transparent; }
.ref-table-scroll::-webkit-scrollbar-thumb { background: var(--border); border-radius: 99px; }
.ref-table { width:100%; border-collapse:collapse; font-size:.8rem; }
.ref-table th { text-align:left; font-size:.68rem; text-transform:uppercase; letter-spacing:.05em; color:var(--text-muted); padding:6px 8px; border-bottom:1px solid var(--border); position:sticky; top:0; background:var(--surface); z-index:1; }
.ref-table td { padding:8px; border-bottom:1px solid var(--border); vertical-align:middle; }
.ref-table code { background:var(--surface2); padding:2px 6px; border-radius:4px; color:var(--accent); font-size:.76rem; }
.row-action-btn {
  background:none; border:1px solid var(--border); color:var(--text-muted); cursor:pointer;
  width:24px; height:24px; border-radius:6px; font-size:.75rem; margin-right:4px;
}
.row-action-btn:hover { color:var(--text); border-color:var(--border-hover); }

.add-row-form { display:flex; gap:8px; flex-wrap:wrap; }
.add-row-form input { flex:1; min-width:120px; }
.category-field { display:flex; gap:6px; flex:1; min-width:160px; }
.category-field select { flex:1; min-width:0; }
.add-category-btn {
  background:var(--surface2); border:1px solid var(--border); color:var(--text-muted);
  width:34px; flex-shrink:0; border-radius:var(--radius-sm); cursor:pointer;
  font-size:1rem; font-weight:600; font-family:inherit; transition:all .15s;
}
.add-category-btn:hover { color:var(--text); border-color:var(--border-hover); }
.detail-error { font-size:.8rem; color:var(--red); background:var(--red-bg); border-radius:var(--radius-sm); padding:8px 12px; display:none; }

/* ── Guided Tour ── */
#tour-overlay {
  position: fixed; inset: 0; z-index: 300;
  background: transparent;
  display: none;
}
#tour-overlay.active { display: block; }
#tour-overlay.tour-centered-mode { background: rgba(15,17,23,0.55); }
.tour-highlight {
  position: relative !important; z-index: 301 !important;
  pointer-events: none !important;
  border-radius: var(--radius-sm);
  box-shadow: 0 0 0 4px rgba(79,70,229,.5), 0 0 0 9999px rgba(15,17,23,0.55) !important;
}
#tour-card {
  position: fixed; z-index: 302;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 18px 20px;
  width: 300px;
  display: none;
}
#tour-card.active { display: block; }
#tour-card.centered {
  top: 50% !important; left: 50% !important; transform: translate(-50%, -50%);
  width: 340px; text-align: center;
}
#tour-card h4 { font-size: .95rem; font-weight: 600; margin: 0 0 6px; padding-right: 16px; }
#tour-card p { font-size: .82rem; color: var(--text-muted); line-height: 1.5; margin: 0 0 14px; }
.tour-card-footer { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
#tour-card.centered .tour-card-footer { justify-content: center; }
.tour-step-count { font-size: .72rem; color: var(--text-muted); white-space: nowrap; }
.tour-card-actions { display: flex; gap: 8px; }
.tour-btn {
  border: 1px solid var(--border); background: var(--surface2); color: var(--text);
  border-radius: 999px; padding: 6px 14px; font-size: .78rem; font-weight: 600;
  cursor: pointer; font-family: inherit;
}
.tour-btn:hover { border-color: var(--border-hover); }
.tour-btn-primary { background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #fff; border: none; }
.tour-close-btn {
  position: absolute; top: 10px; right: 12px; background: none; border: none;
  color: var(--text-muted); cursor: pointer; font-size: 1.1rem; line-height: 1; padding: 2px;
}
.tour-close-btn:hover { color: var(--text); }
.tour-help-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; border-radius: 50%; flex-shrink: 0;
  border: 1px solid rgba(79,70,229,.16); background: rgba(255,255,255,.75); color: var(--accent);
  cursor: pointer; font-size: .82rem; font-weight: 700; font-family: inherit; transition: all .15s ease;
}
.tour-help-btn:hover { background: var(--accent-glow); border-color: rgba(79,70,229,.28); transform: translateY(-1px); }
</style>
</head>
<body>

<header>
  <div class="logo">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
  </div>
  <div class="header-text">
    <h1>PII Redaction Pipeline</h1>
    <p>Powered by GLiNER &amp; Ollama &mdash; </p>
  </div>
  <div class="header-actions">
    <a href="{{ dms_href }}" class="nav-link-btn">DMS</a>
    <a href="/rehydrate" class="nav-link-btn">Rehydrate a file &rarr;</a>
    {% if is_dashboard_admin %}<a href="/dashboard" class="nav-link-btn">Dashboard</a>{% endif %}
    <button type="button" class="tour-help-btn" id="tour-help-btn" title="Replay the guided tour" aria-label="Replay the guided tour">?</button>
  </div>
</header>

<main>
<div class="app-grid">

  <div class="col-left" >

    <button class="sidebar-tab" id="sidebar-toggle" type="button" aria-expanded="true" aria-label="Hide panel" title="Hide panel">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>
    </button>

    <div class="col-left-content">

    <!-- Client / Job -->
    <div class="section-label">Client &amp; Job</div>
    <div class="selector-card">
      <div class="field-group">
        <label for="process-name-input">Process Name</label>
        <input type="text" id="process-name-input" class="field-input" placeholder="e.g. Monthly payroll batch" autocomplete="off">
      </div>
      <div class="field-group">
        <label for="client-select">Client</label>
        <select id="client-select" class="field-input">
          <option value="">Select a client&hellip;</option>
        </select>
      </div>
      <div class="field-group">
        <label for="job-select">Job</label>
        <select id="job-select" class="field-input" disabled>
          <option value="">Select a client first</option>
        </select>
      </div>
      <div class="field-group">
        <label for="format-select">Output Format</label>
        <select id="format-select" class="field-input">
          <option value="as_is" selected>As it is</option>
          <option value="excel">Excel</option>
        </select>
      </div>
    </div>

    <!-- Upload -->
    <div class="section-label" style="margin-top:28px">Upload Documents</div>
    <div class="upload-card">
      <div class="drop-zone" id="drop-zone">
        <label for="file-input" style="cursor:pointer;display:block">
          <div class="upload-icon-wrap">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
              <path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>
              <polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/>
            </svg>
          </div>
          <h2>Drop files here or <span>browse</span></h2>
          <p>Supports PDF, XLSX, XLS, DOCX, DOC, PPTX, Email, images &nbsp;&middot;&nbsp; Up to 200 MB per file</p>
        </label>
        <input type="file" id="file-input" multiple accept=".pdf,.xlsx,.xls,.docx,.doc,.pptx,.png,.jpg,.jpeg,.eml,.msg">
      </div>
      <div class="file-list" id="file-list"></div>
    </div>

    <div class="actions">
      <button class="btn btn-primary" id="run-btn" disabled onclick="runPipeline()">
        <svg viewBox="0 0 24 24" fill="currentColor" style="width:14px;height:14px"><polygon points="5 3 19 12 5 21 5 3"/></svg>
        Run Redaction
      </button>
      <button class="btn btn-ghost" id="clear-btn" disabled onclick="clearFiles()">Clear all</button>
      <span class="file-count" id="file-count"></span>
    </div>

    <!-- Progress -->
    <div class="progress-section" id="progress-section">
      <div class="progress-header">
        <div class="progress-label">
          <div class="spinner" id="spinner"></div>
          <span id="status-text">Uploading…</span>
        </div>
        <div style="display:flex; align-items:center; gap:10px;">
          <button type="button" class="btn btn-ghost" id="cancel-batch-btn" style="display:none; padding:2px 10px; font-size:.78rem;" onclick="cancelBatch()">Cancel</button>
          <span class="progress-pct" id="pct-text">0%</span>
        </div>
      </div>
      <div class="progress-track"><div class="progress-fill" id="prog"></div></div>
    </div>

    <!-- Console -->
    <div class="console-section" id="console-section">
      <div class="console-header">
        <div class="console-dots"><span class="d1"></span><span class="d2"></span><span class="d3"></span></div>
        <span class="console-title">Live Output</span>
      </div>
      <pre id="log"></pre>
    </div>

    </div>

  </div>

  <div class="col-right">

    <div class="output-header">
      <div class="section-label" style="margin-top:0">Output</div>
      <div class="review-timer" id="review-timer" hidden
           title="System: pipeline processing time. Review: your active time from the first Select click to the latest Download. Idle: time with no mouse/keyboard input. Elapsed: total time (Review + Idle + time on other tabs).">
        <span>System <span class="rt-value" id="rt-system">--:--:--</span></span>
        <span class="rt-sep">&middot;</span>
        <span>Review <span class="rt-value" id="rt-review">00:00:00</span></span>
        <span class="rt-sep">&middot;</span>
        <span>Idle <span class="rt-value" id="rt-idle">00:00:00</span></span>
        <span class="rt-sep">&middot;</span>
        <span>Elapsed <span class="rt-value" id="rt-elapsed">00:00:00</span></span>
        <span class="rt-state" id="rt-state">Click Select to start</span>
      </div>
    </div>

    <div class="right-placeholder" id="right-placeholder">
      <div class="ph-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="9" y1="15" x2="15" y2="15"/></svg>
      </div>
      <h3>No output yet</h3>
      <p>Upload files on the left and run redaction — the download list, preview, and reference table will show up here.</p>
    </div>

    <!-- Results -->
    <div class="results-section" id="results-section">
      <div id="detail-section" style="display:none;">
        <div class="empty-state" id="detail-note" style="display:none; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);"></div>
        <div class="file-tabs" id="file-tabs"></div>
        <div class="detail-grid" id="detail-grid">
          <div class="preview-col">
            <div class="preview-toolbar" id="preview-toolbar">
              <button type="button" class="tool-btn" id="select-tool-btn" title="Draw a box around a missed value in the preview">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-dasharray="3 3"><rect x="3" y="3" width="18" height="18" rx="2"/></svg>
                Select
              </button>
              <span class="preview-hint" id="preview-hint">Drag a box around a PII value to send it to the redaction table.</span>
              <span class="preview-page-indicator" id="preview-page-indicator" hidden>
                <span class="ppi-name" id="ppi-name"></span>
                <span class="ppi-page" id="ppi-page"></span>
              </span>
            </div>
            <div class="preview-pane" id="preview-pane"></div>
          </div>
          <div class="table-pane">
            <div class="ref-table-scroll">
              <table class="ref-table">
                <thead><tr><th>Original Value</th><th>Masked As</th><th>Page</th><th></th></tr></thead>
                <tbody id="ref-table-body"></tbody>
              </table>
            </div>
            <div class="add-row-form">
              <input type="text" id="add-value-input" class="field-input" placeholder="Missed value (e.g. phone number)">
              <div class="category-field">
                <select id="add-category-input" class="field-input">
                  <option value="">Category&hellip;</option>
                </select>
                <button type="button" class="add-category-btn" id="add-category-btn" title="Add a new category">+</button>
              </div>
              <button class="btn btn-primary" id="add-value-btn" onclick="addMissingValue()">Add &amp; Re-redact</button>
              <button class="btn btn-ghost" id="cancel-edit-btn" style="display:none" onclick="cancelEdit()">Cancel</button>
            </div>
            <div class="detail-error" id="detail-error"></div>
          </div>
        </div>
      </div>

      <label class="ack-check" id="ack-check-wrap" style="margin-top:24px;">
        <input type="checkbox" id="ack-checkbox">
        <span>I reviewed the redacted file in the preview and confirm that everything is ok</span>
      </label>

      <div class="result-card" id="result-list" style="margin-top:12px;"></div>

      <div class="next-step-row">
        <a class="btn btn-primary is-disabled" id="agate-next-btn" aria-disabled="true"
           href="{{ agate_href or '#' }}" target="_blank" rel="noopener"
           data-has-uid="{{ '1' if agate_href else '0' }}"
           title="{{ 'Send the redacted files to Agate Merger' if agate_href else 'Open this page from your signed-in link to continue' }}">
          Next &rarr;
        </a>
      </div>
      <div class="detail-error" id="agate-next-error" style="margin-top:8px;"></div>
    </div>

  </div>

</div>
</main>

<div id="tour-overlay"></div>
<div id="tour-card"></div>

<script>
const dropZone      = document.getElementById('drop-zone');
const formatSelect  = document.getElementById('format-select');
const fileInput     = document.getElementById('file-input');
const fileListEl    = document.getElementById('file-list');
const runBtn        = document.getElementById('run-btn');
const clearBtn      = document.getElementById('clear-btn');
const fileCountEl   = document.getElementById('file-count');
const logEl         = document.getElementById('log');
const progEl        = document.getElementById('prog');
const progressSec   = document.getElementById('progress-section');
const consoleSec    = document.getElementById('console-section');
const spinner       = document.getElementById('spinner');
const statusText    = document.getElementById('status-text');
const pctText       = document.getElementById('pct-text');
const cancelBatchBtn = document.getElementById('cancel-batch-btn');
let currentBatchId = null;
const resultsSec    = document.getElementById('results-section');
const resultList    = document.getElementById('result-list');
const ackCheckbox   = document.getElementById('ack-checkbox');
const rightPlaceholder = document.getElementById('right-placeholder');
const processNameInput = document.getElementById('process-name-input');
const clientSelect  = document.getElementById('client-select');
const jobSelect     = document.getElementById('job-select');
const detailSec     = document.getElementById('detail-section');
const detailNoteEl  = document.getElementById('detail-note');
const detailGridEl  = document.getElementById('detail-grid');
const fileTabsEl    = document.getElementById('file-tabs');
const previewPane   = document.getElementById('preview-pane');
const previewToolbar = document.getElementById('preview-toolbar');
const selectToolBtn = document.getElementById('select-tool-btn');
const previewHintEl = document.getElementById('preview-hint');
const pageIndicatorEl = document.getElementById('preview-page-indicator');
const ppiNameEl     = document.getElementById('ppi-name');
const ppiPageEl     = document.getElementById('ppi-page');
const refTableBody  = document.getElementById('ref-table-body');
const addValueInput = document.getElementById('add-value-input');
const addCategoryInput = document.getElementById('add-category-input');
const addCategoryBtn = document.getElementById('add-category-btn');
const addValueBtn   = document.getElementById('add-value-btn');
const cancelEditBtn = document.getElementById('cancel-edit-btn');
const detailErrorEl = document.getElementById('detail-error');

let selectedFiles = [];
let selectedJobId = null;
let detailFiles = [];
let currentProcessId = null;
let activeFileIdx = 0;
let selectMode = false;
let marqueeDrag = null;
let activeMarqueeBox = null;
const DEFAULT_PREVIEW_HINT = 'Drag a box around a missed value to send it to the form below.';
let editingRowId = null;

// ── Review timer (wiki/feature-updates/plan-timer-implementation.md) ──
// Starts on the first Select click, pauses after REVIEW_IDLE_MS without
// input (the idle stretch is removed and logged as idle time) or while the
// tab is hidden, and resumes on the next input. A Download click stops it and
// saves; only another Select click restarts it (from the running total).
// Time is summed from performance.now() segments, not interval ticks,
// because browsers throttle timers in background tabs.
const REVIEW_IDLE_MS = {{ review_idle_seconds|int }} * 1000;
const reviewTimerEl = document.getElementById('review-timer');
const rtSystemEl    = document.getElementById('rt-system');
const rtReviewEl    = document.getElementById('rt-review');
const rtIdleEl      = document.getElementById('rt-idle');
const rtElapsedEl   = document.getElementById('rt-elapsed');
const rtStateEl     = document.getElementById('rt-state');
const REVIEW_STATE_LABELS = {
  not_started: 'Click Select to start',
  running: 'Running',
  idle: 'Paused – idle',
  hidden: 'Paused – tab hidden',
  stopped: 'Stopped – click Select to continue',
};
let review = null;

function formatDuration(totalSeconds) {
  const s = Math.max(0, Math.floor(totalSeconds));
  const pad = n => String(n).padStart(2, '0');
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor(s / 60) % 60)}:${pad(s % 60)}`;
}

function resetReviewTimer(processId, saved = null, systemSeconds = null) {
  review = processId ? {
    processId,
    state: 'not_started',
    startedAt: 0, segmentStart: 0, pauseStart: 0, lastActivityAt: 0,
    activeMs:  (saved ? saved.active_seconds : 0) * 1000,
    idleMs:    (saved ? saved.idle_seconds : 0) * 1000,
    hiddenMs:  (saved ? saved.tab_hidden_seconds : 0) * 1000,
    idlePauses: saved ? saved.idle_pause_count : 0,
    tabPauses:  saved ? saved.tab_pause_count : 0,
  } : null;
  reviewTimerEl.hidden = !review;
  rtSystemEl.textContent = systemSeconds == null ? '--:--:--' : formatDuration(systemSeconds);
  renderReviewTimer();
}

async function initReviewTimer(processId) {
  if (review && review.processId === processId) return;
  resetReviewTimer(null);
  try {
    const res = await fetch(`/api/process/${encodeURIComponent(processId)}/review-time`);
    if (!res.ok) return;
    const data = await res.json();
    if (currentProcessId !== processId) return;
    resetReviewTimer(processId, data.review, data.system_seconds);
  } catch (e) { console.warn('Review timer unavailable', e); }
}

function renderReviewTimer() {
  if (!review) return;
  const now = performance.now();
  const activeMs = review.activeMs + (review.state === 'running' ? now - review.segmentStart : 0);
  const idleMs = review.idleMs + (review.state === 'idle' ? now - review.pauseStart : 0);
  const hiddenMs = review.hiddenMs + (review.state === 'hidden' ? now - review.pauseStart : 0);
  rtReviewEl.textContent = formatDuration(activeMs / 1000);
  rtIdleEl.textContent = formatDuration(idleMs / 1000);
  // Elapsed = everything since the first Select click (stopped time after a Download excluded).
  rtElapsedEl.textContent = formatDuration((activeMs + idleMs + hiddenMs) / 1000);
  rtStateEl.textContent = REVIEW_STATE_LABELS[review.state];
  reviewTimerEl.classList.toggle('running', review.state === 'running');
}

function startReviewTimer() {
  if (!review || (review.state !== 'not_started' && review.state !== 'stopped')) return;
  const now = performance.now();
  // startedAt is only set once: wall_seconds back-dates started_at on the first save.
  if (review.state === 'not_started') review.startedAt = now;
  review.segmentStart = review.lastActivityAt = now;
  review.state = 'running';
  renderReviewTimer();
}

function pauseReviewTimer(reason, at) {
  if (!review || review.state !== 'running') return;
  review.activeMs += Math.max(0, at - review.segmentStart);
  review.state = reason;
  review.pauseStart = at;
  if (reason === 'idle') review.idlePauses++;
  if (reason === 'hidden') review.tabPauses++;
  renderReviewTimer();
}

function closeReviewPause(now) {
  if (review.state === 'idle') review.idleMs += now - review.pauseStart;
  if (review.state === 'hidden') review.hiddenMs += now - review.pauseStart;
}

function onReviewActivity() {
  // Stopped by a Download click: only Select restarts it (startReviewTimer).
  if (!review || review.state === 'not_started' || review.state === 'stopped' || document.hidden) return;
  const now = performance.now();
  review.lastActivityAt = now;
  if (review.state === 'running') return;
  closeReviewPause(now);
  review.state = 'running';
  review.segmentStart = now;
  renderReviewTimer();
}

['mousemove', 'mousedown', 'keydown', 'wheel', 'touchstart', 'scroll'].forEach(type =>
  document.addEventListener(type, onReviewActivity, { capture: true, passive: true }));

document.addEventListener('visibilitychange', () => {
  if (!review || !document.hidden) return;
  const now = performance.now();
  if (review.state === 'running') {
    pauseReviewTimer('hidden', now);
  } else if (review.state === 'idle') {
    // Already idle: stop counting idle time and count the rest as tab-hidden.
    closeReviewPause(now);
    review.state = 'hidden';
    review.pauseStart = now;
    review.tabPauses++;
    renderReviewTimer();
  }
});

setInterval(() => {
  if (review && review.state === 'running' && performance.now() - review.lastActivityAt >= REVIEW_IDLE_MS) {
    // Close the segment at the last input, so the idle stretch isn't counted.
    pauseReviewTimer('idle', review.lastActivityAt);
  }
  renderReviewTimer();
}, 1000);

function saveReviewTimer() {
  if (!review || review.state === 'not_started') return;
  const now = performance.now();
  // Stop the timer. A repeat Download while already stopped re-saves the same
  // totals, which only bumps download_count.
  pauseReviewTimer('stopped', now);
  if (review.state !== 'stopped') {
    closeReviewPause(now);
    review.state = 'stopped';
    renderReviewTimer();
  }
  fetch(`/api/process/${encodeURIComponent(review.processId)}/review-time`, {
    method: 'POST',
    keepalive: true,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      active_seconds: Math.round(review.activeMs / 1000),
      idle_seconds: Math.round(review.idleMs / 1000),
      tab_hidden_seconds: Math.round(review.hiddenMs / 1000),
      idle_pauses: review.idlePauses,
      tab_pauses: review.tabPauses,
      wall_seconds: Math.round((now - review.startedAt) / 1000),
    }),
  }).then(res => {
    if (!res.ok) console.warn('Failed to save review time: HTTP ' + res.status);
  }).catch(e => console.warn('Failed to save review time', e));
}

async function loadClients() {
  try {
    const res = await fetch('/api/clients');
    const clients = await res.json();
    clients.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.pid;
      opt.textContent = c.name;
      clientSelect.appendChild(opt);
    });
  } catch (e) { console.error('Failed to load clients', e); }
}
loadClients();

async function resolveSignedLinkContext() {
  const params = new URLSearchParams(window.location.search);
  const token = params.get('token');
  if (!token) return;

  try {
    const uid = params.get('uid') || '';
    const url = `/api/link-context?token=${encodeURIComponent(token)}${uid ? '&uid=' + encodeURIComponent(uid) : ''}`;
    const res = await fetch(url);
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.process_id) {
      console.warn('Could not resolve signed-link context', data);
      return;
    }

    

    appGrid.classList.add('sidebar-collapsed');
    sidebarToggle.style.display = 'none';   
      resultsSec.style.display = 'block';         
    rightPlaceholder.style.display = 'none'; 
    await loadResultDetail(data.process_id, data.file_id || null);
  } catch (e) {
    console.error('Failed to resolve signed-link context', e);
  }
}

resolveSignedLinkContext();

clientSelect.addEventListener('change', () => {
  selectedJobId = null;
  jobSelect.innerHTML = '';
  if (!clientSelect.value) {
    jobSelect.disabled = true;
    jobSelect.appendChild(new Option('Select a client first', ''));
    updateRunButtonState();
    return;
  }
  jobSelect.disabled = true;
  jobSelect.appendChild(new Option('Loading jobs…', ''));
  loadJobsForClient(clientSelect.value);
  updateRunButtonState();
});

async function loadJobsForClient(clientId) {
  try {
    const res = await fetch(`/api/jobs?client_id=${encodeURIComponent(clientId)}`);
    const jobs = await res.json();
    jobSelect.innerHTML = '';
    jobSelect.appendChild(new Option('Select a job…', ''));
    jobs.forEach(j => {
      jobSelect.appendChild(new Option(j.date ? `${j.description} (${j.date})` : j.description, j.aid));
    });
    jobSelect.disabled = false;
  } catch (e) {
    console.error('Failed to load jobs', e);
    jobSelect.innerHTML = '';
    jobSelect.appendChild(new Option('Failed to load jobs', ''));
  }
}

jobSelect.addEventListener('change', () => {
  selectedJobId = jobSelect.value ? parseInt(jobSelect.value, 10) : null;
  updateRunButtonState();
});

processNameInput.addEventListener('input', updateRunButtonState);

function updateRunButtonState() {
  const n = selectedFiles.length;
  runBtn.disabled = !(n > 0 && processNameInput.value.trim() && clientSelect.value && selectedJobId);
  clearBtn.disabled = n === 0;
  fileCountEl.textContent = n ? `${n} file${n>1?'s':''} selected` : '';
}

dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('dragover'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
dropZone.addEventListener('drop', e => { e.preventDefault(); dropZone.classList.remove('dragover'); addFiles(e.dataTransfer.files); });
fileInput.addEventListener('change', () => { addFiles(fileInput.files); fileInput.value=''; });

const sidebarToggle = document.getElementById('sidebar-toggle');
const appGrid = document.querySelector('.app-grid');
sidebarToggle.addEventListener('click', () => {
  const collapsed = appGrid.classList.toggle('sidebar-collapsed');
  sidebarToggle.title = collapsed ? 'Show panel' : 'Hide panel';
  sidebarToggle.setAttribute('aria-label', sidebarToggle.title);
  sidebarToggle.setAttribute('aria-expanded', String(!collapsed));
});

function extOf(name) { return name.slice(name.lastIndexOf('.')).toLowerCase(); }

function addFiles(list) {
  for (const f of list) {
    const ext = extOf(f.name);
    if (!['.pdf','.xlsx','.xls','.docx','.doc','.pptx','.png','.jpg','.jpeg','.eml','.msg'].includes(ext)) { alert('Unsupported file: ' + f.name); continue; }
    if (!selectedFiles.find(x => x.name === f.name)) selectedFiles.push(f);
  }
  renderFiles();
}

function renderFiles() {
  fileListEl.innerHTML = '';
  selectedFiles.forEach((f, i) => {
    const ext  = extOf(f.name).replace('.','');
    const isImg = ext === 'png' || ext === 'jpg' || ext === 'jpeg';
    const isEmail = ext === 'eml' || ext === 'msg';
    const icon = ext === 'pdf' ? 'PDF' : (ext === 'docx' || ext === 'doc') ? 'DOC' : ext === 'pptx' ? 'PPT' : isEmail ? 'EML' : isImg ? 'IMG' : 'XLS';
    const cls  = ext === 'pdf' ? 'pdf' : (ext === 'docx' || ext === 'doc') ? 'docx' : isEmail ? 'docx' : isImg ? 'img' : 'xlsx';
    const mb   = (f.size/1024/1024).toFixed(1);
    const d = document.createElement('div');
    d.className = 'file-item';
    d.innerHTML = `
      <div class="file-icon ${cls}">${icon}</div>
      <span class="file-name">${f.name}</span>
      <span class="file-size">${mb} MB</span>
      <button class="remove-btn" onclick="removeFile(${i})" title="Remove">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="width:13px;height:13px"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>`;
    fileListEl.appendChild(d);
  });
  updateRunButtonState();
}

function removeFile(i) { selectedFiles.splice(i,1); renderFiles(); }
function clearFiles()  { selectedFiles=[]; renderFiles(); }

function setProgress(pct) {
  progEl.style.width = pct + '%';
  pctText.textContent = Math.round(pct) + '%';
}

async function runPipeline() {
  if (!selectedFiles.length || !processNameInput.value.trim() || !clientSelect.value || !selectedJobId) return;
  runBtn.disabled = true; clearBtn.disabled = true;
  logEl.textContent = '';
  progressSec.style.display = 'block';
  consoleSec.style.display  = 'block';
  resultsSec.style.display  = 'none';
  rightPlaceholder.style.display = 'none';
  spinner.className = 'spinner';
  progEl.className  = 'progress-fill';
  statusText.textContent = 'Uploading files…';
  setProgress(5);
  resetReviewTimer(null);
  currentBatchId = null;
  cancelBatchBtn.style.display = 'none';
  cancelBatchBtn.disabled = false;
  cancelBatchBtn.textContent = 'Cancel';

  const form = new FormData();
  selectedFiles.forEach(f => form.append('files', f));
  form.append('process_name', processNameInput.value.trim());
  form.append('client_id', clientSelect.value);
  form.append('job_id', selectedJobId);
  form.append('output_format', formatSelect.value);

  let res;
  try { res = await fetch('/upload', { method:'POST', body:form }); }
  catch(e) { appendLog('[ERROR] ' + e); resetButtons(); return; }
  if (!res.ok) { appendLog('[ERROR] ' + await res.text()); resetButtons(); return; }

  const { job_id } = await res.json();
  statusText.textContent = 'Running pipeline…';
  setProgress(15);

  // Cancelling only skips files that haven't started yet (see
  // /api/batch/<id>/cancel) — with a single file there's nothing left to
  // skip once it's running, so the button isn't shown for a 1-file batch.
  if (selectedFiles.length > 1) {
    currentBatchId = job_id;
    cancelBatchBtn.style.display = 'inline-flex';
  }

  const src = new EventSource('/stream/' + job_id);
  let fakeP = 15;

  src.addEventListener('log', e => {
    appendLog(e.data);
    if (fakeP < 90) { fakeP += 0.4; setProgress(fakeP); }
  });

  src.addEventListener('done', e => {
    src.close();
    setProgress(100);
    progEl.className = 'progress-fill done';
    spinner.className = 'spinner done';
    const data = JSON.parse(e.data);
    statusText.textContent = data.success ? 'Redaction complete!' : 'Finished with errors.';
    showResults(data.output_files, data.files, job_id);
    loadResultDetail(job_id);
    resetButtons();
  });

  src.onerror = () => { src.close(); statusText.textContent='Connection lost.'; spinner.className='spinner done'; resetButtons(); };
}

async function cancelBatch() {
  if (!currentBatchId || cancelBatchBtn.disabled) return;
  cancelBatchBtn.disabled = true;
  cancelBatchBtn.textContent = 'Cancelling…';
  try {
    const res = await fetch(`/api/batch/${encodeURIComponent(currentBatchId)}/cancel`, { method: 'POST' });
    if (!res.ok) { appendLog('[ERROR] Cancel request failed: ' + await res.text()); cancelBatchBtn.disabled = false; cancelBatchBtn.textContent = 'Cancel'; return; }
    appendLog('[INFO] Cancel requested — the file in progress will finish, remaining files will be skipped.');
    statusText.textContent = 'Cancelling — finishing current file…';
    cancelBatchBtn.textContent = 'Cancelling…';
  } catch (e) {
    appendLog('[ERROR] Cancel request failed: ' + e);
    cancelBatchBtn.disabled = false;
    cancelBatchBtn.textContent = 'Cancel';
  }
}

function resetButtons() {
  updateRunButtonState();
  currentBatchId = null;
  cancelBatchBtn.style.display = 'none';
}

function appendLog(text) { logEl.textContent += text + '\\n'; logEl.scrollTop = logEl.scrollHeight; }

const agateNextBtn = document.getElementById('agate-next-btn');

function updateDownloadState() {
  const enabled = ackCheckbox.checked;
  resultList.querySelectorAll('.dl-btn').forEach(btn => {
    btn.classList.toggle('disabled', !enabled);
    btn.setAttribute('aria-disabled', String(!enabled));
  });
  // Next -> Agate Merger: needs the review tick, a signed-in uid and output files.
  const nextEnabled = enabled && agateNextBtn.dataset.hasUid === '1'
    && !!resultList.querySelector('.dl-btn');
  agateNextBtn.classList.toggle('is-disabled', !nextEnabled);
  agateNextBtn.setAttribute('aria-disabled', String(!nextEnabled));
}
ackCheckbox.addEventListener('change', updateDownloadState);
const agateNextError = document.getElementById('agate-next-error');
agateNextBtn.addEventListener('click', async e => {
  e.preventDefault();
  if (agateNextBtn.getAttribute('aria-disabled') === 'true' || agateNextBtn.dataset.busy) return;
  saveReviewTimer();
  agateNextError.style.display = 'none';
  // Open the tab NOW, inside the click — a window.open() after an await is
  // popup-blocked. Its location is set once the files reach Agate.
  const tab = window.open('', '_blank');
  const label = agateNextBtn.innerHTML;
  agateNextBtn.dataset.busy = '1';
  agateNextBtn.classList.add('is-disabled');
  agateNextBtn.textContent = 'Sending to Agate…';
  try {
    const res = await fetch('/api/agate-handoff', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ process_id: currentProcessId }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.redirect_url) throw new Error(data.error || `Hand-off failed (${res.status})`);
    if (tab) tab.location.href = data.redirect_url; else window.location.href = data.redirect_url;
  } catch (err) {
    if (tab) tab.close();
    agateNextError.textContent = err.message;
    agateNextError.style.display = 'block';
  } finally {
    delete agateNextBtn.dataset.busy;
    agateNextBtn.innerHTML = label;
    updateDownloadState();
  }
});
resultList.addEventListener('click', e => {
  const btn = e.target.closest('.dl-btn');
  if (!btn) return;
  if (!ackCheckbox.checked) { e.preventDefault(); return; }
  saveReviewTimer();
});

function downloadHref(fileId, filename) {
  // Prefer file_id (reads pii_files.redacted_path from DB — always current after
  // manual Add & Re-redact); fall back to filename route if no file_id.
  return fileId
    ? `/download/id/${encodeURIComponent(fileId)}`
    : `/download/${encodeURIComponent(filename)}`;
}

function renderDownloadList(files, { resetAck = true, processId = null } = {}) {
  resultsSec.style.display = 'block';
  resultList.innerHTML = '';
  if (resetAck) ackCheckbox.checked = false;
  if (!files || !files.length) {
    resultList.innerHTML = '<div class="empty-state">No output files found &mdash; check the log above.</div>';
    updateDownloadState();
    return;
  }
  if (processId && files.length > 1) {
    const zipHref = `/download/process/${encodeURIComponent(processId)}/compliance_package`;
    const zd = document.createElement('div');
    zd.className = 'result-item';
    zd.innerHTML = `
      <div class="result-file-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><path d="M10 12h4"/></svg>
      </div>
      <span class="result-name">All ${files.length} files (+ audit logs)</span>
      <span class="badge badge-ok">ZIP</span>
      <a class="dl-btn disabled" aria-disabled="true" href="${zipHref}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        Download All (ZIP)
      </a>`;
    resultList.appendChild(zd);
  }
  files.forEach(f => {
    const name = f.redacted_filename || f.filename;
    const fileId = f.file_id || '';
    const href = downloadHref(fileId, name);
    const d = document.createElement('div');
    d.className = 'result-item';
    if (fileId) d.dataset.fileId = fileId;
    d.innerHTML = `
      <div class="result-file-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="9" y1="15" x2="15" y2="15"/></svg>
      </div>
      <span class="result-name">${escapeHtml(name)}</span>
      <span class="badge badge-ok">Redacted</span>
      <a class="dl-btn disabled" aria-disabled="true" href="${href}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        Download
      </a>`;
    resultList.appendChild(d);
  });
  updateDownloadState();
}

function showResults(files, filesWithId, processId = null) {
  const fileIdByName = {};
  (filesWithId || []).forEach(f => { fileIdByName[f.filename] = f.file_id; });
  renderDownloadList(
    (files || []).map(f => ({
      filename: f,
      redacted_filename: f,
      file_id: fileIdByName[f] || '',
    })),
    { processId },
  );
}

function refreshDownloadList() {
  if (!detailFiles.length) return;
  renderDownloadList(detailFiles, { resetAck: false, processId: currentProcessId });
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

function showDetailNote(message) {
  detailSec.style.display = 'block';
  detailNoteEl.style.display = 'block';
  detailNoteEl.textContent = message;
  fileTabsEl.style.display = 'none';
  detailGridEl.style.display = 'none';
}

async function loadResultDetail(processId, preferredFileId = null) {
  detailFiles = [];
  activeFileIdx = 0;
  currentProcessId = processId;
  initReviewTimer(processId);
  cancelEdit();
  try {
    const res = await fetch(`/api/redaction-map/${encodeURIComponent(processId)}`);
    const data = await res.json().catch(() => ({}));
    if (res.status === 403) {
      showDetailNote('Reference table & preview are only shown when you open this app via your normal signed-in link (the one with ?uid=... in the URL) — open this page through that link to see them.');
      return;
    }
    if (!res.ok) {
      showDetailNote(data.error || `Couldn't load the reference table (HTTP ${res.status}).`);
      return;
    }
    detailFiles = data.files || [];
    if (!detailFiles.length) {
      showDetailNote('No redaction data found for this run.');
      return;
    }
    if (preferredFileId) {
      const preferredIdx = detailFiles.findIndex(f => f.file_id === preferredFileId || f.filename === preferredFileId || f.redacted_filename === preferredFileId);
      if (preferredIdx >= 0) activeFileIdx = preferredIdx;
    }
    detailNoteEl.style.display = 'none';
    detailGridEl.style.display = 'grid';
    detailSec.style.display = 'block';
    renderDownloadList(detailFiles, { processId });
    renderFileTabs();
    loadCategories();
    await loadActiveFile();
    setTimeout(maybeAutoStartTourPart2, 200);
  } catch (e) {
    console.error('Failed to load reference table/preview', e);
    showDetailNote(`Couldn't load the reference table: ${e}`);
  }
}

async function loadCategories(selectName) {
  try {
    const res = await fetch('/api/categories');
    const categories = await res.json();
    if (!res.ok) return;
    const current = addCategoryInput.value;
    addCategoryInput.innerHTML = '';
    addCategoryInput.appendChild(new Option('Category…', ''));
    categories.forEach(c => addCategoryInput.appendChild(new Option(c, c)));
    setCategoryValue(selectName || current);
  } catch (e) {
    console.error('Failed to load categories', e);
  }
}

function setCategoryValue(cat) {
  if (!cat) { addCategoryInput.value = ''; return; }
  const hasOption = Array.from(addCategoryInput.options).some(o => o.value === cat);
  if (!hasOption) addCategoryInput.appendChild(new Option(cat, cat));
  addCategoryInput.value = cat;
}

addCategoryBtn.addEventListener('click', async () => {
  const name = (prompt('New category name (e.g. PASSPORT_NUMBER):') || '').trim();
  if (!name) return;
  try {
    const res = await fetch('/api/categories', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ name }),
    });
    const data = await res.json();
    if (!res.ok) { alert(data.error || 'Failed to add category'); return; }
    await loadCategories(data.name);
  } catch (e) {
    alert(String(e));
  }
});

const FILE_TABS_MAX_VISIBLE = 5;

function renderFileTabs() {
  fileTabsEl.innerHTML = '';
  fileTabsEl.style.display = detailFiles.length > 1 ? 'flex' : 'none';
  if (!detailFiles.length) return;

  let visibleIdxs;
  if (detailFiles.length <= FILE_TABS_MAX_VISIBLE) {
    visibleIdxs = detailFiles.map((_, i) => i);
  } else {
    visibleIdxs = Array.from({ length: FILE_TABS_MAX_VISIBLE }, (_, i) => i);
    if (activeFileIdx >= FILE_TABS_MAX_VISIBLE) visibleIdxs[FILE_TABS_MAX_VISIBLE - 1] = activeFileIdx;
    visibleIdxs.sort((a, b) => a - b);
  }
  const visibleSet = new Set(visibleIdxs);
  const overflowIdxs = detailFiles.map((_, i) => i).filter(i => !visibleSet.has(i));

  const selectFile = (i) => { activeFileIdx = i; cancelEdit(); renderFileTabs(); loadActiveFile(); };

  visibleIdxs.forEach(i => {
    const f = detailFiles[i];
    const tab = document.createElement('button');
    tab.type = 'button';
    tab.className = 'file-tab' + (i === activeFileIdx ? ' active' : '');
    tab.textContent = f.filename;
    tab.title = f.filename;
    tab.onclick = () => selectFile(i);
    fileTabsEl.appendChild(tab);
  });

  if (overflowIdxs.length) {
    const wrap = document.createElement('div');
    wrap.className = 'file-tab-more-wrap';

    const moreBtn = document.createElement('button');
    moreBtn.type = 'button';
    moreBtn.className = 'file-tab file-tab-more';
    moreBtn.textContent = `+${overflowIdxs.length} more`;

    const dropdown = document.createElement('div');
    dropdown.className = 'file-tab-dropdown';
    dropdown.style.display = 'none';

    const search = document.createElement('input');
    search.type = 'text';
    search.className = 'file-tab-search';
    search.placeholder = 'Search files…';

    const list = document.createElement('div');
    list.className = 'file-tab-dropdown-list';

    const renderList = (query) => {
      list.innerHTML = '';
      const q = (query || '').trim().toLowerCase();
      const matches = overflowIdxs.filter(i => detailFiles[i].filename.toLowerCase().includes(q));
      if (!matches.length) {
        const empty = document.createElement('div');
        empty.className = 'file-tab-dropdown-empty';
        empty.textContent = 'No files match';
        list.appendChild(empty);
        return;
      }
      matches.forEach(i => {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'file-tab-dropdown-item' + (i === activeFileIdx ? ' active' : '');
        item.textContent = detailFiles[i].filename;
        item.title = detailFiles[i].filename;
        item.onclick = () => selectFile(i);
        list.appendChild(item);
      });
    };
    renderList('');
    search.oninput = () => renderList(search.value);
    search.onkeydown = (e) => { if (e.key === 'Escape') closeFileTabDropdown(); };

    dropdown.appendChild(search);
    dropdown.appendChild(list);

    moreBtn.onclick = (e) => {
      e.stopPropagation();
      const isOpen = dropdown.style.display !== 'none';
      closeFileTabDropdown();
      if (!isOpen) {
        dropdown.style.display = 'flex';
        search.value = '';
        renderList('');
        search.focus();
      }
    };

    wrap.appendChild(moreBtn);
    wrap.appendChild(dropdown);
    fileTabsEl.appendChild(wrap);
  }
}

function closeFileTabDropdown() {
  const dropdown = fileTabsEl.querySelector('.file-tab-dropdown');
  if (dropdown) dropdown.style.display = 'none';
}

document.addEventListener('click', (e) => {
  if (!fileTabsEl.contains(e.target)) closeFileTabDropdown();
});

async function loadActiveFile() {
  detailErrorEl.style.display = 'none';
  renderTable();
  previewPane.innerHTML = '<div class="empty-state">Loading preview&hellip;</div>';
  updatePageIndicator();
  const f = detailFiles[activeFileIdx];
  try {
    const res = await fetch(`/api/preview/${encodeURIComponent(f.file_id)}`);
    const preview = await res.json();
    if (!res.ok) {
      previewPane.innerHTML = `<div class="empty-state">${escapeHtml(preview.error || 'Preview unavailable')}</div>`;
      return;
    }
    f.token_pages = preview.token_pages || {};
    f.previewWords = preview.words || null;
    renderPreview(preview);
    renderTable();
  } catch (e) {
    previewPane.innerHTML = '<div class="empty-state">Preview unavailable</div>';
  }
}

const TABLE_PREVIEW_HINT = 'Double-click a cell to send its value to the form below.';

// mode: 'image' (marquee Select tool), 'table' (double-click a cell), or 'none'.
function setPreviewTool(mode) {
  if (mode === 'none') {
    previewToolbar.style.display = 'none';
  } else {
    previewToolbar.style.display = 'flex';
  }
  // The marquee Select tool only works on rendered page images, so it's shown
  // for 'image' previews and hidden for 'table' previews (Excel/email), which
  // use double-click-to-send instead.
  selectToolBtn.style.display = (mode === 'image') ? '' : 'none';
  previewHintEl.textContent = (mode === 'table') ? TABLE_PREVIEW_HINT : DEFAULT_PREVIEW_HINT;
  if (mode !== 'image') {
    selectMode = false;
    selectToolBtn.classList.remove('active');
    previewPane.classList.remove('select-mode');
  }
}

// Back-compat shim for existing call sites.
function setSelectToolAvailable(available) {
  setPreviewTool(available ? 'image' : 'none');
}

function renderPreview(preview) {
  previewPane.innerHTML = '';
  clearMarquee();
  if (!preview) { previewPane.innerHTML = '<div class="empty-state">Preview unavailable</div>'; updatePageIndicator(); return; }
  if (preview.type === 'table') {
    setPreviewTool('table');
    // Excel/email previews have no Select button; the review timer starts when
    // the user double-clicks a cell to send a value (see fillMissedValue()).
    const sheets = preview.sheets || [];
    // Render one sheet's rows into a fresh table element.
    const buildTable = (sheet) => {
      const table = document.createElement('table');
      table.className = 'preview-table';
      (sheet.rows || []).forEach(row => {
        const tr = document.createElement('tr');
        row.forEach(cell => {
          const td = document.createElement('td');
          td.textContent = cell;
          td.title = 'Double-click to send this value to the form below';
          td.addEventListener('dblclick', () => fillMissedValue(cell));
          tr.appendChild(td);
        });
        table.appendChild(tr);
      });
      return table;
    };
    if (sheets.length > 1) {
      // Excel-style tabs: a tab strip plus one visible sheet at a time.
      const tabBar = document.createElement('div');
      tabBar.className = 'preview-tabs';
      const tableHost = document.createElement('div');
      const showSheet = (idx) => {
        tableHost.innerHTML = '';
        tableHost.appendChild(buildTable(sheets[idx]));
        [...tabBar.children].forEach((b, i) => b.classList.toggle('active', i === idx));
      };
      sheets.forEach((sheet, idx) => {
        const tab = document.createElement('button');
        tab.type = 'button';
        tab.className = 'preview-tab';
        tab.textContent = sheet.name;
        tab.title = sheet.name;
        tab.addEventListener('click', () => showSheet(idx));
        tabBar.appendChild(tab);
      });
      previewPane.appendChild(tabBar);
      previewPane.appendChild(tableHost);
      showSheet(0);
    } else {
      previewPane.appendChild(buildTable(sheets[0] || {rows: []}));
    }
  } else if (preview.type === 'pages') {
    setSelectToolAvailable(true);
    preview.images.forEach((src, i) => {
      const wrap = document.createElement('div');
      wrap.className = 'preview-page-wrap';
      wrap.dataset.page = i + 1;
      const img = document.createElement('img');
      img.className = 'preview-page';
      img.dataset.page = i + 1;
      img.draggable = false;
      img.src = src;
      wrap.appendChild(img);
      previewPane.appendChild(wrap);
    });
  } else if (preview.type === 'image') {
    setSelectToolAvailable(true);
    const wrap = document.createElement('div');
    wrap.className = 'preview-page-wrap';
    const img = document.createElement('img');
    img.className = 'preview-page';
    img.draggable = false;
    img.src = preview.image;
    wrap.appendChild(img);
    previewPane.appendChild(wrap);
  }
  updatePageIndicator();
}

// File name + "Page X of N" in the preview toolbar. The current page is the
// last one whose top has scrolled past the upper third of the pane, so the
// number flips once a page fills most of the view.
function updatePageIndicator() {
  const f = detailFiles[activeFileIdx];
  const wraps = previewPane.querySelectorAll('.preview-page-wrap');
  pageIndicatorEl.hidden = !f || !wraps.length;
  if (pageIndicatorEl.hidden) return;
  ppiNameEl.textContent = f.filename;
  ppiNameEl.title = f.filename;
  const line = previewPane.scrollTop + previewPane.clientHeight / 3;
  let current = 1;
  // Skip pages whose image hasn't loaded yet (zero height), or they'd all count as scrolled past.
  wraps.forEach((wrap, i) => { if (wrap.offsetHeight > 0 && wrap.offsetTop <= line) current = i + 1; });
  ppiPageEl.textContent = `Page ${current} of ${wraps.length}`;
}

let pageIndicatorFrame = null;
function schedulePageIndicator() {
  if (pageIndicatorFrame) return;
  pageIndicatorFrame = requestAnimationFrame(() => { pageIndicatorFrame = null; updatePageIndicator(); });
}
previewPane.addEventListener('scroll', schedulePageIndicator, { passive: true });
// Page images load after they're added, changing every page's offsetTop.
// 'load' doesn't bubble, so listen in the capture phase.
previewPane.addEventListener('load', schedulePageIndicator, true);

function jumpToPage(pageNum) {
  const img = previewPane.querySelector(`img.preview-page[data-page="${pageNum}"]`);
  if (img) img.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function fillMissedValue(text) {
  text = (text || '').trim();
  if (!text) return;
  startReviewTimer();  // sending a value from an Excel/email cell counts as review start
  addValueInput.value = text;
  addCategoryInput.focus();
}

selectToolBtn.addEventListener('click', () => {
  startReviewTimer();
  selectMode = !selectMode;
  selectToolBtn.classList.toggle('active', selectMode);
  previewPane.classList.toggle('select-mode', selectMode);
});

function clearMarquee() {
  if (activeMarqueeBox) { activeMarqueeBox.remove(); activeMarqueeBox = null; }
}

function wordsForPage(page) {
  const f = detailFiles[activeFileIdx];
  const words = f && f.previewWords;
  if (!words) return [];
  return page ? (words[page - 1] || []) : words;
}

previewPane.addEventListener('mousedown', e => {
  if (!selectMode) return;
  const wrap = e.target.closest('.preview-page-wrap');
  if (!wrap) return;
  e.preventDefault();
  clearMarquee();
  const rect = wrap.getBoundingClientRect();
  const box = document.createElement('div');
  box.className = 'marquee-box';
  wrap.appendChild(box);
  activeMarqueeBox = box;
  marqueeDrag = { wrap, startX: e.clientX - rect.left, startY: e.clientY - rect.top, box };
  updateMarqueeBox(e);
});

document.addEventListener('mousemove', e => {
  if (marqueeDrag) updateMarqueeBox(e);
});

document.addEventListener('mouseup', () => {
  if (marqueeDrag) finishMarquee();
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (marqueeDrag) { marqueeDrag.box.remove(); marqueeDrag = null; }
    clearMarquee();
  }
});

function updateMarqueeBox(e) {
  const { wrap, startX, startY, box } = marqueeDrag;
  const rect = wrap.getBoundingClientRect();
  const curX = Math.min(Math.max(e.clientX - rect.left, 0), rect.width);
  const curY = Math.min(Math.max(e.clientY - rect.top, 0), rect.height);
  const x = Math.min(startX, curX), y = Math.min(startY, curY);
  const w = Math.abs(curX - startX), h = Math.abs(curY - startY);
  box.style.left = x + 'px'; box.style.top = y + 'px';
  box.style.width = w + 'px'; box.style.height = h + 'px';
}

async function finishMarquee() {
  const { wrap, box } = marqueeDrag;
  marqueeDrag = null;
  const boxW = parseFloat(box.style.width) || 0;
  const boxH = parseFloat(box.style.height) || 0;
  if (boxW < 6 || boxH < 6) { box.remove(); if (activeMarqueeBox === box) activeMarqueeBox = null; return; }

  const img = wrap.querySelector('img.preview-page');
  const wrapRect = wrap.getBoundingClientRect();
  const scaleX = img.naturalWidth / wrapRect.width;
  const scaleY = img.naturalHeight / wrapRect.height;
  const boxLeft = parseFloat(box.style.left) || 0;
  const boxTop = parseFloat(box.style.top) || 0;
  const x0 = boxLeft * scaleX, y0 = boxTop * scaleY;
  const x1 = (boxLeft + boxW) * scaleX, y1 = (boxTop + boxH) * scaleY;
  const page = wrap.dataset.page ? parseInt(wrap.dataset.page, 10) : null;

  await resolveMarqueeSelection({ page, x0, y0, x1, y1 });
}

function countSelectionLines(words) {
  // Redaction matching only ever searches within one visual line at a time
  // (see _find_spans_in_line in redaction_formats.py), so a marquee box that
  // catches words from two different lines can never be found as a single
  // contiguous string server-side. Detect that here, before even trying, by
  // clustering word y-centers with a tolerance scaled to the words' own
  // height (works for both normal-size and OCR'd text).
  if (words.length < 2) return words.length;
  const heights = words.map(w => w.y1 - w.y0).filter(h => h > 0);
  const avgHeight = heights.length ? heights.reduce((a, b) => a + b, 0) / heights.length : 10;
  const tol = Math.max(4, avgHeight * 0.6);
  const centers = words.map(w => (w.y0 + w.y1) / 2).sort((a, b) => a - b);
  let lines = 1;
  for (let i = 1; i < centers.length; i++) {
    if (centers[i] - centers[i - 1] > tol) lines++;
  }
  return lines;
}

async function resolveMarqueeSelection({ page, x0, y0, x1, y1 }) {
  const words = wordsForPage(page).filter(w => {
    const cx = (w.x0 + w.x1) / 2, cy = (w.y0 + w.y1) / 2;
    return cx >= x0 && cx <= x1 && cy >= y0 && cy <= y1;
  });
  words.sort((a, b) => {
    const ay = Math.round(a.y0 / 8), by = Math.round(b.y0 / 8);
    return ay - by || a.x0 - b.x0;
  });

  if (words.length && countSelectionLines(words) > 1) {
    detailErrorEl.textContent = 'That box spans more than one line — draw a box around a single line at a time for each redaction.';
    detailErrorEl.style.display = 'block';
    return;
  }

  let text = words.map(w => w.text).join(' ').trim();

  if (!text) {
    const f = detailFiles[activeFileIdx];
    previewHintEl.textContent = 'No pre-read text under that box — running OCR…';
    try {
      const res = await fetch(`/api/preview/${encodeURIComponent(f.file_id)}/ocr-region`, {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ page, x0, y0, x1, y1 }),
      });
      const data = await res.json();
      if (res.ok) text = (data.text || '').trim();
    } catch (e) { /* leave text empty, error path below handles it */ }
    previewHintEl.textContent = DEFAULT_PREVIEW_HINT;
  }

  if (text) {
    detailErrorEl.style.display = 'none';
    fillMissedValue(text);
  } else {
    detailErrorEl.textContent = "Couldn't read any text in that box — try drawing tighter around the value.";
    detailErrorEl.style.display = 'block';
  }
}

function renderTable() {
  const f = detailFiles[activeFileIdx];
  const tokenPages = f.token_pages || {};
  refTableBody.innerHTML = '';
  (f.rows || []).forEach(row => {
    const tr = document.createElement('tr');
    const isManual = row.source === 'manual';
    const pages = tokenPages[row.redacted_text];
    const pageCell = pages && pages.length
      ? pages.map(p => `<a href="#" onclick="jumpToPage(${p});return false;">${p}</a>`).join(', ')
      : '';
    tr.innerHTML = `
      <td>${escapeHtml(row.source_text)}</td>
      <td><code>${escapeHtml(row.redacted_text)}</code></td>
      <td>${pageCell}</td>
      <td>${isManual
        ? `<button class="row-action-btn" onclick="startEditRow(${row.id})" title="Edit">&#9998;</button>
           <button class="row-action-btn" onclick="deleteRow(${row.id})" title="Delete">&times;</button>`
        : `<span class="badge" style="opacity:.5">auto</span>
           <button class="row-action-btn" onclick="deleteRow(${row.id})" title="Delete">&times;</button>`}</td>`;
    refTableBody.appendChild(tr);
  });
}

function startEditRow(rowId) {
  const f = detailFiles[activeFileIdx];
  const row = (f.rows || []).find(r => r.id === rowId);
  if (!row) return;
  editingRowId = rowId;
  addValueInput.value = row.source_text;
  const m = row.redacted_text.match(/<\*([A-Z0-9_]+)_\d+\*>/);
  setCategoryValue(m ? m[1] : '');
  addValueBtn.textContent = 'Save Edit';
  cancelEditBtn.style.display = 'inline-flex';
  addValueInput.focus();
}

function cancelEdit() {
  editingRowId = null;
  if (addValueInput) addValueInput.value = '';
  if (addCategoryInput) addCategoryInput.value = '';
  if (addValueBtn) addValueBtn.textContent = 'Add & Re-redact';
  if (cancelEditBtn) cancelEditBtn.style.display = 'none';
  if (detailErrorEl) detailErrorEl.style.display = 'none';
}

async function addMissingValue() {
  const f = detailFiles[activeFileIdx];
  if (!f) return;
  const source_text = addValueInput.value.trim();
  const category = addCategoryInput.value.trim();
  detailErrorEl.style.display = 'none';
  if (!source_text || !category) return;
  addValueBtn.disabled = true;
  try {
    let res;
    if (editingRowId) {
      res = await fetch(`/api/redaction-map/${editingRowId}`, {
        method: 'PUT', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ source_text, category }),
      });
    } else {
      res = await fetch('/api/redaction-map/add', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ file_id: f.file_id, source_text, category }),
      });
    }
    const data = await res.json();
    if (!res.ok) {
      detailErrorEl.textContent = data.error || 'Request failed';
      detailErrorEl.style.display = 'block';
      return;
    }
    f.rows = data.rows;
    if (data.redacted_filename) f.redacted_filename = data.redacted_filename;
    f.token_pages = (data.preview && data.preview.token_pages) || {};
    f.previewWords = (data.preview && data.preview.words) || null;
    renderTable();
    renderPreview(data.preview);
    cancelEdit();
    relockDownload();
  } catch (e) {
    detailErrorEl.textContent = String(e);
    detailErrorEl.style.display = 'block';
  } finally {
    addValueBtn.disabled = false;
  }
}

function relockDownload() {
  ackCheckbox.checked = false;
  refreshDownloadList();
}

async function deleteRow(rowId) {
  const f = detailFiles[activeFileIdx];
  const row = (f.rows || []).find(r => r.id === rowId);
  const message = row && row.source === 'manual'
    ? 'Remove this manually-added value and restore the original text in the output file?'
    : 'This value was automatically detected as PII. Deleting it will restore the original text in the output file. Are you sure?';
  if (!confirm(message)) return;
  try {
    const res = await fetch(`/api/redaction-map/${rowId}`, { method: 'DELETE' });
    const data = await res.json();
    if (!res.ok) {
      detailErrorEl.textContent = data.error || 'Delete failed';
      detailErrorEl.style.display = 'block';
      return;
    }
    f.rows = data.rows;
    if (data.redacted_filename) f.redacted_filename = data.redacted_filename;
    f.token_pages = (data.preview && data.preview.token_pages) || {};
    f.previewWords = (data.preview && data.preview.words) || null;
    renderTable();
    renderPreview(data.preview);
    relockDownload();
  } catch (e) {
    detailErrorEl.textContent = String(e);
    detailErrorEl.style.display = 'block';
  }
}

/* ── Guided Tour ── */
const TOUR_PART1_SEEN_KEY = 'pii_tour_part1_seen_v1';
const TOUR_PART2_SEEN_KEY = 'pii_tour_part2_seen_v1';

const TOUR_STEPS_PART1 = [
  { title: 'Welcome to the PII Redaction Pipeline', body: "This quick tour shows you what each tool on this page does. Skip anytime — you can reopen it later from the ? button up top." },
  { selector: '#process-name-input', title: 'Process Name', body: 'Give this batch a name so you can find it again later.' },
  { selector: '#client-select', title: 'Client & Job', body: "Pick which client and job this upload belongs to — job options load once you've chosen a client." },
  { selector: '#format-select', title: 'Output Format', body: 'Choose Excel to flatten everything into one workbook, or As it is to keep the original file format.' },
  { selector: '#drop-zone', title: 'Upload Documents', body: 'Drop files here or browse — PDF, Excel, Word, or image files, up to 200 MB each.' },
  { selector: '#run-btn', title: 'Run Redaction', body: "Once you've filled in the details above and added files, click here to start the pipeline." },
  { selector: '.header-actions', title: 'More tools', body: 'DMS opens the Document Management Suite. Rehydrate a file restores the original values from a previously redacted file.' },
  { title: "That's it for now", body: "Once your first run finishes, we'll walk you through the results tools — preview, reference table, and more." },
];

const TOUR_STEPS_PART2 = [
  { selector: '#preview-pane', title: 'Preview', body: 'This shows your redacted file so you can check it before downloading.' },
  { selector: '#select-tool-btn', title: 'Select tool', body: 'Spot a value the pipeline missed? Drag a box around it in the preview to send it to the table below.' },
  { selector: '.ref-table-scroll', title: 'Reference table', body: 'Every value that got masked, and what it was replaced with.' },
  { selector: '.add-row-form', title: 'Add a missing value', body: 'Missed something? Type it here, pick or add a category, and re-redact.' },
  { selector: '#ack-check-wrap', title: 'Acknowledge', body: "Tick this once you've reviewed the output — it unlocks the download button." },
  { selector: '#result-list', title: 'Download', body: 'Grab your redacted file(s) here.' },
  { selector: '#agate-next-btn', title: 'Next', body: 'Once you have ticked the review box, continue to Agate Merger in a new tab.' },
];

const tourOverlayEl = document.getElementById('tour-overlay');
const tourCardEl    = document.getElementById('tour-card');
let tourState = null;          // { steps, idx, sidebarWasCollapsed, onDone }
let tourHighlightEl = null;
let tourPositionFn = null;

function startTour(steps, onDone) {
  if (tourState) endTour(false);
  const sidebarWasCollapsed = appGrid.classList.contains('sidebar-collapsed');
  if (sidebarWasCollapsed) sidebarToggle.click();
  tourState = { steps, idx: 0, sidebarWasCollapsed, onDone };
  tourOverlayEl.classList.add('active');
  tourCardEl.classList.add('active');
  window.addEventListener('keydown', tourKeyHandler);
  window.addEventListener('resize', tourWindowReposition);
  renderTourStep();
}

function tourKeyHandler(e) { if (e.key === 'Escape') endTour(true); }
function tourWindowReposition() { if (tourPositionFn) tourPositionFn(); }

function clearTourHighlight() {
  if (tourHighlightEl) tourHighlightEl.classList.remove('tour-highlight');
  tourHighlightEl = null;
  tourPositionFn = null;
}

function endTour(runOnDone) {
  if (!tourState) return;
  clearTourHighlight();
  tourOverlayEl.classList.remove('active', 'tour-centered-mode');
  tourCardEl.classList.remove('active');
  window.removeEventListener('keydown', tourKeyHandler);
  window.removeEventListener('resize', tourWindowReposition);
  const { sidebarWasCollapsed, onDone } = tourState;
  if (sidebarWasCollapsed) sidebarToggle.click();
  tourState = null;
  if (runOnDone && onDone) onDone();
}

function tourNext() {
  if (!tourState) return;
  if (tourState.idx >= tourState.steps.length - 1) { endTour(true); return; }
  tourState.idx += 1;
  renderTourStep();
}
function tourBack() {
  if (!tourState || tourState.idx === 0) return;
  tourState.idx -= 1;
  renderTourStep();
}

function renderTourStep() {
  if (!tourState) return;
  clearTourHighlight();
  const { steps, idx } = tourState;
  const step = steps[idx];
  const isCentered = !step.selector;

  tourOverlayEl.classList.toggle('tour-centered-mode', isCentered);
  tourCardEl.classList.toggle('centered', isCentered);
  tourCardEl.innerHTML = `
    <button type="button" class="tour-close-btn" onclick="endTour(true)" aria-label="Close tour">&times;</button>
    <h4>${escapeHtml(step.title)}</h4>
    <p>${escapeHtml(step.body)}</p>
    <div class="tour-card-footer">
      <span class="tour-step-count">Step ${idx + 1} of ${steps.length}</span>
      <div class="tour-card-actions">
        ${idx > 0 ? '<button type="button" class="tour-btn" onclick="tourBack()">Back</button>' : ''}
        <button type="button" class="tour-btn" onclick="endTour(true)">Skip</button>
        <button type="button" class="tour-btn tour-btn-primary" onclick="tourNext()">${idx === steps.length - 1 ? 'Done' : 'Next'}</button>
      </div>
    </div>`;

  if (isCentered) {
    tourCardEl.style.top = '';
    tourCardEl.style.left = '';
    return;
  }

  const target = document.querySelector(step.selector);
  if (!target) { tourNext(); return; }
  target.scrollIntoView({ block: 'center', behavior: 'auto' });
  target.classList.add('tour-highlight');
  tourHighlightEl = target;

  const position = () => {
    const r = target.getBoundingClientRect();
    const cardRect = tourCardEl.getBoundingClientRect();
    let top = r.bottom + 16;
    if (top + cardRect.height > window.innerHeight - 12) top = Math.max(12, r.top - cardRect.height - 16);
    let left = Math.min(r.left, window.innerWidth - cardRect.width - 12);
    left = Math.max(12, left);
    tourCardEl.style.top = top + 'px';
    tourCardEl.style.left = left + 'px';
  };
  tourPositionFn = position;
  position();
  setTimeout(position, 80);
}

function maybeAutoStartTourPart1() {
  if (new URLSearchParams(window.location.search).get('token')) return;
  if (localStorage.getItem(TOUR_PART1_SEEN_KEY)) return;
  startTour(TOUR_STEPS_PART1, () => localStorage.setItem(TOUR_PART1_SEEN_KEY, '1'));
}
window.addEventListener('load', () => setTimeout(maybeAutoStartTourPart1, 200));

function maybeAutoStartTourPart2() {
  if (tourState) return;
  if (localStorage.getItem(TOUR_PART2_SEEN_KEY)) return;
  startTour(TOUR_STEPS_PART2, () => localStorage.setItem(TOUR_PART2_SEEN_KEY, '1'));
}

document.getElementById('tour-help-btn').addEventListener('click', () => {
  startTour(TOUR_STEPS_PART1, () => {
    localStorage.setItem(TOUR_PART1_SEEN_KEY, '1');
    if (detailSec.style.display === 'block' && detailGridEl.style.display !== 'none') {
      startTour(TOUR_STEPS_PART2, () => localStorage.setItem(TOUR_PART2_SEEN_KEY, '1'));
    }
  });
});
</script>
</body>
</html>"""


REHYDRATE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Rehydrate — PII Redaction Pipeline</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #f3f5fb;
  --surface: #ffffff;
  --surface2: #f1f3f9;
  --border: rgba(23,25,35,0.08);
  --border-hover: rgba(79,70,229,0.4);
  --text: #171923;
  --text-muted: #6b7280;
  --accent: #4f46e5;
  --accent2: #2563eb;
  --accent-glow: rgba(79,70,229,0.10);
  --green: #15803d;
  --green-bg: rgba(21,128,61,0.09);
  --red: #b91c1c;
  --red-bg: rgba(185,28,28,0.08);
  --amber: #92400e;
  --amber-bg: rgba(217,119,6,0.10);
  --radius: 14px;
  --radius-sm: 8px;
  --shadow: 0 1px 2px rgba(23,25,35,.04), 0 12px 28px -16px rgba(79,70,229,.16);
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: 'Inter', system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  background-image:
    radial-gradient(ellipse 60% 40% at 12% -10%, rgba(79,70,229,0.10) 0%, transparent 60%),
    radial-gradient(ellipse 50% 40% at 100% 0%, rgba(37,99,235,0.08) 0%, transparent 55%);
  background-attachment: fixed;
}

header {
  display: flex; align-items: center; gap: 14px;
  padding: 16px 32px;
  border-bottom: 1px solid var(--border);
  background: rgb(191 216 255 / 80%);
  backdrop-filter: blur(12px);
  position: sticky; top: 0; z-index: 10;
}
.logo {
  width: 36px; height: 36px; border-radius: 10px;
  background: linear-gradient(135deg, #4f46e5, #2563eb);
  box-shadow: 0 4px 14px -4px rgba(79,70,229,.5);
  display: flex; align-items: center; justify-content: center; flex-shrink: 0;
}
.logo svg { width: 20px; height: 20px; color: #fff; }
.header-text h1 { font-size: 1rem; font-weight: 600; letter-spacing: -0.01em; }
.header-text p  { font-size: 0.72rem; color: var(--text-muted); margin-top: 1px; }
.back-link {
  margin-left: auto; font-size: .78rem; font-weight: 500; color: var(--accent);
  text-decoration: none; display: flex; align-items: center; gap: 6px;
}
.back-link:hover { text-decoration: underline; }
.back-link svg { width: 13px; height: 13px; }

main { max-width: 620px; margin: 40px auto; padding: 0 24px 20px; }
.page-intro { margin-bottom: 28px; }
.page-intro h2 { font-size: 1.3rem; font-weight: 700; letter-spacing: -0.01em; margin-bottom: 6px; }
.page-intro p { font-size: .85rem; color: var(--text-muted); line-height: 1.6; max-width: 520px; }

.section-label {
  font-size: 0.7rem; font-weight: 700; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--text-muted);
  margin-bottom: 10px; display: flex; align-items: center; gap: 8px;
}
.section-label .step-num {
  width: 16px; height: 16px; border-radius: 5px; flex-shrink: 0;
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  color: #fff; font-size: .62rem; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}
.section-label::after { content:''; flex:1; height:1px; background:var(--border); }
.section-gap { margin-top: 26px; }

.selector-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 18px;
  display: flex; flex-direction: column; gap: 14px;
}
.field-group { display: flex; flex-direction: column; gap: 6px; position: relative; }
.field-group label { font-size: .75rem; font-weight: 600; color: var(--text-muted); }
.field-input {
  background: var(--surface2); border: 1px solid var(--border);
  border-radius: var(--radius-sm); color: var(--text);
  padding: 10px 12px; font-size: .85rem; font-family: inherit;
  outline: none; transition: border-color .15s;
}
.field-input:focus { border-color: var(--border-hover); }
.field-input:disabled { opacity: .45; cursor: not-allowed; }
select.field-input { appearance: none; cursor: pointer; }

.files-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  box-shadow: var(--shadow); overflow: hidden;
}
.files-card .files-summary {
  padding: 12px 18px; font-size: .78rem; color: var(--text-muted);
  border-bottom: 1px solid var(--border); background: var(--surface2);
}
.file-list { display: flex; flex-direction: column; gap: 1px; }
.file-item {
  display: flex; align-items: center; gap: 12px;
  padding: 11px 18px;
  border-top: 1px solid var(--border);
  font-size: 0.85rem;
  animation: slideIn .18s ease;
}
.file-list > .file-item:first-child { border-top: none; }
@keyframes slideIn { from { opacity:0; transform:translateY(-4px) } to { opacity:1; transform:none } }
.file-icon {
  width: 32px; height: 32px; border-radius: 8px; flex-shrink:0;
  display:flex; align-items:center; justify-content:center; font-size:.65rem; font-weight:700;
}
.file-icon.pdf  { background: rgba(197,48,48,.10);  color:#c53030; }
.file-icon.xlsx { background: rgba(47,133,90,.10);   color:#2f855a; }
.file-icon.docx { background: rgba(43,108,176,.10);  color:#2b6cb0; }
.file-icon.img  { background: rgba(128,90,213,.10);  color:#805ad5; }
.file-name { flex:1; font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.file-size { color: var(--text-muted); font-size:.75rem; flex-shrink:0; }
.badge {
  font-size:.67rem; font-weight:700; padding:3px 9px; border-radius:20px;
  letter-spacing:.05em; text-transform:uppercase; flex-shrink: 0;
}
.badge-neutral { background: var(--surface2); color: var(--text-muted); border: 1px solid var(--border); }
.remove-btn {
  background:none; border:none; cursor:pointer; color:var(--text-muted);
  width:24px; height:24px; border-radius:6px; display:flex; align-items:center; justify-content:center;
  transition: background .15s, color .15s; flex-shrink:0;
}
.remove-btn:hover { background:var(--red-bg); color:var(--red); }

.upload-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  box-shadow: var(--shadow); overflow: hidden;
}
.drop-zone {
  padding: 40px 24px; text-align: center; cursor: pointer;
  transition: background .2s, border-color .2s;
  border: 2px dashed transparent;
  border-radius: var(--radius);
  position: relative;
}
.drop-zone.dragover { background: var(--accent-glow); border-color: var(--accent); }
.drop-zone:hover .upload-icon-wrap { transform: translateY(-3px); }
.upload-icon-wrap {
  width: 56px; height: 56px; border-radius: 14px; margin: 0 auto 14px;
  background: linear-gradient(135deg, rgba(99,179,237,0.15), rgba(99,179,237,0.05));
  border: 1px solid rgba(99,179,237,0.2);
  display: flex; align-items: center; justify-content: center;
  transition: transform .25s cubic-bezier(.34,1.56,.64,1);
}
.upload-icon-wrap svg { width: 24px; height: 24px; color: var(--accent); }
.drop-zone h2 { font-size: .95rem; font-weight: 600; margin-bottom: 6px; }
.drop-zone p  { font-size: 0.8rem; color: var(--text-muted); line-height: 1.5; }
.drop-zone p span { color: var(--accent); }
.drop-zone.has-file { display: none; }
#upload-input { display: none; }

.alert {
  display: none; align-items: flex-start; gap: 10px;
  font-size: .8rem; line-height: 1.55; border-radius: var(--radius-sm);
  padding: 12px 14px; margin-top: 16px;
}
.alert svg { width: 15px; height: 15px; flex-shrink: 0; margin-top: 1px; }
.alert-warning { color: var(--amber); background: var(--amber-bg); border: 1px solid rgba(217,119,6,0.2); }
.alert-error   { color: var(--red);   background: var(--red-bg);   border: 1px solid rgba(185,28,28,0.2); }

.actions { margin-top: 22px; display: flex; gap: 10px; align-items: center; }
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  padding: 11px 22px; border-radius: var(--radius-sm); border: none;
  font-size: 0.875rem; font-weight: 600; cursor: pointer;
  transition: all .18s; white-space:nowrap; font-family:inherit;
}
.btn:disabled { opacity: .4; cursor: not-allowed; pointer-events: none; }
.btn-primary {
  width: 100%;
  background: linear-gradient(135deg, #2d6fa4, #63b3ed);
  color: #fff; box-shadow: 0 2px 12px rgba(99,179,237,0.25);
}
.btn-primary:not(:disabled):hover {
  box-shadow: 0 4px 20px rgba(99,179,237,0.4); transform: translateY(-1px);
}
.spinner {
  width:14px; height:14px; border:2px solid rgba(255,255,255,.4);
  border-top-color:#fff; border-radius:50%;
  animation: spin .7s linear infinite; display: none;
}
@keyframes spin { to { transform: rotate(360deg) } }

@media (max-width: 560px) {
  header { padding: 14px 18px; }
  main { margin: 24px auto; padding: 0 16px 20px; }
}
</style>
</head>
<body>

<header>
  <div class="logo">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
  </div>
  <div class="header-text">
    <h1>Rehydrate a File</h1>
    <p>Restore original values into a derived document</p>
  </div>
  <div style="margin-left:auto; display:flex; align-items:center; gap:16px;">
    {% if is_dashboard_admin %}<a href="/dashboard" class="back-link" style="margin-left:0;">Dashboard</a>{% endif %}
    <a href="/" class="back-link" style="margin-left:0;">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>
      Redaction Pipeline
    </a>
  </div>
</header>

<main>
  <div class="page-intro">
    <h2>Rehydrate a file</h2>
    <p>Pick the client, job, and process this batch was redacted under, then upload the derived document (e.g. a prepared ITR draft) to restore the original values and download it.</p>
  </div>

  <form id="rehydrate-form">
    <div class="section-label"><span class="step-num">1</span>Client &amp; Job</div>
    <div class="selector-card">
      <div class="field-group">
        <label for="client-select">Client</label>
        <select id="client-select" class="field-input" required>
          <option value="">Select a client&hellip;</option>
        </select>
      </div>
      <div class="field-group">
        <label for="job-select">Job</label>
        <select id="job-select" class="field-input" disabled required>
          <option value="">Select a client first</option>
        </select>
      </div>
      <div class="field-group">
        <label for="process-select">Process Name</label>
        <select id="process-select" class="field-input" disabled required>
          <option value="">Select a job first</option>
        </select>
      </div>
    </div>

    <div id="files-section" style="display:none">
      <div class="section-label section-gap"><span class="step-num">2</span>Files in This Process</div>
      <div class="files-card">
        <div class="files-summary" id="files-summary"></div>
        <div class="file-list" id="files-body"></div>
      </div>
    </div>

    <div id="upload-section" style="display:none">
      <div class="section-label section-gap"><span class="step-num">3</span>Upload Derived Document</div>
      <div class="upload-card">
        <div class="drop-zone" id="drop-zone">
          <label for="upload-input" style="cursor:pointer;display:block">
            <div class="upload-icon-wrap">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
                <path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>
                <polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/>
              </svg>
            </div>
            <h2>Drop file here or <span>browse</span></h2>
            <p>PDF, XLSX, XLS, DOCX, DOC &nbsp;&middot;&nbsp; images can't be rehydrated</p>
          </label>
          <input type="file" id="upload-input" accept=".pdf,.xlsx,.xls,.docx,.doc,.pptx,.png,.jpg,.jpeg,.eml,.msg">
        </div>
        <div class="file-list" id="upload-preview"></div>
      </div>
    </div>

    <div class="alert alert-warning" id="warning">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
      <span id="warning-text"></span>
    </div>
    <div class="alert alert-error" id="error">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
      <span id="error-text"></span>
    </div>

    <div class="actions">
      <button type="submit" class="btn btn-primary" id="rehydrate-btn" disabled>
        <span class="spinner" id="btn-spinner"></span>
        <span id="btn-label">Rehydrate &amp; Download</span>
      </button>
    </div>
  </form>
</main>
<script>
const clientSelect   = document.getElementById('client-select');
const jobSelect      = document.getElementById('job-select');
const processSelect  = document.getElementById('process-select');
const filesSection   = document.getElementById('files-section');
const filesSummary   = document.getElementById('files-summary');
const filesBody      = document.getElementById('files-body');
const uploadSection  = document.getElementById('upload-section');
const dropZone       = document.getElementById('drop-zone');
const uploadInput    = document.getElementById('upload-input');
const uploadPreview  = document.getElementById('upload-preview');
const warningEl      = document.getElementById('warning');
const warningTextEl  = document.getElementById('warning-text');
const errorEl        = document.getElementById('error');
const errorTextEl    = document.getElementById('error-text');
const btn            = document.getElementById('rehydrate-btn');
const btnSpinner     = document.getElementById('btn-spinner');
const btnLabel       = document.getElementById('btn-label');

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

function extOf(name) { return name.slice(name.lastIndexOf('.')).toLowerCase().replace('.', ''); }

function iconFor(ext) {
  const isImg = ext === 'png' || ext === 'jpg' || ext === 'jpeg';
  const isEmail = ext === 'eml' || ext === 'msg';
  const label = ext === 'pdf' ? 'PDF' : (ext === 'docx' || ext === 'doc') ? 'DOC' : ext === 'pptx' ? 'PPT' : isEmail ? 'EML' : isImg ? 'IMG' : 'XLS';
  const cls   = ext === 'pdf' ? 'pdf' : (ext === 'docx' || ext === 'doc') ? 'docx' : ext === 'pptx' ? 'docx' : isEmail ? 'docx' : isImg ? 'img' : 'xlsx';
  return { label, cls };
}

function resetDownstream(fromProcess) {
  if (!fromProcess) {
    processSelect.innerHTML = '<option value="">Select a job first</option>';
    processSelect.disabled = true;
  }
  filesSection.style.display = 'none';
  filesBody.innerHTML = '';
  uploadSection.style.display = 'none';
  clearUpload();
  updateButtonState();
}

function updateButtonState() {
  btn.disabled = !(processSelect.value && uploadInput.files.length);
}

async function loadClients() {
  try {
    const res = await fetch('/api/clients');
    const clients = await res.json();
    clients.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.pid; opt.textContent = c.name;
      clientSelect.appendChild(opt);
    });
  } catch (e) { console.error('Failed to load clients', e); }
}
loadClients();

clientSelect.addEventListener('change', () => {
  jobSelect.innerHTML = '';
  resetDownstream(false);
  if (!clientSelect.value) {
    jobSelect.disabled = true;
    jobSelect.appendChild(new Option('Select a client first', ''));
    return;
  }
  jobSelect.disabled = true;
  jobSelect.appendChild(new Option('Loading jobs…', ''));
  loadJobsForClient(clientSelect.value);
});

async function loadJobsForClient(clientId) {
  try {
    const res = await fetch(`/api/jobs?client_id=${encodeURIComponent(clientId)}`);
    const jobs = await res.json();
    jobSelect.innerHTML = '';
    jobSelect.appendChild(new Option('Select a job…', ''));
    jobs.forEach(j => {
      jobSelect.appendChild(new Option(j.date ? `${j.description} (${j.date})` : j.description, j.aid));
    });
    jobSelect.disabled = false;
  } catch (e) {
    console.error('Failed to load jobs', e);
    jobSelect.innerHTML = '';
    jobSelect.appendChild(new Option('Failed to load jobs', ''));
  }
}

jobSelect.addEventListener('change', () => {
  resetDownstream(false);
  if (!jobSelect.value) {
    processSelect.disabled = true;
    processSelect.innerHTML = '<option value="">Select a job first</option>';
    return;
  }
  processSelect.disabled = true;
  processSelect.innerHTML = '<option value="">Loading processes…</option>';
  loadProcesses(clientSelect.value, jobSelect.value);
});

async function loadProcesses(clientId, jobId) {
  try {
    const res = await fetch(`/api/processes?client_id=${encodeURIComponent(clientId)}&job_id=${encodeURIComponent(jobId)}`);
    const processes = await res.json();
    processSelect.innerHTML = '';
    if (!Array.isArray(processes) || !processes.length) {
      processSelect.appendChild(new Option('No named processes for this job', ''));
      processSelect.disabled = true;
      return;
    }
    processSelect.appendChild(new Option('Select a process…', ''));
    processes.forEach(p => {
      const label = p.file_count === 1 ? `${p.process_name} (1 file)` : `${p.process_name} (${p.file_count} files)`;
      processSelect.appendChild(new Option(label, p.process_id));
    });
    processSelect.disabled = false;
  } catch (e) {
    console.error('Failed to load processes', e);
    processSelect.innerHTML = '';
    processSelect.appendChild(new Option('Failed to load processes', ''));
  }
}

function hideAlerts() {
  errorEl.style.display = 'none';
  warningEl.style.display = 'none';
}

function showError(message) {
  errorTextEl.textContent = message;
  errorEl.style.display = 'flex';
}

function showWarning(message) {
  warningTextEl.textContent = message;
  warningEl.style.display = 'flex';
}

processSelect.addEventListener('change', async () => {
  filesSection.style.display = 'none';
  filesBody.innerHTML = '';
  uploadSection.style.display = 'none';
  clearUpload();
  hideAlerts();
  updateButtonState();
  if (!processSelect.value) return;

  try {
    const res = await fetch(`/api/redaction-map/${encodeURIComponent(processSelect.value)}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Failed to load files');
    const files = data.files || [];
    filesSummary.textContent = files.length === 1 ? '1 file' : `${files.length} files`;
    files.forEach(f => {
      const { label, cls } = iconFor(extOf(f.filename));
      const count = (f.rows || []).length;
      const d = document.createElement('div');
      d.className = 'file-item';
      d.innerHTML = `
        <div class="file-icon ${cls}">${label}</div>
        <span class="file-name">${escapeHtml(f.filename)}</span>
        <span class="badge badge-neutral">${count} value${count === 1 ? '' : 's'}</span>`;
      filesBody.appendChild(d);
    });
    filesSection.style.display = 'block';
    uploadSection.style.display = 'block';
  } catch (e) {
    showError(String(e.message || e));
  }
  updateButtonState();
});

function clearUpload() {
  uploadInput.value = '';
  uploadPreview.innerHTML = '';
  dropZone.classList.remove('has-file');
}

function renderUploadPreview() {
  uploadPreview.innerHTML = '';
  const file = uploadInput.files[0];
  if (!file) { dropZone.classList.remove('has-file'); return; }
  const { label, cls } = iconFor(extOf(file.name));
  const mb = (file.size / 1024 / 1024).toFixed(1);
  const d = document.createElement('div');
  d.className = 'file-item';
  d.innerHTML = `
    <div class="file-icon ${cls}">${label}</div>
    <span class="file-name">${escapeHtml(file.name)}</span>
    <span class="file-size">${mb} MB</span>
    <button type="button" class="remove-btn" title="Remove">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="width:13px;height:13px"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>`;
  d.querySelector('.remove-btn').addEventListener('click', () => { clearUpload(); updateButtonState(); });
  uploadPreview.appendChild(d);
  dropZone.classList.add('has-file');
}

function setUploadFile(file) {
  if (!file) return;
  const dt = new DataTransfer();
  dt.items.add(file);
  uploadInput.files = dt.files;
  renderUploadPreview();
  updateButtonState();
}

uploadInput.addEventListener('change', () => { renderUploadPreview(); updateButtonState(); });
dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('dragover'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('dragover');
  if (e.dataTransfer.files.length) setUploadFile(e.dataTransfer.files[0]);
});

document.getElementById('rehydrate-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  hideAlerts();
  if (!processSelect.value || !uploadInput.files.length) return;

  const form = new FormData();
  form.append('process_id', processSelect.value);
  form.append('file', uploadInput.files[0]);

  btn.disabled = true;
  btnSpinner.style.display = 'inline-block';
  btnLabel.textContent = 'Rehydrating…';
  try {
    const res = await fetch('/rehydrate/upload', { method: 'POST', body: form });
    if (!res.ok) {
      const data = await res.json().catch(() => ({error: 'Rehydration failed'}));
      showError(data.error || 'Rehydration failed');
      return;
    }

    const warningsHeader = res.headers.get('X-Rehydrate-Warnings');
    if (warningsHeader) {
      try {
        const info = JSON.parse(decodeURIComponent(warningsHeader));
        const tokens = (info.conflicts || []).map(c => c.token).join(', ');
        showWarning(`${info.conflicts.length} token(s) had ambiguous values across this batch and were left unresolved: ${tokens}. Review and hand-fix if needed.`);
      } catch (e) { console.error('Failed to parse warnings header', e); }
    }

    const blob = await res.blob();
    const disposition = res.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="?([^"]+)"?/);
    const filename = match ? match[1] : 'rehydrated_file';
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    showError(String(err));
  } finally {
    btnSpinner.style.display = 'none';
    btnLabel.textContent = 'Rehydrate & Download';
    updateButtonState();
  }
});
</script>
</body>
</html>"""



@app.route("/")
@swag_from("docs/swagger/index.yml")
def index():
    uid_enc = request.args.get("uid")
    if uid_enc:
        try:
            session["uid"] = int(_decrypt_uid_param(uid_enc))
            session.permanent = True
        except Exception as e:
            print(f"[WARN] Failed to decrypt uid '{uid_enc}': {e}")
    dms_href = build_dms_href(session.get("uid"))
    return render_template_string(
        HTML, dms_href=dms_href, agate_href=build_agate_href(session.get("uid")),
        is_dashboard_admin=_is_dashboard_admin(session.get("uid")),
        review_idle_seconds=REVIEW_IDLE_SECONDS,
    )


@app.route("/dms")
def dms_page():
    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>DMS</title>
  <style>
    :root {
      --bg: #f3f5fb;
      --surface: #ffffff;
      --text: #171923;
      --text-muted: #6b7280;
      --accent: #4f46e5;
      --accent2: #2563eb;
      --border: rgba(23,25,35,0.08);
      --radius: 16px;
      --shadow: 0 1px 2px rgba(23,25,35,.04), 0 12px 28px -16px rgba(79,70,229,.16);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 32px;
      max-width: 560px;
      width: 100%;
      text-align: center;
    }
    h1 { margin: 0 0 10px; font-size: 1.7rem; }
    p { margin: 0 0 18px; color: var(--text-muted); line-height: 1.6; }
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 10px 16px;
      border-radius: 999px;
      background: linear-gradient(135deg, var(--accent), var(--accent2));
      color: #fff;
      text-decoration: none;
      font-weight: 600;
    }
  </style>
</head>
<body>
  <div class="card">
    <h1>DMS</h1>
    <p>The Document Management Suite entry point is now available from the navigation bar. You can extend this page with your DMS workflows, document lists, or search tools here.</p>
    <a class="btn" href="/">Back to Redaction Pipeline</a>
  </div>
</body>
</html>
""")


@app.route("/api/link-context")
def api_link_context():
    uid_enc = request.args.get("uid", "").strip()
    if uid_enc:
        try:
            session["uid"] = int(_decrypt_uid_param(uid_enc))
            session.permanent = True
        except Exception as e:
            print(f"[WARN] Failed to decrypt uid '{uid_enc}': {e}")

    token = request.args.get("token", "").strip()
    context = _resolve_handoff_token(token)
    if not context.get("process_id"):
        return jsonify({"error": "Could not resolve process context from token"}), 400

    process_id = context["process_id"]
    files = db.get_files_for_process(process_id)
    if not files:
        return jsonify({"error": f"Unknown process_id: {process_id}"}), 404

    storage_path = context.get("storage_path")
    if context.get("file_id") and storage_path:
        _remember_dms_storage_path(context["file_id"], storage_path)
    elif storage_path:
        for f in files:
            filename = str(f.get("filename") or "")
            redacted_path = str(f.get("redacted_path") or "")
            if storage_path in filename or storage_path in redacted_path or Path(storage_path).name in filename:
                context["file_id"] = f["file_id"]
                _remember_dms_storage_path(f["file_id"], storage_path)
                break

    try:
        client = db.get_client(context.get("client_id"))
        context["client_name"] = client["name"] if client else None
    except Exception as e:
        print(f"[WARN] Failed to look up client: {e}")
        context["client_name"] = None

    try:
        job = db.get_job(context.get("job_id"))
        context["job_name"] = job["description"] if job else None
    except Exception as e:
        print(f"[WARN] Failed to look up job: {e}")
        context["job_name"] = None

    return jsonify(context)


@app.route("/api/clients")
@swag_from("docs/swagger/clients.yml")
def api_clients():
    auth_err = _require_uid()
    if auth_err:
        return auth_err
    try:
        uid = _resolve_uid()
        allowed_pids = db.get_allowed_pids(uid) if uid is not None else None
        return jsonify(db.get_clients(allowed_pids))
    except Exception as e:
        print(f"[WARN] Failed to load clients: {e}")
        return jsonify({"error": "Failed to load clients"}), 500


@app.route("/api/jobs")
@swag_from("docs/swagger/jobs.yml")
def api_jobs():
    auth_err = _require_uid()
    if auth_err:
        return auth_err
    client_id = request.args.get("client_id", type=int)
    if not client_id:
        return jsonify({"error": "client_id required"}), 400
    uid = _resolve_uid()
    if uid is not None and client_id not in db.get_allowed_pids(uid):
        return jsonify({"error": "Client not permitted for this user"}), 403
    q = request.args.get("q", "")
    try:
        return jsonify(db.search_jobs(client_id, q))
    except Exception as e:
        print(f"[WARN] Failed to search jobs for client_id={client_id}: {e}")
        return jsonify({"error": "Failed to search jobs"}), 500


@app.route("/api/processes")
@swag_from("docs/swagger/processes.yml")
def api_processes():
    """Process Name picker for the rehydrate page
    (wiki/feature-updates/phase-1/plan-rehyrdration-implementation.md,
    Section 3.4.1) — scoped to one client+job (Open Question NEW-4)."""
    auth_err = _require_uid()
    if auth_err:
        return auth_err

    client_id = request.args.get("client_id", type=int)
    job_id = request.args.get("job_id", type=int)
    if not client_id or not job_id:
        return jsonify({"error": "client_id and job_id are required"}), 400
    uid = _resolve_uid()
    if uid is not None and client_id not in db.get_allowed_pids(uid):
        return jsonify({"error": "Client not permitted for this user"}), 403
    try:
        return jsonify(db.list_processes(client_id, job_id))
    except Exception as e:
        print(f"[WARN] Failed to list processes for client_id={client_id}, job_id={job_id}: {e}")
        return jsonify({"error": "Failed to load processes"}), 500


@app.route("/api/batch/<process_id>/cancel", methods=["POST"])
@swag_from("docs/swagger/cancel_batch.yml")
def api_cancel_batch(process_id: str):
    """Mark an in-progress batch cancelled. dual_redaction_pipeline.py's main()
    checks this before starting each remaining file and stops early — the
    file already being processed still finishes (see
    wiki/plan-compare-wiki.md Q2)."""
    auth_err = _require_uid()
    if auth_err:
        return auth_err
    try:
        rows = db.cancel_batch(process_id)
    except Exception as e:
        print(f"[WARN] Failed to cancel batch process_id={process_id}: {e}")
        return jsonify({"error": "Failed to cancel batch"}), 500
    return jsonify({"process_id": process_id, "cancelled_rows": rows})


REVIEW_TIME_MAX_SECONDS = 24 * 60 * 60


@app.route("/api/agate-handoff", methods=["POST"])
def api_agate_handoff():
    """Review page's Next button: push this process's current redacted files
    (same files as the per-file / ZIP downloads, including manual re-redacts)
    to Agate Merger's POST /api/intake, and return the Agate URL that opens
    with them already in its upload list."""
    auth_err = _require_uid()
    if auth_err:
        return auth_err
    uid = _resolve_uid()
    if uid is None:
        return jsonify({"error": "Open this page from your signed-in link to continue."}), 400
    process_id = str((request.get_json(silent=True) or {}).get("process_id") or "").strip()
    if not process_id:
        return jsonify({"error": "process_id is required"}), 400
    file_rows = db.get_files_for_process(process_id)
    if not file_rows:
        return jsonify({"error": f"Unknown process_id: {process_id}"}), 404
    if file_rows[0]["client_id"] not in db.get_allowed_pids(uid):
        return jsonify({"error": "Client not permitted for this user"}), 403
    base = (AGATE_MERGER_URL or "").rstrip("/")
    if not base or not AGATE_INTAKE_API_KEY:
        return jsonify({"error": "Agate Merger hand-off is not configured (AGATE_MERGER_URL / AGATE_INTAKE_API_KEY)."}), 500

    upload, missing = [], []
    for row in file_rows:
        path = Path(row["redacted_path"]) if row.get("redacted_path") else None
        if path and path.exists():
            upload.append(("files", (path.name, path.read_bytes(), "application/octet-stream")))
        else:
            missing.append(row.get("filename") or row.get("file_id"))
    if not upload:
        return jsonify({"error": "No redacted files found on disk for this process."}), 404

    enc_uid = crypto_utils.encrypt(str(uid))
    try:
        resp = httpx.post(
            f"{base}/api/intake",
            files=upload,
            # process/client/job travel with the files so the app that finally
            # builds a summary can ask /api/rehydrate/* to restore real values
            # on download.
            data={"uid": enc_uid, "source": "pii-redaction", "process_id": process_id,
                  "client_id": str(file_rows[0]["client_id"]), "job_id": str(file_rows[0]["job_id"])},
            headers={"X-Agate-Intake-Key": AGATE_INTAKE_API_KEY},
            timeout=180.0,
        )
        body = resp.json()
    except Exception as e:
        print(f"[WARN] Agate hand-off failed for process {process_id}: {e}")
        return jsonify({"error": f"Could not reach Agate Merger ({e.__class__.__name__})."}), 502
    if resp.status_code != 200 or not body.get("ok"):
        return jsonify({"error": f"Agate Merger refused the files: {body.get('error') or resp.status_code}"}), 502

    return jsonify({
        "redirect_url": f"{base}/?uid={quote(enc_uid, safe='')}&intake={body['token']}",
        "sent": len(upload),
        "missing": missing,
        "rejected": body.get("rejected", []),
    })


# ---------------------------------------------------------------------------
# Server-to-server rehydration for summaries built downstream (Agate apps).
# The Insurance/Portfolio/Fact Find backend calls these with X-API-Key when
# the user clicks Download, so the file they save has real values while every
# copy stored on the servers stays redacted.
# ---------------------------------------------------------------------------

_PROCESS_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TOKEN_TEXT_RE = re.compile(r"<\*[A-Z0-9_]+?_\d+\*>")


def _rehydrate_scope(values):
    """Validate a /api/rehydrate/* call and build the merged token map.
    Returns (token_map, error_response). Rules: API key only; the uid must be
    allowed the client; every process must belong to exactly that client+job
    (token numbering is only stable within one client+job, so mixing would
    risk putting one client's names on another's document)."""
    if not _api_key_valid():
        return None, (jsonify({"error": "Invalid or missing API key"}), 401)
    try:
        uid = int(values.get("uid"))
        client_id = int(values.get("client_id"))
        job_id = int(values.get("job_id"))
    except (TypeError, ValueError):
        return None, (jsonify({"error": "uid, client_id and job_id must be integers"}), 400)
    raw_ids = values.get("process_ids") or ""
    process_ids = sorted({p.strip() for p in (raw_ids if isinstance(raw_ids, list) else str(raw_ids).split(",")) if str(p).strip()})
    if not process_ids or not all(_PROCESS_ID_RE.match(p) for p in process_ids):
        return None, (jsonify({"error": "process_ids is required"}), 400)
    if client_id not in db.get_allowed_pids(uid):
        return None, (jsonify({"error": "Client not permitted for this user"}), 403)

    token_map = {}
    for process_id in process_ids:
        rows = db.get_files_for_process(process_id)
        if not rows:
            return None, (jsonify({"error": f"Unknown process_id: {process_id}"}), 404)
        if any(r["client_id"] != client_id or r["job_id"] != job_id for r in rows):
            return None, (jsonify({"error": f"{process_id} does not belong to this client/job"}), 403)
        for token, candidates in db.get_redaction_map_for_process(process_id).items():
            merged = token_map.setdefault(token, {})
            for source_text, file_ids in candidates.items():
                merged.setdefault(source_text, []).extend(file_ids)
    print(f"[AUDIT] rehydrate uid={uid} client={client_id} job={job_id} processes={','.join(process_ids)}")
    return token_map, None


@app.route("/api/rehydrate/file", methods=["POST"])
def api_rehydrate_file():
    """multipart: file, uid, client_id, job_id, process_ids (comma-separated).
    Returns the file with every unambiguous token replaced. A file with no
    tokens at all comes back unchanged (nothing to restore). Headers:
    X-Rehydrate-Matched / X-Rehydrate-Unresolved (tokens still left)."""
    token_map, err = _rehydrate_scope(request.form)
    if err:
        return err
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"error": "No file uploaded"}), 400
    ext = Path(secure_filename(upload.filename)).suffix.lower()
    if ext not in REHYDRATE_ALLOWED_EXTENSIONS or ext in redaction_formats.IMAGE_EXTENSIONS:
        return jsonify({"error": f"Unsupported file type: {ext}"}), 400

    # Unique temp folder per call (concurrent downloads can't collide) and
    # always deleted -- the rehydrated copy holds real PII.
    work_dir = Path(tempfile.mkdtemp(prefix="rehydrate_", dir=str(REHYDRATE_UPLOADS_DIR)))
    try:
        in_path = work_dir / f"in{ext}"
        out_path = work_dir / f"out{ext}"
        upload.save(str(in_path))
        # No yellow marking: this is the finished document the adviser hands
        # on, not a review copy (the /rehydrate page keeps its highlight).
        result = redaction_formats.rehydrate_uploaded_file(in_path, out_path, token_map, highlight=False)
        final = in_path if result["zero_match"] else out_path
        unresolved = len(redaction_formats.find_tokens_in_file(final))
        data = final.read_bytes()
    except Exception as e:
        print(f"[WARN] /api/rehydrate/file failed: {e}")
        return jsonify({"error": f"Rehydration failed: {e}"}), 500
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    resp = Response(data, mimetype=upload.mimetype or "application/octet-stream")
    resp.headers["X-Rehydrate-Matched"] = str(result["matched"])
    resp.headers["X-Rehydrate-Unresolved"] = str(unresolved)
    return resp


@app.route("/api/rehydrate/text", methods=["POST"])
def api_rehydrate_text():
    """JSON {uid, client_id, job_id, process_ids, texts: [..]} -> {texts: [..]}
    -- for short values such as the client surname used in a file name."""
    body = request.get_json(silent=True) or {}
    token_map, err = _rehydrate_scope(body)
    if err:
        return err

    def restore(match):
        candidates = token_map.get(match.group(0))
        return next(iter(candidates)) if candidates and len(candidates) == 1 else match.group(0)

    texts = [_TOKEN_TEXT_RE.sub(restore, str(t)) for t in (body.get("texts") or [])]
    return jsonify({"texts": texts})


def _review_time_context(process_id: str):
    """Shared auth + lookup for the review-time routes. Returns (uid, error_response)."""
    auth_err = _require_uid()
    if auth_err:
        return None, auth_err
    uid = _resolve_uid()
    if uid is None:
        return None, (jsonify({"error": "uid is required to track review time"}), 400)
    files = db.get_files_for_process(process_id)
    if not files:
        return None, (jsonify({"error": f"Unknown process_id: {process_id}"}), 404)
    if files[0]["client_id"] not in db.get_allowed_pids(uid):
        return None, (jsonify({"error": "Client not permitted for this user"}), 403)
    return uid, None


@app.route("/api/process/<process_id>/review-time")
@swag_from("docs/swagger/review_time_get.yml")
def api_review_time_get(process_id: str):
    """System (pipeline) time for the batch plus any review time already saved
    by this user — the results screen's timer pill starts from these values."""
    try:
        uid, err = _review_time_context(process_id)
        if err:
            return err
        return jsonify({
            "process_id": process_id,
            "idle_threshold_seconds": REVIEW_IDLE_SECONDS,
            "system_seconds": db.get_system_seconds(process_id),
            "review": db.get_review_time(process_id, uid),
        })
    except Exception as e:
        print(f"[WARN] Failed to load review time for process_id={process_id}: {e}")
        return jsonify({"error": "Failed to load review time"}), 500


@app.route("/api/process/<process_id>/review-time", methods=["POST"])
@swag_from("docs/swagger/review_time_save.yml")
def api_review_time_save(process_id: str):
    """Save the review timer on a Download click (plan-timer-implementation.md
    Q2: every download saves; the stored value is the latest one)."""
    body = request.get_json(silent=True) or {}
    fields = ["active_seconds", "idle_seconds", "tab_hidden_seconds",
              "idle_pauses", "tab_pauses", "wall_seconds"]
    values = {}
    for name in fields:
        value = body.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return jsonify({"error": f"{name} must be a non-negative integer"}), 400
        values[name] = min(value, REVIEW_TIME_MAX_SECONDS)
    try:
        uid, err = _review_time_context(process_id)
        if err:
            return err
        db.save_review_time(
            process_id, uid,
            values["active_seconds"], values["idle_seconds"], values["tab_hidden_seconds"],
            values["idle_pauses"], values["tab_pauses"], values["wall_seconds"],
            REVIEW_IDLE_SECONDS, db.get_system_seconds(process_id),
        )
        return jsonify({"process_id": process_id, "review": db.get_review_time(process_id, uid)})
    except Exception as e:
        print(f"[WARN] Failed to save review time for process_id={process_id}: {e}")
        return jsonify({"error": "Failed to save review time"}), 500


@app.route("/upload", methods=["POST"])
@swag_from("docs/swagger/upload.yml")
def upload():
    api_key_header = request.headers.get("X-API-Key")
    if api_key_header is not None and not _api_key_valid():
        return jsonify({"error": "Invalid API key"}), 401

    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files received"}), 400

    process_name = request.form.get("process_name", "").strip()
    client_id = request.form.get("client_id", type=int)
    job_id = request.form.get("job_id", type=int)
    if not process_name:
        return jsonify({"error": "Process Name is required"}), 400
    if not client_id or not job_id:
        return jsonify({"error": "Client and Job are required"}), 400

    uid = session.get("uid")
    if uid is None and api_key_header is not None:
        # Server-to-server caller, authenticated via X-API-Key above — no
        # session to derive a uid from, so the caller supplies it directly.
        uid = request.form.get("uid", type=int)
    if uid is None:
        print("[WARN] No uid in session — skipping upload log")
    elif client_id not in db.get_allowed_pids(uid):
        return jsonify({"error": "Client not permitted for this user"}), 403

    output_format = request.form.get("output_format", "excel")
    if output_format not in ("excel", "as_is"):
        return jsonify({"error": f"Unsupported output_format: {output_format}"}), 400

    client = db.get_client(client_id)
    if not client:
        return jsonify({"error": "Unknown or inactive client"}), 400
    job = db.get_job(job_id)
    if not job or job["pid"] != client_id:
        return jsonify({"error": "Unknown job for this client"}), 400

    saved = []
    for f in files:
        name = secure_filename(f.filename)
        if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
            return jsonify({"error": f"Unsupported type: {name}"}), 400
        f.save(str(FILES_DIR / name))
        saved.append(name)

    client_code = db.get_or_create_client_code(client_id, client["name"], client["short_name"])
    process_id = db.next_id("PRC", client_code)

    manifest_files = []
    file_id_to_log_id = {}
    for name in saved:
        stem = Path(name).stem
        file_id = db.next_id("FIL", client_code)
        # Placeholder path: for "as_is" this is a best-guess (input extension) —
        # if the pipeline falls back to Excel at runtime, _run_pipeline() patches
        # this row with the true path/format once the subprocess finishes.
        placeholder_ext = Path(name).suffix.lower() if output_format == "as_is" else ".xlsx"
        redacted_path = str(OUTPUT_DIR / f"{stem}_redacted{placeholder_ext}")
        db.record_file(file_id, process_id, client_id, job_id, name, redacted_path, output_format, process_name)
        manifest_files.append({"filename": name, "file_id": file_id})

        log_id = None
        if uid is not None:
            try:
                log_id = db.log_upload(uid, name, process_id)
            except Exception as e:
                print(f"[WARN] Failed to log upload for uid={uid}, file={name}: {e}")
        file_id_to_log_id[file_id] = log_id

    manifest_path = FILES_DIR / f"manifest_{process_id}.json"
    manifest_path.write_text(
        json.dumps({
            "client_id": client_id,
            "job_id": job_id,
            "process_id": process_id,
            "process_name": process_name,
            "output_format": output_format,
            "files": manifest_files,
        }),
        encoding="utf-8",
    )

    q = queue.Queue()
    with _job_lock:
        _job_queues[process_id] = q

    threading.Thread(
        target=_run_pipeline,
        args=(process_id, saved, file_id_to_log_id, manifest_path),
        daemon=True,
    ).start()
    return jsonify({"job_id": process_id, "saved": saved, "files": manifest_files})


def _mark_upload_status(log_id, success: bool, error_message: str = None, output_file: str = None):
    if log_id is None:
        return
    try:
        db.update_upload_status(log_id, "success" if success else "fail", error_message, output_file)
    except Exception as e:
        print(f"[WARN] Failed to update upload status for log_id={log_id}: {e}")


def _mark_upload_statuses(file_id_to_log_id: dict, success: bool, error_message: str = None,
                           output_by_file_id: dict = None, done_log_ids: set = None):
    """Mark every not-yet-updated log row in this batch with the overall
    success/fail — the fallback for files that got a log_id but no
    per-file result (e.g. the pipeline crashed before writing any output)."""
    output_by_file_id = output_by_file_id or {}
    done_log_ids = done_log_ids or set()
    for file_id, log_id in file_id_to_log_id.items():
        if log_id is None or log_id in done_log_ids:
            continue
        _mark_upload_status(log_id, success, error_message, output_by_file_id.get(file_id))
        done_log_ids.add(log_id)


def _run_pipeline(process_id: str, filenames: list, file_id_to_log_id: dict = None, manifest_path: Path = None):
    file_id_to_log_id = file_id_to_log_id or {}
    q = _job_queues[process_id]
    venv_python = BASE_DIR / "pii_env" / "Scripts" / "python.exe"
    python = str(venv_python) if (os.name == "nt" and venv_python.exists()) else sys.executable
    script = str(BASE_DIR / "dual_redaction_pipeline.py")

    q.put(("log", f"[INFO] Python : {python}"))
    q.put(("log", f"[INFO] Script : {script}"))
    q.put(("log", f"[INFO] BaseDir: {BASE_DIR}"))

    if not Path(python).exists():
        q.put(("log", f"[ERROR] Python not found: {python}"))
        q.put(("done", {"success": False, "output_files": []}))
        _mark_upload_statuses(file_id_to_log_id, False, f"Python not found: {python}")
        return
    if not Path(script).exists():
        q.put(("log", f"[ERROR] Pipeline script not found: {script}"))
        q.put(("done", {"success": False, "output_files": []}))
        _mark_upload_statuses(file_id_to_log_id, False, f"Pipeline script not found: {script}")
        return

    # Delete existing raw and redacted outputs for uploaded files so pipeline regenerates.
    # Redacted output extension varies with output_format ("as_is" keeps the source
    # extension), so glob for any {stem}_redacted.* rather than a fixed suffix.
    RAW_DIR = FILES_DIR / "output" / "raw"
    for name in filenames:
        stem = Path(name).stem
        stale_paths = [RAW_DIR / f"{stem}_raw.xlsx"]
        if OUTPUT_DIR.exists():
            stale_paths += list(OUTPUT_DIR.glob(f"{stem}_redacted.*"))
        for stale in stale_paths:
            if stale.exists():
                stale.unlink()
                q.put(("log", f"[INFO] Cleared existing output: {stale.name}"))

    before = set(OUTPUT_DIR.iterdir()) if OUTPUT_DIR.exists() else set()

    error_message = None
    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        cmd = [python, "-u", script]
        if manifest_path is not None:
            cmd += ["--manifest", str(manifest_path)]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=False, cwd=str(BASE_DIR), env=env,
        )
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            # strip ANSI escape sequences and private-use chars
            import re as _re
            line = _re.sub(r'\x1b\[[0-9;]*[mGKHF]', '', line)
            line = _re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', line)
            line = ''.join(c if ord(c) < 0xe000 or ord(c) > 0xf8ff else '' for c in line)
            if line.strip():
                q.put(("log", line))
        proc.wait()
        success = proc.returncode == 0
        if not success:
            error_message = f"Pipeline exited with code {proc.returncode}"
    except Exception as e:
        q.put(("log", f"[ERROR] {e}"))
        success = False
        error_message = str(e)

    after = set(OUTPUT_DIR.iterdir()) if OUTPUT_DIR.exists() else set()
    new_files = [p.name for p in (after - before)]

    # The pipeline writes a result sidecar naming the *actual* redacted filename and
    # output_format it used per file_id — needed because "As is" mode can silently
    # fall back to Excel at runtime, so the upload-time placeholder path in MySQL
    # (pii_files.redacted_path) may be wrong until patched here.
    result_path = FILES_DIR / f"manifest_{process_id}_result.json"
    output_by_file_id = {}
    done_log_ids = set()
    if result_path.exists():
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            for file_id, info in result.get("files", {}).items():
                redacted_filename = info.get("redacted_filename")
                out_format = info.get("output_format", "excel")
                if redacted_filename:
                    db.update_file_output(file_id, str(OUTPUT_DIR / redacted_filename), out_format)
                    output_by_file_id[file_id] = redacted_filename
                log_id = file_id_to_log_id.get(file_id)
                if log_id is not None:
                    _mark_upload_status(log_id, success, error_message, redacted_filename)
                    done_log_ids.add(log_id)
        except Exception as e:
            q.put(("log", f"[WARN] Failed to reconcile output format from result file: {e}"))
        finally:
            try: result_path.unlink(missing_ok=True)
            except Exception: pass

    # Any file with no per-file result entry (e.g. the pipeline crashed before
    # writing one) still gets the overall batch success/fail, with no output_file.
    _mark_upload_statuses(file_id_to_log_id, success, error_message, output_by_file_id, done_log_ids)

    q.put(("done", {
        "success": success,
        "output_files": new_files,
        "files": [{"file_id": fid, "filename": name} for fid, name in output_by_file_id.items()],
    }))

    for name in filenames:
        try: (FILES_DIR / name).unlink(missing_ok=True)
        except Exception: pass
    if manifest_path is not None:
        try: manifest_path.unlink(missing_ok=True)
        except Exception: pass


@app.route("/stream/<job_id>")
@swag_from("docs/swagger/stream.yml")
def stream(job_id: str):
    with _job_lock:
        q = _job_queues.get(job_id)
    if q is None:
        return Response("Job not found", status=404)

    def generate():
        while True:
            try:
                kind, data = q.get(timeout=15)
            except queue.Empty:
                # keepalive comment so the browser connection stays open
                yield ": keepalive\n\n"
                continue
            if kind == "log":
                yield f"event: log\ndata: {data}\n\n"
            elif kind == "done":
                yield f"event: done\ndata: {json.dumps(data)}\n\n"
                break

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _run_redact(process_id: str, files_request: list):
    """Background-thread body for POST /api/redact — masks each file's
    caller-supplied source_values against its already-redacted output, in
    place, reusing the same mask_single_value()/next_token_for_category()
    building blocks as the single-value 'add missed value' flow
    (api_redaction_map_add). See
    wiki/feature-updates/phase-1/plan-api-redaction-implementation.md,
    Section 3.3/3.7."""
    q = _redact_queues[process_id]
    results = []

    for file_req in files_request:
        file_id = (file_req.get("file_id") or "").strip()
        file_name = file_req.get("file_name") or ""
        source_values = file_req.get("source_values") or []

        file_row = db.get_file(file_id)
        redacted_path = Path(file_row["redacted_path"]) if file_row and file_row.get("redacted_path") else None
        if not file_row or not redacted_path or not redacted_path.exists():
            file_result = {"process_id": process_id, "file_id": file_id, "file_name": file_name,
                            "status": "unknown_file_id", "values": []}
            results.append(file_result)
            q.put(("file_done", file_result))
            continue

        existing_rows = db.get_redaction_rows(file_id)
        values_result = []
        for source_text in source_values:
            source_text = (source_text or "").strip()
            if not source_text:
                continue
            if any(r["source_text"].strip().lower() == source_text.lower() for r in existing_rows):
                continue  # already masked in this file — silent skip (plan Section 5, Q5)
            if len(source_text) < 3:
                values_result.append({"source_value": source_text, "status": "too_short"})  # Q8
                continue

            category = redaction_formats.guess_category(source_text)
            token = redaction_formats.next_token_for_category(
                [r["redacted_text"] for r in existing_rows], category
            )
            try:
                events = redaction_formats.mask_single_value(redacted_path, source_text, token)
            except Exception as e:
                print(f"[WARN] Redact-request masking failed for {file_id}/{source_text}: {e}")
                values_result.append({"source_value": source_text, "status": "error", "error": str(e)})
                continue
            if not events:
                values_result.append({"source_value": source_text, "status": "not_found"})
                continue

            db.insert_redaction_row(
                file_row["client_id"], file_row["job_id"], file_row["process_id"], file_id,
                source_text, token, source="api",
            )
            existing_rows.append({"source_text": source_text, "redacted_text": token})
            values_result.append({"source_value": source_text, "status": "redacted", "category": category, "token": token})

        file_result = {"process_id": process_id, "file_id": file_id, "file_name": file_name,
                        "status": "completed", "values": values_result}
        results.append(file_result)
        q.put(("file_done", file_result))

    q.put(("done", {"process_id": process_id, "files": results}))


@app.route("/api/redact", methods=["POST"])
@swag_from("docs/swagger/redact_request.yml")
def api_redact():
    auth_err = _require_uid()
    if auth_err:
        return auth_err

    data = request.get_json(silent=True) or {}
    process_id = (data.get("process_id") or "").strip()
    files_request = data.get("files")
    if not process_id or not isinstance(files_request, list) or not files_request:
        return jsonify({"error": "process_id and a non-empty files list are required"}), 400
    for f in files_request:
        if not isinstance(f, dict) or not (f.get("file_id") or "").strip():
            return jsonify({"error": "Each entry in files requires a file_id"}), 400
        if not isinstance(f.get("source_values"), list):
            return jsonify({"error": f"file {f.get('file_id')}: source_values must be a list"}), 400

    q = queue.Queue()
    with _job_lock:
        _redact_queues[process_id] = q

    threading.Thread(
        target=_run_redact,
        args=(process_id, files_request),
        daemon=True,
    ).start()

    return jsonify({
        "process_id": process_id,
        "stream_url": f"/stream/redact/{quote(process_id)}",
    }), 202


@app.route("/stream/redact/<process_id>")
@swag_from("docs/swagger/redact_stream.yml")
def stream_redact(process_id: str):
    with _job_lock:
        q = _redact_queues.get(process_id)
    if q is None:
        return Response("Redaction job not found", status=404)

    def generate():
        while True:
            try:
                kind, data = q.get(timeout=15)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if kind == "file_done":
                yield f"event: file_done\ndata: {json.dumps(data)}\n\n"
            elif kind == "done":
                yield f"event: done\ndata: {json.dumps(data)}\n\n"
                break

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/download/<filename>")
@swag_from("docs/swagger/download.yml")
def download(filename: str):
    path = OUTPUT_DIR / secure_filename(filename)
    if not path.exists():
        return "File not found", 404
    return send_file(str(path), as_attachment=True)


@app.route("/download/id/<file_id>")
@swag_from("docs/swagger/download_by_id.yml")
def download_by_id(file_id: str):
    auth_err = _require_uid()
    if auth_err:
        return auth_err

    file_row = db.get_file(file_id)
    if not file_row or not file_row.get("redacted_path"):
        return jsonify({"error": f"Unknown file_id: {file_id}"}), 404
    path = Path(file_row["redacted_path"])
    if not path.exists():
        return jsonify({"error": "Redacted output not found on disk"}), 404
    return send_file(str(path), as_attachment=True, download_name=path.name)


@app.route("/download/process/<process_id>/compliance_package")
@swag_from("docs/swagger/download_compliance_package.yml")
def download_compliance_package(process_id: str):
    """Zip every redacted output (plus its per-file audit log, if present) for
    one process into a single downloadable file — same auth gate as the
    existing per-file download route, no separate access-control layer
    (see wiki/plan-compare-wiki.md Q5)."""
    auth_err = _require_uid()
    if auth_err:
        return auth_err

    file_rows = db.get_files_for_process(process_id)
    if not file_rows:
        return jsonify({"error": f"No files found for process_id: {process_id}"}), 404

    buf = io.BytesIO()
    added_any = False
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for row in file_rows:
            redacted_path = Path(row["redacted_path"]) if row.get("redacted_path") else None
            if redacted_path and redacted_path.exists():
                zf.write(str(redacted_path), arcname=f"redacted/{redacted_path.name}")
                added_any = True

            audit_path = AUDIT_DIR / f"pii_audit_log_{Path(row['filename']).stem}.xlsx"
            if audit_path.exists():
                zf.write(str(audit_path), arcname=f"audit/{audit_path.name}")
                added_any = True

    if not added_any:
        return jsonify({"error": "No redacted outputs or audit logs found on disk for this process"}), 404

    buf.seek(0)
    return send_file(
        buf, as_attachment=True,
        download_name=f"compliance_package_{process_id}.zip",
        mimetype="application/zip",
    )


@app.route("/rehydrate")
@swag_from("docs/swagger/rehydrate_page.yml")
def rehydrate_page():
    return render_template_string(
        REHYDRATE_HTML, is_dashboard_admin=_is_dashboard_admin(session.get("uid"))
    )


@app.route("/rehydrate", methods=["POST"])
@swag_from("docs/swagger/rehydrate_file.yml")
def rehydrate_file():
    auth_err = _require_uid()
    if auth_err:
        return auth_err

    file_id = request.form.get("file_id", "").strip()
    if not file_id:
        return jsonify({"error": "file_id is required"}), 400

    file_row = db.get_file(file_id)
    if not file_row:
        return jsonify({"error": f"Unknown file_id: {file_id}"}), 404

    venv_python = BASE_DIR / "pii_env" / "Scripts" / "python.exe"
    python = str(venv_python) if (os.name == "nt" and venv_python.exists()) else sys.executable
    script = str(BASE_DIR / "dual_redaction_pipeline.py")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        proc = subprocess.run(
            [python, "-u", script, "--unmask-file", file_id],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=str(BASE_DIR), env=env, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Rehydration timed out"}), 500

    output_log = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
    stem = Path(file_row["filename"]).stem
    # "as_is" rehydration restores the original file extension; "excel" always
    # produces .xlsx. file_row["output_format"] reflects the *actual* format used
    # (patched post-run in _run_pipeline() in case "as_is" fell back to Excel).
    if file_row.get("output_format") == "as_is":
        unmasked_ext = Path(file_row["filename"]).suffix.lower()
    else:
        unmasked_ext = ".xlsx"
    unmasked_path = UNMASKED_DIR / f"{stem}_unmasked{unmasked_ext}"

    if proc.returncode != 0 or not unmasked_path.exists():
        print(f"[WARN] Rehydrate failed for {file_id}:\n{output_log}")
        return jsonify({"error": "Rehydration failed — see server log"}), 500

    return send_file(str(unmasked_path), as_attachment=True, download_name=unmasked_path.name)


@app.route("/rehydrate/upload", methods=["POST"])
@swag_from("docs/swagger/rehydrate_upload.yml")
def rehydrate_upload():
    """Rehydrate an uploaded, derived document (e.g. an ITR draft prepared
    from several redacted source files) against a process's full token map —
    see wiki/feature-updates/phase-1/plan-rehyrdration-implementation.md,
    Section 3.0/3.1/3.4. Kept as a separate endpoint from POST /rehydrate
    (Open Question #6) since the two flows validate and fail differently."""
    auth_err = _require_uid()
    if auth_err:
        return auth_err

    process_id = request.form.get("process_id", "").strip()
    if not process_id:
        return jsonify({"error": "process_id is required"}), 400

    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"error": "No file uploaded"}), 400

    name = secure_filename(upload.filename)
    ext = Path(name).suffix.lower()
    if ext not in REHYDRATE_ALLOWED_EXTENSIONS:
        return jsonify({
            "error": f"Unsupported file type: {ext or name}. "
                     f"Supported: {', '.join(sorted(REHYDRATE_ALLOWED_EXTENSIONS))}"
        }), 400
    if ext in redaction_formats.IMAGE_EXTENSIONS:
        return jsonify({
            "error": "Images can't be rehydrated this way — pixel \"as is\" redaction "
                     "burns the redaction box into the image with no searchable text left "
                     "to restore. Upload the PDF/Word/Excel document instead."
        }), 400

    files = db.get_files_for_process(process_id)
    if not files:
        return jsonify({"error": f"Unknown process_id: {process_id}"}), 404

    token_map = db.get_redaction_map_for_process(process_id)

    upload_path = REHYDRATE_UPLOADS_DIR / name
    upload.save(str(upload_path))
    output_path = REHYDRATE_OUTPUT_DIR / f"{Path(name).stem}_rehydrated{ext}"

    try:
        result = redaction_formats.rehydrate_uploaded_file(upload_path, output_path, token_map)
    except Exception as e:
        print(f"[WARN] Rehydration failed for process_id={process_id}, file={name}: {e}")
        return jsonify({"error": f"Rehydration failed: {e}"}), 500

    if result["zero_match"]:
        client = db.get_client(files[0]["client_id"])
        job = db.get_job(files[0]["job_id"])
        client_name = client["name"] if client else str(files[0]["client_id"])
        job_desc = job["description"] if job else str(files[0]["job_id"])
        return jsonify({
            "error": (
                f"None of the {len(token_map)} redacted value(s) for {client_name} / {job_desc} "
                f"were found in the uploaded file — check that the right process was selected."
            )
        }), 400

    response = send_file(str(output_path), as_attachment=True, download_name=output_path.name)
    if result["conflicts"]:
        # Best-effort with a warning (Open Question NEW-1, no answer given —
        # this is the plan's own recommended default, option (c)): conflicting
        # tokens are left as literal text in the file; surface which ones via
        # a response header so the frontend can warn without a second request.
        response.headers["X-Rehydrate-Warnings"] = quote(json.dumps({
            "matched": result["matched"],
            "conflicts": result["conflicts"],
        }))
    return response


# ---------------------------------------------------------------------------
# Reference table + preview + manual "Add missing value" flow
# (wiki/feature-updates/phase-1/plan-table-implementation.md). Gated behind
# session["uid"] like /rehydrate — this is the same plaintext-PII precedent.
# ---------------------------------------------------------------------------

def _api_key_valid() -> bool:
    """Constant-time check of the X-API-Key header against the configured
    server-to-server API_KEY. False (never raises) if the header is missing
    or API_KEY isn't configured — see
    wiki/feature-updates/phase-1/plan-api-key-implementation.md."""
    provided = request.headers.get("X-API-Key", "")
    if not API_KEY or not provided:
        return False
    return hmac.compare_digest(provided, API_KEY)


def _require_uid():
    if session.get("uid") is not None:
        return None
    if request.headers.get("X-API-Key") is not None:
        if _api_key_valid():
            return None
        return jsonify({"error": "Invalid API key"}), 401
    return jsonify({"error": "Not authorized — open this app via your normal signed-in link first"}), 403


def _resolve_uid():
    """uid to use for client-permission scoping (userwiseclient), separate
    from _require_uid()'s auth gate. Session uid wins; otherwise an
    API-key caller may supply one explicitly. Returns None when no uid is
    known at all — trusted API-key callers with no uid are left unscoped,
    same as today's upload-logging behavior."""
    uid = session.get("uid")
    if uid is not None:
        return uid
    if request.headers.get("X-API-Key") is not None and _api_key_valid():
        return request.values.get("uid", type=int)
    return None


def _restore_row(redacted_path: Path, file_row: dict, row: dict):
    """Undo one manually-added row in the redacted output. Token-searchable
    formats (xlsx/xls/docx/doc/pdf) restore the single value in place;
    "as is" images are destructive (pixel-burned, no token text layer), so
    they're regenerated from the stored encrypted original using every
    *other* row still on file for this file_id."""
    ext = redacted_path.suffix.lower()
    if ext in redaction_formats.IMAGE_EXTENSIONS:
        enc_path = file_row.get("original_encrypted_path")
        if not enc_path or not Path(enc_path).exists():
            raise RuntimeError("No stored original found for this image — cannot restore a single value")
        remaining = db.get_redaction_rows(row["file_id"])
        entities = {r["source_text"].lower(): r["redacted_text"] for r in remaining if r["id"] != row["id"]}
        redaction_formats.remask_image_from_original(Path(enc_path), redacted_path, entities)
    else:
        redaction_formats.unmask_single_value(redacted_path, row["redacted_text"], row["source_text"])


def _no_match_error(redacted_path: Path) -> str:
    """Build an actionable 'value not found' message. Scanned/image-based
    pages are matched via OCR (Tesseract), which doesn't always transcribe
    text identically to what a person reads or types (spacing, punctuation,
    misread characters) — so for those files, point at the marquee tool
    (which fills the input from the exact OCR reading already used for
    matching) instead of leaving the user to guess why a visually-correct
    value didn't match."""
    base = "Value not found anywhere in the document."
    ext = redacted_path.suffix.lower()
    likely_scanned = ext in redaction_formats.IMAGE_EXTENSIONS
    if not likely_scanned and ext == ".pdf":
        try:
            doc = redaction_formats.fitz.open(str(redacted_path))
            likely_scanned = any(page.get_images(full=True) for page in doc)
            doc.close()
        except Exception:
            pass
    if likely_scanned:
        return (base + " This looks like a scanned/image page — matching relies on OCR text "
                "recognition, which can read the page slightly differently than what was typed. "
                "Try drawing a selection box directly over the value in the preview instead — it "
                "fills the field with the exact text OCR already detected there.")
    return base


@app.route("/api/redaction-map/<process_id>")
@swag_from("docs/swagger/redaction_map_get.yml")
def api_redaction_map(process_id: str):
    auth_err = _require_uid()
    if auth_err:
        return auth_err

    files = db.get_files_for_process(process_id)
    if not files:
        return jsonify({"error": f"Unknown process_id: {process_id}"}), 404

    result = []
    for f in files:
        redacted_path = Path(f["redacted_path"]) if f["redacted_path"] else None
        result.append({
            "file_id": f["file_id"],
            "filename": f["filename"],
            "redacted_filename": redacted_path.name if redacted_path else None,
            "output_format": f["output_format"],
            "rows": db.get_redaction_rows(f["file_id"]),
        })
    return jsonify({"files": result})


@app.route("/api/preview/<file_id>")
@swag_from("docs/swagger/preview.yml")
def api_preview(file_id: str):
    auth_err = _require_uid()
    if auth_err:
        return auth_err

    file_row = db.get_file(file_id)
    if not file_row or not file_row.get("redacted_path"):
        return jsonify({"error": f"Unknown file_id: {file_id}"}), 404
    redacted_path = Path(file_row["redacted_path"])
    if not redacted_path.exists():
        return jsonify({"error": "Redacted output not found on disk"}), 404

    tokens = [r["redacted_text"] for r in db.get_redaction_rows(file_id)]
    try:
        preview = redaction_formats.render_preview(redacted_path, tokens=tokens)
    except Exception as e:
        print(f"[WARN] Preview failed for {file_id}: {e}")
        return jsonify({"error": f"Preview failed: {e}"}), 500
    return jsonify(preview)


@app.route("/api/preview/<file_id>/ocr-region", methods=["POST"])
@swag_from("docs/swagger/preview_ocr_region.yml")
def api_preview_ocr_region(file_id: str):
    """Fallback for the preview's marquee selection tool: OCR just the box
    the user drew, used only when no pre-extracted word (render_preview's
    `words`) falls under the rectangle client-side."""
    auth_err = _require_uid()
    if auth_err:
        return auth_err

    file_row = db.get_file(file_id)
    if not file_row or not file_row.get("redacted_path"):
        return jsonify({"error": f"Unknown file_id: {file_id}"}), 404
    redacted_path = Path(file_row["redacted_path"])
    if not redacted_path.exists():
        return jsonify({"error": "Redacted output not found on disk"}), 404

    data = request.get_json(silent=True) or {}
    try:
        x0, y0, x1, y1 = (float(data[k]) for k in ("x0", "y0", "x1", "y1"))
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "x0, y0, x1, y1 are required numbers"}), 400
    page = data.get("page")

    try:
        text = redaction_formats.ocr_region(redacted_path, page, x0, y0, x1, y1)
    except Exception as e:
        print(f"[WARN] OCR-region failed for {file_id}: {e}")
        return jsonify({"error": f"OCR failed: {e}"}), 500
    return jsonify({"text": text})


@app.route("/api/redaction-map/add", methods=["POST"])
@swag_from("docs/swagger/redaction_map_add.yml")
def api_redaction_map_add():
    auth_err = _require_uid()
    if auth_err:
        return auth_err

    data = request.get_json(silent=True) or {}
    file_id = (data.get("file_id") or "").strip()
    source_text = (data.get("source_text") or "").strip()
    category = (data.get("category") or "").strip()
    if not file_id or not source_text or not category:
        return jsonify({"error": "file_id, source_text and category are required"}), 400

    file_row = db.get_file(file_id)
    if not file_row or not file_row.get("redacted_path"):
        return jsonify({"error": f"Unknown file_id: {file_id}"}), 404
    redacted_path = Path(file_row["redacted_path"])
    if not redacted_path.exists():
        return jsonify({"error": "Redacted output not found on disk"}), 404

    existing_rows = db.get_redaction_rows(file_id)
    for row in existing_rows:
        if row["source_text"].strip().lower() == source_text.lower():
            return jsonify({"error": f"Already masked as {row['redacted_text']}"}), 409

    token = redaction_formats.next_token_for_category(
        [r["redacted_text"] for r in existing_rows], category
    )

    try:
        events = redaction_formats.mask_single_value(redacted_path, source_text, token)
    except Exception as e:
        print(f"[WARN] Add-value masking failed for {file_id}: {e}")
        return jsonify({"error": f"Masking failed: {e}"}), 500

    if not events:
        return jsonify({"error": _no_match_error(redacted_path)}), 400

    db.insert_redaction_row(
        file_row["client_id"], file_row["job_id"], file_row["process_id"], file_id,
        source_text, token, source="manual",
    )

    rows = db.get_redaction_rows(file_id)
    dms_synced = _sync_redacted_to_dms(file_id, redacted_path, rows)
    try:
        preview = redaction_formats.render_preview(redacted_path, tokens=[r["redacted_text"] for r in rows])
    except Exception as e:
        print(f"[WARN] Preview refresh failed for {file_id}: {e}")
        preview = None

    return jsonify({
        "rows": rows,
        "preview": preview,
        "redacted_filename": redacted_path.name,
        "dms_synced": dms_synced,
    })


@app.route("/api/redaction-map/<int:row_id>", methods=["DELETE"])
@swag_from("docs/swagger/redaction_map_delete.yml")
def api_redaction_map_delete(row_id: int):
    auth_err = _require_uid()
    if auth_err:
        return auth_err

    row = db.get_redaction_row(row_id)
    if not row:
        return jsonify({"error": "Unknown row"}), 404

    file_row = db.get_file(row["file_id"])
    if not file_row or not file_row.get("redacted_path"):
        return jsonify({"error": "Unknown file for this row"}), 404
    redacted_path = Path(file_row["redacted_path"])

    try:
        _restore_row(redacted_path, file_row, row)
    except Exception as e:
        print(f"[WARN] Restore failed for row {row_id}: {e}")
        return jsonify({"error": f"Restore failed: {e}"}), 500

    db.delete_redaction_row(row_id)

    rows = db.get_redaction_rows(row["file_id"])
    dms_synced = _sync_redacted_to_dms(row["file_id"], redacted_path, rows)
    try:
        preview = redaction_formats.render_preview(redacted_path, tokens=[r["redacted_text"] for r in rows])
    except Exception as e:
        print(f"[WARN] Preview refresh failed after delete for row {row_id}: {e}")
        preview = None

    return jsonify({
        "rows": rows,
        "preview": preview,
        "redacted_filename": redacted_path.name,
        "dms_synced": dms_synced,
    })


@app.route("/api/redaction-map/<int:row_id>", methods=["PUT"])
@swag_from("docs/swagger/redaction_map_edit.yml")
def api_redaction_map_edit(row_id: int):
    auth_err = _require_uid()
    if auth_err:
        return auth_err

    data = request.get_json(silent=True) or {}
    new_source_text = (data.get("source_text") or "").strip()
    new_category = (data.get("category") or "").strip()
    if not new_source_text or not new_category:
        return jsonify({"error": "source_text and category are required"}), 400

    row = db.get_redaction_row(row_id)
    if not row:
        return jsonify({"error": "Unknown row"}), 404
    if row["source"] != "manual":
        return jsonify({"error": "Only manually-added rows can be edited"}), 403

    file_id = row["file_id"]
    file_row = db.get_file(file_id)
    if not file_row or not file_row.get("redacted_path"):
        return jsonify({"error": "Unknown file for this row"}), 404
    redacted_path = Path(file_row["redacted_path"])

    other_rows = [r for r in db.get_redaction_rows(file_id) if r["id"] != row_id]
    for r in other_rows:
        if r["source_text"].strip().lower() == new_source_text.lower():
            return jsonify({"error": f"Already masked as {r['redacted_text']}"}), 409

    try:
        _restore_row(redacted_path, file_row, row)
    except Exception as e:
        print(f"[WARN] Restore-for-edit failed for row {row_id}: {e}")
        return jsonify({"error": f"Restore failed: {e}"}), 500

    token = redaction_formats.next_token_for_category(
        [r["redacted_text"] for r in other_rows], new_category
    )

    try:
        events = redaction_formats.mask_single_value(redacted_path, new_source_text, token)
    except Exception as e:
        print(f"[WARN] Re-mask failed for row {row_id}: {e}")
        return jsonify({"error": f"Masking failed: {e}"}), 500

    if not events:
        return jsonify({"error": _no_match_error(redacted_path) + " (the old value was already restored)"}), 400

    db.delete_redaction_row(row_id)
    db.insert_redaction_row(
        file_row["client_id"], file_row["job_id"], file_row["process_id"], file_id,
        new_source_text, token, source="manual",
    )

    rows = db.get_redaction_rows(file_id)
    dms_synced = _sync_redacted_to_dms(file_id, redacted_path, rows)
    try:
        preview = redaction_formats.render_preview(redacted_path, tokens=[r["redacted_text"] for r in rows])
    except Exception as e:
        print(f"[WARN] Preview refresh failed after edit for row {row_id}: {e}")
        preview = None

    return jsonify({
        "rows": rows,
        "preview": preview,
        "redacted_filename": redacted_path.name,
        "dms_synced": dms_synced,
    })


@app.route("/api/categories", methods=["GET"])
@swag_from("docs/swagger/categories_get.yml")
def api_categories():
    auth_err = _require_uid()
    if auth_err:
        return auth_err
    return jsonify(db.get_categories())


@app.route("/api/categories", methods=["POST"])
@swag_from("docs/swagger/categories_add.yml")
def api_categories_add():
    auth_err = _require_uid()
    if auth_err:
        return auth_err

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    try:
        normalized = db.add_category(name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({"name": normalized, "categories": db.get_categories()})


if __name__ == "__main__":
    print("PII Redaction UI -> http://127.0.0.1:5009")
    app.run(host="0.0.0.0", port=5009, debug=False, threaded=True)
