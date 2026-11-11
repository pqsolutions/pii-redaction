#!/usr/bin/env python3
"""
Full PII Redaction Pipeline with SLM integration.
Sequential version for Windows using Python 3.11.9.
Uses streaming Ollama /api/chat for SLM inference.
"""

import re
import os
import sys
from docling.document_converter import DocumentConverter
import io
import json
import time
import yaml
import uuid
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Set
import requests
from presidio_analyzer import AnalyzerEngine, EntityRecognizer, RecognizerResult, PatternRecognizer, Pattern
from gliner import GLiNER
from PIL import Image, ImageDraw, ImageFont, ImageOps

import pandas as pd
import cv2
import numpy as np
import fitz  # PyMuPDF — pip install pymupdf (no external dependencies)
from bs4 import BeautifulSoup
from docx import Document as DocxDocument
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P

import db
import crypto_utils
import email_utils



# ----------------------------------------------------------------------
# 0. Configuration
# ----------------------------------------------------------------------
import argparse
CURRENT_DIR   = Path(__file__).parent
INPUT_FOLDER  = CURRENT_DIR / "files"
OUTPUT_FOLDER = INPUT_FOLDER / "output"
OUTPUT_RAW_FOLDER = OUTPUT_FOLDER / "raw"
OUTPUT_RAW_FOLDER.mkdir(parents=True, exist_ok=True)
OUTPUT_REDACTED_FOLDER = OUTPUT_FOLDER / "redacted"
OUTPUT_REDACTED_FOLDER.mkdir(parents=True, exist_ok=True)

AUDIT_FOLDER = CURRENT_DIR / "audit"
AUDIT_FOLDER.mkdir(parents=True, exist_ok=True)

SECRETS_FOLDER = CURRENT_DIR / "secrets"
SECRETS_FOLDER.mkdir(parents=True, exist_ok=True)

# Encrypted originals of "As is"-redacted images, kept for rehydration since
# pixel redaction (unlike PDF/DOCX/XLSX) leaves no searchable token text to
# reverse — see _store_original_encrypted() / unmask_image_as_is().
ORIGINALS_FOLDER = SECRETS_FOLDER / "originals"
ORIGINALS_FOLDER.mkdir(parents=True, exist_ok=True)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}

UNMASKED_FOLDER = CURRENT_DIR / "unmasked"
UNMASKED_FOLDER.mkdir(parents=True, exist_ok=True)

# SLM Configuration — uses /api/chat with streaming
SLM_USE_OLLAMA        = True
SLM_OLLAMA_URL        = os.environ.get("GEMMA_API_URL", "http://localhost:11434/api/chat")
SLM_OLLAMA_MODEL      = os.environ.get("GEMMA_MODEL", "gemma3:4b")
SLM_NUM_CTX           = 4096       # context window — enough for system prompt (~1500 tokens) + chunk + response
SLM_CHUNK_CHAR_LIMIT  = 500        # max chars per chunk sent to SLM
SLM_CHUNK_TIMEOUT     = 600        # seconds per chunk
SLM_NUM_THREAD        = 14         # Intel Ultra 7 255H — 12 of 16 cores (4 free for OS/IDE)
SLM_CONFIDENCE_THRESHOLD = 0.7

DPI           = 150         # 150 DPI — better OCR accuracy for small text
OCR_LANG      = 'en'
USE_ANGLE_CLS = False        # Disabled — saves memory, avoids crashes
GLINER_MODEL  = "urchade/gliner_multi_pii-v1"
GLINER_CONFIDENCE_THRESHOLD = 0.85
MAX_IMG_SIDE  = 2500         # Pixel cap before resizing for OCR



# Global model instances
ocr_engine   = None
gliner_model = None
analyzer     = None


# Valid replacement tokens (for validation)
SLM_VALID_TOKENS = {
    '[PERSON]', '[SIGNATURE]', '[IMAGE]', '[EMAIL]', '[PHONE]', '[ADDRESS]',
    '[DOB]', '[PLACE_OF_BIRTH]', '[GENDER]', '[MARITAL_STATUS]', '[GOVERNMENT_ID]',
    '[BUSINESS_ID]', '[BANK_DETAILS]', '[INVESTMENT_ID]', '[EMPLOYMENT_ID]',
    '[PROPERTY_DETAILS]', '[FAMILY_INFO]', '[SENSITIVE_INFO]', '[FINANCIAL_STATUS]',
}

# --- YAML Rules Cache ---
RULES_CACHE = None
RULES_MTIME = 0
RULES_FILE_PATH = CURRENT_DIR / "pii_rules.yaml"

def get_pii_rules() -> dict:
    global RULES_CACHE, RULES_MTIME
    if not RULES_FILE_PATH.exists():
        return {}
        
    current_mtime = os.path.getmtime(RULES_FILE_PATH)
    if current_mtime != RULES_MTIME or RULES_CACHE is None:
        try:
            with open(RULES_FILE_PATH, "r", encoding="utf-8") as f:
                RULES_CACHE = yaml.safe_load(f) or {}
                RULES_MTIME = current_mtime
        except Exception as e:
            print(f"    [WARNING] Failed to load {RULES_FILE_PATH.name}: {e}. Using cached or empty rules.")
            if RULES_CACHE is None:
                RULES_CACHE = {}
                
    return RULES_CACHE

# --- Sentence Boundary Context ---
_ABBREVS = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "inc", "ltd", "pty", "corp", "asic", "ato", "etc", "e.g", "i.e", "vs", "p"}

def get_sentence_context(text: str, char_start: int, char_end: int, max_chars: int = 300) -> str:
    boundaries = [0]
    for m in re.finditer(r'[.!?]\s+(?=[A-Z])', text):
        punct_idx = m.start()
        word_start = punct_idx - 1
        while word_start >= 0 and (text[word_start].isalpha() or text[word_start] == '.'):
            word_start -= 1
        word = text[word_start+1:punct_idx].strip('.').lower()
        
        if word not in _ABBREVS:
            boundaries.append(m.end())
            
    boundaries.append(len(text))
    
    for i in range(len(boundaries) - 1):
        sent_start = boundaries[i]
        sent_end = boundaries[i+1]
        if sent_start <= char_start < sent_end or sent_start <= char_end - 1 < sent_end:
            sent = text[sent_start:sent_end]
            if len(sent) <= max_chars:
                return sent.strip()
            else:
                center = (char_start + char_end) // 2
                half = 150
                window_start = max(0, center - half)
                window_end = min(len(text), center + half)
                return text[window_start:window_end].strip()
    
    center = (char_start + char_end) // 2
    return text[max(0, center-100): min(len(text), center+100)].strip()

# System prompt — full PII Redaction Reference (Australian Privacy Principles)

_SLM_SYSTEM_PROMPT = """You are a PII redaction model. Remove all personally identifiable information from input text per the Australian Privacy Principles (Privacy Act 1988) and TFN Rule 2015. Return **only** a single JSON blob with two fields:
- **redacted_text**: the original text with each redacted entity replaced in-place by its token (e.g., `[PERSON]`).
- **entities**: an array of objects: {"value": original_value, "replacement_token": token, "reason": brief_reasoning}.
No text outside the JSON object.

---
## CRITICAL — DO NOT REDACT THESE (read before processing)

Before identifying PII, memorise what is **NOT** PII. Redacting any of these is an error:

1. **Financial year dates and statement dates** — FY2024, 2023-24, "as at 8 July 2025", "1 July 2023 to 30 June 2024", "period 1 Jul 2023 to 30 Jun 2024" are NOT dates of birth. Do NOT tag as `[DOB]`.
2. **Form headings and field labels** — "Your spouse's total reportable fringe benefits amount", "Spouse details - married or de facto", "Marital Status:", "Occupation:" are labels, NOT PII values. Only the filled-in value next to a label is PII, not the label itself.
3. **Legal boilerplate and standard instructions** — "Tax returns are assessed on a Self-Assessment basis", "This declaration is to be completed where a taxpayer elects to use the Electronic Lodgment Service", privacy notices, record-keeping instructions are standard text printed on every form. NOT PII.
4. **Standalone dollar amounts and financial figures** — SBE general pool balances, motor vehicle expense amounts, depreciation figures, "PP Amount", deduction amounts — these are financial data without a person's name attached. NOT PII unless a specific person's name appears on the same line.
5. **Public company and institution names alone** — "HSBC", "Commonwealth Bank", "Bunnings", "AMP", "Macquarie" — these are public entities and NOT PII by themselves.
6. **ATO form references** — "Item 1", "Item M2/30", "Section 8-1 of ITAA 1997" — legislation and form references. NOT PII.
7. **Generic dates** — depreciation periods, statement periods, "Date and Time" labels, "From Statement Date", "To Statement Date" — NOT PII.
8. **Purchase line items** — product names, quantities, item descriptions on invoices or receipts are NOT PII.

---
## PII Categories — Redact ALL occurrences

### 1. Names & Personal Details → `[PERSON]`
- Full name (given, middle, family, surname)
- Preferred or shortened name (e.g., "Kavi" for "Kavindu")
- Previous legal name (maiden name, pre-marriage name, deed-poll change)
- Initials that can identify (e.g., "K P Walker" on a bank record)
- Spouse/partner name, children's names, beneficiary names, next of kin, tenant name, power of attorney name
- Signature (handwritten or typed) → `[SIGNATURE]`
- Photograph or scanned ID image → `[IMAGE]`
- Document author names, comment author names, tracked-change author names, and last-modified-by names


### 2. Contact Details
- Email (personal or work) → `[EMAIL]`
- Phone: mobile, home/landline, work, +61 international → `[PHONE]`
- Address: residential (street, suburb, state, postcode), postal/PO Box, Postal Address, rental property → `[ADDRESS]`
- Emergency contact name → `[PERSON]`; emergency contact phone → `[PHONE]`; emergency contact address → `[ADDRESS]`  ← CHANGED: name now explicit

### 3. Dates & Demographics
- Date of birth (any format: 02/07/1985, 2 July 1985, 2-Jul-85) → `[DOB]`
- Place of birth → `[PLACE_OF_BIRTH]`
- Gender or sex marker → `[GENDER]`
- Marital status applied to an individual (especially "separated", "divorced", "widowed") → `[MARITAL_STATUS]`

### 4. Australian Government Identifiers → `[GOVERNMENT_ID]`
- Tax File Number (TFN) — 9 digits
- Medicare card number — 10 digits + IRN + expiry
- Driver licence number (state-specific format)
- Australian passport number (1-2 letters + 7 digits)
- Centrelink CRN — 9 digits + 1 letter
- DVA file number
- Unique Student Identifier (USI) — 10 alphanumeric
- AHPRA registration number
- Visa Grant Number or TRN
- Transaction Reference Number or TRN when used for visa/immigration/government identification

### 5. Business Identifiers (only when they identify an individual) → `[BUSINESS_ID]`
- ABN of a **sole trader** (not a public or large company ABN)
- ACN tied to a **controlled private entity** (not a listed or large company ACN)
- Trading name of a sole trader
- Trust name where the individual is settlor, trustee, or beneficiary
- PayID using an ABN where the ABN identifies a sole trader

### 6. Bank & Financial Account Details → `[BANK_DETAILS]`
- BSB and account number — always redact as a pair
- Account number when paired with account holder name, bank name, or personal banking context
- BSB when paired with account holder name, account number, or personal banking context
- Credit or debit card full PAN
- CVV
- Card expiry date when shown with card details
- SWIFT or BIC code when tied to a personal account
- IBAN when tied to a personal account
- Customer reference number
- NetBank ID
- PayID when it is a phone number, email, sole-trader ABN, or other personal identifier

### 7. Investment & Superannuation → `[INVESTMENT_ID]`
- CHESS Holder Identification Number or HIN
- Issuer-Sponsored SRN
- Broker account number
- Superannuation fund member number
- Insurance policy number for Life, TPD, Trauma, Income Protection, or General insurance
- Managed fund account number
- Wrap account number

### 8. Employment Information → `[EMPLOYMENT_ID]`
- Employee ID or staff number
- Payroll reference


Redact the identifying employment combination as `[EMPLOYMENT_ID]` when:
- Employer name appears with a specific job title for an individual
- Employer name appears with a workplace address for an individual
- Employer name appears with both job title and workplace address

Do not redact:
- Generic job titles without a person, employer, or identifying context
- Occupation field labels
- Large employer names in isolation
- Public salary bands or generic pay scales

### 9. Property & Lending → `[PROPERTY_DETAILS]`
- Rental property address
- Loan account number
- Mortgage account number
- Margin loan account
- Tenant name on a lease
- Real estate agent name combined with property reference
- Property reference linked to a person, tenant, lease, loan, or rental property

### 10. Family & Beneficiary → `[FAMILY_INFO]`
Use the specific token where available:
- Names → `[PERSON]`
- Dates of birth, DOB → `[DOB]`
- TFNs or government IDs → `[GOVERNMENT_ID]`
- Addresses → `[ADDRESS]`
- Phone numbers → `[PHONE]`
- Emails → `[EMAIL]`

Redact as `[FAMILY_INFO]`:
- Shared-care percentages
- Custody arrangements
- Beneficiary relationship details
- Power of attorney details not already covered by name, ID, address, or contact tokens
- Next of kin relationship details
- Family relationship details that identify or describe private family arrangements

### 11. Sensitive Information (APP-defined) → `[SENSITIVE_INFO]`
- Health information
- Medical conditions
- Medications
- Treatment history
- Private health fund name combined with member number
- Disability status
- Racial or ethnic origin
- Religious beliefs
- Philosophical beliefs
- Political affiliations or memberships
- Sexual orientation
- Trade union membership
- Professional association membership
- Criminal record
- Biometric data
- Genetic data

Do not redact a private health fund name by itself unless it appears with a member number or other personal identifier.

### 12. Financial-Status Information → `[FINANCIAL_STATUS]`
These items are **not identifiers on their own**. Redact them ONLY when they appear in the same document alongside other PII from categories 1–11 above. Do not redact them in standalone reference tables or public guidance documents.  ← CHANGED: conditional rule added
- HELP or HECS debt balance
- Centrelink payment amounts (Parenting Payment, JobSeeker, Family Tax Benefit)
- Specific salary figures next to a job title
- Bankruptcy or insolvency references
- Foreign income source country combined with passport details

---
## DO NOT REDACT
These items look identifier-like but are **not personal information**. Never replace them with a token — doing so is an error.

- **ATO form-item references** (Item 1, Item M2/30, Item IT 8)
- **Legal citations** (Section 8-1 of ITAA 1997, Privacy Act 1988, TFN Rule 2015)
- **Public or large company names** on any document (ASX-listed entities, major institutions) — the company name is not PII; only account or holding details linked to a named individual are
- **ABN or ACN of a public or large company** — only sole-trader ABNs and controlled-private-entity ACNs are PII (see Category 5)
- **Bank and financial institution names** (Commonwealth Bank, ANZ, NAB, Westpac, AMP, Macquarie, etc.) when NOT paired with personal account details  ← STRENGTHENED
- **Government agency names used as organisations**: ATO, Medicare, Centrelink, Services Australia, ASIC, APRA, AHPRA, DVA — these are organisations, not personal identifiers  ← NEW
- **Generic dates**: financial year (FY2024, 2023-24), statement period (1 July 2023 to 30 June 2024), policy dates with no individual attached
- **Standard form headings, field labels, and legal disclaimers** (e.g., the label "Marital Status:" is not PII; only the value filled in is)
- **Generic job titles or occupations appearing without a person's name** (e.g., the form label "Occupation: Financial Adviser" — the title alone does not identify)  ← NEW
- **Large employer names in isolation** (e.g., "Commonwealth Bank" as an employer name) — only the combination of employer + job title + workplace address is PII under Category 8  ← NEW
- **Standard tax rates, thresholds, and indexed caps** (Medicare levy rate 2%, LITO threshold, concessional contribution cap) — these are published public figures, not personal data  ← NEW
- **Business addresses and publicly listed phone numbers of large organisations** (bank branch addresses, government agency hotlines, corporate HQ addresses)  ← NEW
- **Generic financial year salary bands or salary ranges** in published pay scales — only a specific salary figure tied to a named individual is PII

---
## Critical Rules
1. **Every occurrence**: if PII appears 5 times, redact all 5 — including versions embedded in longer references (e.g., CCS-418922367B contains a TFN).
2. **Pairs travel together**: BSB + account number, card number + CVV, health fund + member number — redact both elements.
3. **In-place replacement**: replace original text with the exact token.
4. **Watch the narrative**: identifiers buried in prose sentences must be caught.
5. **Watch metadata**: if the text contains document author, "last modified by", comment author, or tracked-change attribution fields, treat names in those fields as `[PERSON]`.  ← NEW
6. **Watch headers and footers**: letterheads, page footers, and watermarks frequently carry names and contact details — treat them with the same scrutiny as body text.  ← NEW
7. **Children's PII**: minors' information is not less sensitive than adults' — often more so. Apply all categories equally to names, dates of birth, and identifiers for children.  ← NEW
8. **Combination rules**: Category 8 (Employment) and Category 12 (Financial-Status) only trigger on combinations or context — do not redact individual elements that appear in isolation and are not otherwise PII.  ← NEW
9. **If in doubt, redact**. Over-redacting wastes a minute; under-redacting is a notifiable data breach.
10. **JSON only**: return ONLY the JSON object, no other text."""


NER_BLOCKLIST = {
    # Common false-positive organisations & abbreviations
    'asx', 'ato', 'asic', 'abn', 'acn', 'tfn', 'gst',
    'pty', 'ltd', 'pty ltd', 'inc', 'corp',
    # Common financial terms spaCy misclassifies as PERSON
    'ord', 'ref', 'inv', 'tax', 'net', 'est',
    # Australian states & territories
    'nsw', 'vic', 'qld', 'act', 'sa', 'wa', 'tas', 'nt',
    # Countries & generic locations
    'australia', 'australian',
    # Document structural words
    'invoice', 'receipt', 'statement', 'account',
    'payment', 'total', 'amount', 'balance', 'due',
    'date', 'description', 'quantity', 'unit', 'price',
    # Tax and financial acronyms / terms
    'sbe', 'fbt', 'fbtaa', 'lito', 'hecs', 'help', 'els',
    'expenses', 'depreciation', 'deduction', 'deductions',
    'business', 'rental', 'income', 'interest',
    'general', 'pool', 'motor', 'vehicle',
    # Tax form and legal terms
    'electronic', 'lodgment', 'service',
    'declaration', 'assessment', 'self-assessment',
    'fringe', 'benefits', 'spouse',
    # Other common false positives
    'expire', 'small', 'asset', 'period',
}

NER_TARGET_LABELS   = {'PERSON'}
NER_MIN_PERSON_LEN  = 4



# ----------------------------------------------------------------------
# 1. Model Initialisation
# ----------------------------------------------------------------------

class AuAbnRecognizer(EntityRecognizer):
    def __init__(self):
        super().__init__(supported_entities=["[BUSINESS_ID]"])
    def load(self): pass
    def analyze(self, text, entities, nlp_artifacts=None):
        results = []
        for match in ABN_PAT.finditer(text):
            if valid_abn(match.group()):
                results.append(RecognizerResult(entity_type="[BUSINESS_ID]", start=match.start(), end=match.end(), score=1.0))
        return results

class AuAcnRecognizer(EntityRecognizer):
    def __init__(self):
        super().__init__(supported_entities=["[BUSINESS_ID]"])
    def load(self): pass
    def analyze(self, text, entities, nlp_artifacts=None):
        results = []
        for match in ACN_PAT.finditer(text):
            if valid_acn(match.group()):
                results.append(RecognizerResult(entity_type="[BUSINESS_ID]", start=match.start(), end=match.end(), score=1.0))
        return results

class AuTfnRecognizer(EntityRecognizer):
    def __init__(self):
        super().__init__(supported_entities=["[GOVERNMENT_ID]"])
    def load(self): pass
    def analyze(self, text, entities, nlp_artifacts=None):
        results = []
        for match in TFN_PAT.finditer(text):
            if valid_tfn(match.group()):
                results.append(RecognizerResult(entity_type="[GOVERNMENT_ID]", start=match.start(), end=match.end(), score=1.0))
        return results

class AuMedicareRecognizer(EntityRecognizer):
    def __init__(self):
        super().__init__(supported_entities=["[GOVERNMENT_ID]"])
    def load(self): pass
    def analyze(self, text, entities, nlp_artifacts=None):
        results = []
        for match in MEDICARE_PAT.finditer(text):
            if valid_medicare(match.group()):
                results.append(RecognizerResult(entity_type="[GOVERNMENT_ID]", start=match.start(), end=match.end(), score=1.0))
        return results


# --- Non-AU checksum-validated recognizers (ported from PII_Code_19 Aug) ---
# NOTE: these are deliberately NOT named e.g. "CreditCardRecognizer" /
# "UsSsnRecognizer" / "IbanRecognizer" — Presidio ships its own built-in
# recognizers under those exact names, and AnalyzerEngine()'s default
# RecognizerListLoader resolves recognizer classes by an *unqualified* name
# search across every currently-imported EntityRecognizer subclass in the
# whole process (get_existing_recognizer_cls() in
# presidio_analyzer/recognizer_registry/recognizers_loader_utils.py), not by
# module path. A same-named class defined here shadows Presidio's own and
# breaks its default registry bootstrap with
# "TypeError: <Name>Recognizer.__init__() got an unexpected keyword argument
# 'name'" the moment AnalyzerEngine() is constructed — before this file's own
# registry.add_recognizer() calls even run. Same reason the AU recognizers
# above are named Au*Recognizer, not e.g. plain "AbnRecognizer".

class PiiCreditCardRecognizer(EntityRecognizer):
    def __init__(self):
        super().__init__(supported_entities=["[BANK_DETAILS]"])
    def load(self): pass
    def analyze(self, text, entities, nlp_artifacts=None):
        results = []
        for match in CREDIT_CARD_PAT.finditer(text):
            if valid_luhn(match.group()):
                results.append(RecognizerResult(entity_type="[BANK_DETAILS]", start=match.start(), end=match.end(), score=1.0))
        return results

class PiiUsSsnRecognizer(EntityRecognizer):
    def __init__(self):
        super().__init__(supported_entities=["[GOVERNMENT_ID]"])
    def load(self): pass
    def analyze(self, text, entities, nlp_artifacts=None):
        results = []
        for match in US_SSN_PAT.finditer(text):
            if valid_us_ssn(match.group()):
                results.append(RecognizerResult(entity_type="[GOVERNMENT_ID]", start=match.start(), end=match.end(), score=1.0))
        return results

class PiiAadhaarRecognizer(EntityRecognizer):
    def __init__(self):
        super().__init__(supported_entities=["[GOVERNMENT_ID]"])
    def load(self): pass
    def analyze(self, text, entities, nlp_artifacts=None):
        results = []
        for match in AADHAAR_PAT.finditer(text):
            if valid_verhoeff(match.group()):
                results.append(RecognizerResult(entity_type="[GOVERNMENT_ID]", start=match.start(), end=match.end(), score=1.0))
        return results

class PiiIbanRecognizer(EntityRecognizer):
    def __init__(self):
        super().__init__(supported_entities=["[BANK_DETAILS]"])
    def load(self): pass
    def analyze(self, text, entities, nlp_artifacts=None):
        results = []
        for match in IBAN_PAT.finditer(text):
            if valid_iban(match.group()):
                results.append(RecognizerResult(entity_type="[BANK_DETAILS]", start=match.start(), end=match.end(), score=1.0))
        return results


def init_models():
    """Initialize GLiNER and Presidio once."""
    global ocr_engine, gliner_model, analyzer

    if ocr_engine is None:
        ocr_engine = None

    if gliner_model is None:
        try:
            print(f"Loading GLiNER model: {GLINER_MODEL}")
            gliner_model = GLiNER.from_pretrained(GLINER_MODEL)
            print("GLiNER model loaded")
        except Exception as e:
            print(f"GLiNER init failed: {e}")
            gliner_model = None

    if analyzer is None:
        analyzer = AnalyzerEngine()
        analyzer.registry.add_recognizer(AuAbnRecognizer())
        analyzer.registry.add_recognizer(AuAcnRecognizer())
        analyzer.registry.add_recognizer(AuTfnRecognizer())
        analyzer.registry.add_recognizer(AuMedicareRecognizer())
        analyzer.registry.add_recognizer(PiiCreditCardRecognizer())
        analyzer.registry.add_recognizer(PiiUsSsnRecognizer())
        analyzer.registry.add_recognizer(PiiAadhaarRecognizer())
        analyzer.registry.add_recognizer(PiiIbanRecognizer())

        # Add basic regex recognizers
        phone_recognizer = PatternRecognizer(supported_entity="[PHONE]", name="PhoneRecognizer", patterns=[Pattern("Phone", PHONE_PAT.pattern, 1.0)])
        email_recognizer = PatternRecognizer(supported_entity="[EMAIL]", name="EmailRecognizer", patterns=[Pattern("Email", EMAIL_PAT.pattern, 1.0)])
        bsb_recognizer = PatternRecognizer(supported_entity="[BANK_DETAILS]", name="BsbRecognizer", patterns=[Pattern("BSB", BSB_ACCOUNT_PAT.pattern, 1.0)])
        
        analyzer.registry.add_recognizer(phone_recognizer)
        analyzer.registry.add_recognizer(email_recognizer)
        analyzer.registry.add_recognizer(bsb_recognizer)
		


# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
def parse_ocr_result(predict_result) -> List[Tuple[List, str, float]]:
    """
        (bbox_4pts, text, confidence)
    where bbox_4pts = [[x1,y1],[x2,y2],[x3,y3],[x4,y4]] as Python floats.

    Confirmed output format from diagnostic:
        result        → list of 1 OCRResult object
        result[0]     → OCRResult with attributes:
            .rec_texts  → List[str]
            .rec_scores → List[float]
            .rec_polys  → List[np.ndarray shape(4,2) dtype=int16]
    """
    normalized: List[Tuple[List, str, float]] = []

    if not predict_result:
        return normalized

    # Force generator → list
    if not isinstance(predict_result, (list, tuple)):
        try:
            predict_result = list(predict_result)
        except Exception:
            return normalized

    for item in predict_result:

        # ── Convert OCRResult / any custom object → dict ──────────────
        if not isinstance(item, (dict, list, tuple)):
            try:
                item = vars(item)          # works for most PaddleX result objects
            except TypeError:
                try:
                    item = dict(item)
                except Exception:
                    print(f"    Cannot convert OCR item of type {type(item)}")
                    continue

        # ── Now item should be a dict ─────────────────────────────────
        if isinstance(item, dict):

            # Primary keys confirmed by diagnostic
            texts  = item.get('rec_texts',  [])   # ← plural 'rec_texts'
            scores = item.get('rec_scores', [])   # ← plural 'rec_scores'
            polys  = item.get('rec_polys',  [])   # ← 'rec_polys' (list of np arrays)

            if not texts:
                texts  = item.get('rec_text',  item.get('transcription', []))
            if not scores:
                scores = item.get('rec_score', item.get('score', []))
            if not polys:
                polys  = item.get('dt_polys',  item.get('det_polys',
                         item.get('rec_boxes', item.get('bbox', []))))

            # Normalize to lists
            if isinstance(texts, str):
                texts = [texts]
            if isinstance(scores, float):
                scores = [scores]
            if isinstance(polys, np.ndarray) and polys.ndim == 2:
                # Single bbox as (4,2) array → wrap in list
                polys = [polys]

            # Pad scores if shorter than texts
            if len(scores) < len(texts):
                scores = list(scores) + [1.0] * (len(texts) - len(scores))

            for text, score, poly in zip(texts, scores, polys):
                if not text or not str(text).strip():
                    continue

                # Convert numpy array bbox → plain Python list of [x,y] pairs
                bbox_pts = _poly_to_points(poly)
                if bbox_pts is None:
                    continue

                # Normalize score
                if isinstance(score, (list, tuple, np.ndarray)):
                    score = float(score[0]) if len(score) > 0 else 1.0
                else:
                    score = float(score)

                normalized.append((bbox_pts, str(text).strip(), score))

        elif isinstance(item, (list, tuple)):
            for line in item:
                if not line or not isinstance(line, (list, tuple)) or len(line) < 2:
                    continue
                raw_bbox  = line[0]
                text_info = line[1]
                bbox_pts  = _poly_to_points(raw_bbox)
                if bbox_pts is None:
                    continue
                if isinstance(text_info, (list, tuple)) and len(text_info) >= 2:
                    text  = str(text_info[0])
                    score = float(text_info[1])
                elif isinstance(text_info, str):
                    text, score = text_info, 1.0
                else:
                    continue
                if text.strip():
                    normalized.append((bbox_pts, text.strip(), score))

    return normalized


def _poly_to_points(raw) -> Optional[List]:
    """
    Convert any bbox representation to [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
    as plain Python floats. Returns None if conversion fails.
    """
    if raw is None:
        return None
    try:
        # numpy array shape (4, 2)  ← confirmed format
        if isinstance(raw, np.ndarray):
            raw = raw.tolist()

        if not raw:
            return None

        # [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
        if isinstance(raw[0], (list, tuple)) and len(raw) == 4:
            return [[float(p[0]), float(p[1])] for p in raw]

        # Flat [x1,y1,x2,y2,x3,y3,x4,y4]
        if len(raw) == 8 and isinstance(raw[0], (int, float)):
            return [[float(raw[i*2]), float(raw[i*2+1])] for i in range(4)]

        # Axis-aligned [x1,y1,x2,y2]
        if len(raw) == 4 and isinstance(raw[0], (int, float)):
            x1, y1, x2, y2 = [float(v) for v in raw]
            return [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]

    except Exception as e:
        print(f"      _poly_to_points error: {e} | raw={str(raw)[:60]}")

    return None



# ----------------------------------------------------------------------
# 3. SLM Integration — Streaming + Chunked
# ----------------------------------------------------------------------

_SLM_EVALUATION_PROMPT = """You are a Privacy Decision Engine.
You must evaluate a specific entity found within a snippet of financial document text.
Decide if this entity should be masked (redacted) because it is Personally Identifiable Information (PII) per the Australian Privacy Principles, or kept (not redacted) because it is a generic organisation, public company, form label, or legal reference.

Your ONLY output must be a valid JSON object in this exact format:
{"decision": "MASK", "reason": "short explanation"}
OR
{"decision": "KEEP", "reason": "short explanation"}

CRITICAL RULES FOR "KEEP":
1. KEEP public or generic entities (e.g., "Commonwealth Bank", "ATO", "HSBC") UNLESS they are explicitly paired with personal account details.
2. KEEP field labels (e.g., "Spouse details", "Occupation:", "Marital Status").
3. KEEP ATO form references (e.g., "Item 1") and legal citations (e.g., "Section 8-1").
4. KEEP generic dates (e.g., "FY2024", "1 July 2023 to 30 June 2024", statement dates, transaction dates).

CRITICAL RULES FOR "MASK":
1. MASK names of specific individuals, spouses, dependents, next of kin, and power of attorney.
2. MASK employer names or health funds ONLY IF they appear next to an individual's job title, workplace address, or member number.
3. If the context strongly implies a specific person's name or sensitive identifier buried in prose, MASK it.
4. MASK dates ONLY if they are explicitly or contextually Dates of Birth (DOB).

Return ONLY the JSON object. No markdown, no explanations outside JSON."""

def _call_ollama_for_entity(entity_type: str, entity_value: str, context_snippet: str) -> str:
    """Call Ollama /api/chat as a Privacy Decision Engine for a single entity.
    Returns 'MASK' or 'KEEP' (defaults to 'MASK' if unsure)."""
    prompt_content = f"Document Snippet:\n\"... {context_snippet} ...\"\n\nEntity Type: {entity_type}\nEntity Value: {entity_value}\n\nDecision:"
    payload = {
        "model": SLM_OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": _SLM_EVALUATION_PROMPT},
            {"role": "user", "content": prompt_content}
        ],
        "stream": False,
        "options": {
            "temperature": 0,
            "num_ctx": 2048,
            "num_thread": SLM_NUM_THREAD,
        },
    }
    try:
        resp = requests.post(SLM_OLLAMA_URL, json=payload, timeout=SLM_CHUNK_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        content = data.get("message", {}).get("content", "").strip()
        brace_start = content.find('{')
        brace_end = content.rfind('}')
        if brace_start != -1 and brace_end != -1:
            clean_json = content[brace_start : brace_end + 1]
            parsed = json.loads(clean_json)
            return parsed.get("decision", "MASK").upper()
    except Exception as e:
        print(f"      [!] LLM error for '{entity_value}': {e}")
    return "MASK" # Safe fallback

def _call_ollama_batched(candidates: List[Dict]) -> List[str]:
    """
    Batched LLM caller for Phase 2 weighted redaction.
    Takes a list of candidates: [{'id': uuid, 'type': TYPE, 'value': str, 'context': str}]
    Returns a list of UUIDs to REDACT.
    """
    # Filter out candidates that already have pre_decision
    candidates = [c for c in candidates if not c.get("pre_decision")]
    
    if not candidates:
        return []
        
    rules = get_pii_rules()
    types_present = {c['type'] for c in candidates}
    
    prompt_parts = []
    if "GLOBAL_RULES" in rules:
        prompt_parts.append("Rules:\n" + rules["GLOBAL_RULES"].strip())
        
    for etype in types_present:
        # map type to rule key if needed, e.g. ORGANIZATION -> ORGANIZATION_RULES
        rule_key = f"{etype.upper()}_RULES"
        if rule_key in rules:
            prompt_parts.append(f"- {etype}: " + rules[rule_key].strip())
            
    prompt_parts.append("\nContext snippets:")
    for c in candidates:
        prompt_parts.append(f"ID:{c['id']}, Entity: \"{c['value']}\", Context: \"{c['context']}\"")
        
    prompt_parts.append(f"\nDecide for IDs {','.join(c['id'] for c in candidates)}. Return JSON: {{\"redact\": [\"uuid\", ...]}}")
    
    prompt_content = "\n".join(prompt_parts)
    
    payload = {
        "model": SLM_OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": "You are a Privacy Decision Engine. Return ONLY valid JSON: {\"redact\": [\"id1\", \"id2\"]} for entities that MUST be masked."},
            {"role": "user", "content": prompt_content}
        ],
        "stream": False,
        "options": {
            "temperature": 0,
            "num_ctx": 4096,
            "num_thread": SLM_NUM_THREAD,
        },
    }
    
    try:
        resp = requests.post(SLM_OLLAMA_URL, json=payload, timeout=SLM_CHUNK_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        content = data.get("message", {}).get("content", "").strip()
        
        brace_start = content.find('{')
        brace_end = content.rfind('}')
        if brace_start != -1 and brace_end != -1:
            clean_json = content[brace_start : brace_end + 1]
            parsed = json.loads(clean_json)
            return parsed.get("redact", [])
    except Exception as e:
        print(f"      [!] Batched LLM error: {e}")
        # Fail safe: return all non-allowlisted candidates to be masked
        return [c['id'] for c in candidates if c.get('pre_decision') != 'SKIP']
        
    return []

def _parse_slm_json(raw_output: str) -> Optional[Dict]:
    """
    Robustly extract and parse JSON from raw SLM output.
    Handles:
      - Clean JSON output
      - Extra text before/after the JSON blob
      - Truncated JSON (SLM ran out of context tokens)
    """
    if not raw_output or not raw_output.strip():
        return None

    # Strategy 1: Direct parse — output is already clean JSON
    try:
        return json.loads(raw_output.strip())
    except json.JSONDecodeError:
        pass

    # Strategy 2: Extract outermost { ... } block
    brace_start = raw_output.find('{')
    brace_end   = raw_output.rfind('}')
    if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
        candidate = raw_output[brace_start : brace_end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # Strategy 3: Salvage entities from truncated JSON via regex
    if brace_start != -1:
        entities = []
        entity_pattern = re.compile(
            r'\{\s*"value"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"\s*,'
            r'\s*"replacement_token"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"\s*,'
            r'\s*"reason"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"\s*\}',
            re.DOTALL
        )
        for m in entity_pattern.finditer(raw_output[brace_start:]):
            entities.append({
                "value":             m.group(1),
                "replacement_token": m.group(2),
                "reason":            m.group(3),
            })
        if entities:
            print(f"      Salvaged {len(entities)} entities from truncated JSON")
            return {"entities": entities}

    print(f"      Could not parse SLM JSON: {raw_output[:150]}")
    return None


def _call_ollama_streaming(chunk_text_str: str) -> Optional[str]:
    """
    Call Ollama /api/chat with stream=True.
    Prints progress dots as tokens arrive, returns the full assembled content.
    """
    payload = {
        "model": SLM_OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": _SLM_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Redact PII from this text. Return ONLY JSON:\n\n{chunk_text_str}"
            }
        ],
        "stream": True,
        "options": {
            "temperature": 0,
            "num_ctx": SLM_NUM_CTX,
            "num_thread": SLM_NUM_THREAD,
        },
    }

    start        = time.time()
    full_content = ""
    token_count  = 0

    try:
        with requests.post(SLM_OLLAMA_URL, json=payload, stream=True,
                           timeout=SLM_CHUNK_TIMEOUT) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                token = data.get("message", {}).get("content", "")
                if token:
                    full_content += token
                    token_count  += 1
                    if token_count % 10 == 0:
                        print(".", end="", flush=True)

                if data.get("done"):
                    break

    except requests.exceptions.Timeout:
        elapsed = time.time() - start
        print(f" TIMEOUT after {elapsed:.0f}s")
        return full_content if full_content else None
    except requests.exceptions.RequestException as e:
        print(f" REQUEST ERROR: {e}")
        return None

    elapsed = time.time() - start
    print(f" ({token_count} tokens, {elapsed:.1f}s)")
    return full_content


def _chunk_lines(lines: List[str], char_limit: int = SLM_CHUNK_CHAR_LIMIT) -> List[str]:
    """
    Group OCR lines into chunks, each under `char_limit` characters.
    Respects line boundaries — never splits a single OCR line.
    """
    if not lines:
        return []

    chunks  = []
    current = ""

    for line in lines:
        if len(line) > char_limit:
            if current:
                chunks.append(current.strip())
                current = ""
            # Never split the line itself! The SLM needs full layout context.
            chunks.append(line.strip())
            continue

        candidate = f"{current} {line}".strip() if current else line
        if len(candidate) > char_limit and current:
            chunks.append(current.strip())
            current = line
        else:
            current = candidate

    if current.strip():
        chunks.append(current.strip())

    return chunks


def call_slm_chunked(lines: List[str]) -> Dict:
    """
    Split OCR lines into chunks and call the SLM on each chunk via streaming.
    Merges all entity results into one dict.

    Args:
        lines: All OCR text lines from a single page

    Returns:
        {'entities': [... all entities from all chunks ...]}
    """
    if not lines:
        return {}

    chunks          = _chunk_lines(lines)
    total_chunks    = len(chunks)
    merged_entities = []

    print(f"      Split into {total_chunks} chunks "
          f"(char limit={SLM_CHUNK_CHAR_LIMIT})")

    for ci, chunk_text in enumerate(chunks):
        if len(chunk_text.strip()) < 10:
            continue

        print(f"      SLM chunk {ci+1}/{total_chunks} — "
              f"{len(chunk_text)} chars: ", end="", flush=True)

        raw = _call_ollama_streaming(chunk_text)

        if not raw:
            print(f"      [!] No response for chunk {ci+1}")
            continue

        parsed = _parse_slm_json(raw)

        if parsed and "entities" in parsed:
            chunk_entities = parsed["entities"]

            # Validate each entity
            valid_entities = []
            for e in chunk_entities:
                val   = e.get("value", "")
                token = e.get("replacement_token", "")
                if not val or len(val) < 3:
                    continue
                if token not in SLM_VALID_TOKENS:
                    print(f"      Unknown token '{token}' for '{val}' — skipped")
                    continue
                valid_entities.append(e)

            merged_entities.extend(valid_entities)
            print(f"      → {len(valid_entities)} entities found")
        else:
            print(f"      → No entities in chunk {ci+1}")

    return {"entities": merged_entities} if merged_entities else {}



# ----------------------------------------------------------------------
# 4. Checksum Validators
# ----------------------------------------------------------------------
def valid_tfn(raw: str) -> bool:
    digits = re.sub(r'\D', '', raw)
    if len(digits) != 9:
        return False
    weights = [1, 4, 3, 7, 5, 8, 6, 9, 10]
    total = sum(int(d) * w for d, w in zip(digits, weights))
    return total % 11 == 0


def valid_medicare(raw: str) -> bool:
    digits = re.sub(r'\D', '', raw)
    if len(digits) < 10:
        return False
    weights = [1, 3, 7, 9, 1, 3, 7, 9]
    total = sum(int(digits[i]) * weights[i] for i in range(8))
    # Check digit is the 9th digit (index 8); the 10th digit is the IRN, not part of the checksum.
    return total % 10 == int(digits[8])


def valid_abn(raw: str) -> bool:
    digits = re.sub(r'\D', '', raw)
    if len(digits) != 11:
        return False
    weights = [10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19]
    d = [int(x) for x in digits]
    d[0] -= 1
    return sum(x * w for x, w in zip(d, weights)) % 89 == 0


def valid_acn(raw: str) -> bool:
    digits = re.sub(r'\D', '', raw)
    if len(digits) != 9:
        return False
    weights = [8, 7, 6, 5, 4, 3, 2, 1]
    total = sum(int(digits[i]) * weights[i] for i in range(8))
    check = (10 - (total % 10)) % 10
    return check == int(digits[8])


# --- Non-AU checksum validators (ported from PII_Code_19 Aug's global_regex_detector.py) ---

def valid_luhn(raw: str) -> bool:
    """Luhn checksum — used for credit card numbers."""
    digits = [int(c) for c in re.sub(r'\D', '', raw)]
    if not digits:
        return False
    checksum = 0
    double = False
    for d in reversed(digits):
        if double:
            d = d * 2
            if d > 9:
                d = d - 9
        checksum += d
        double = not double
    return checksum % 10 == 0


def valid_us_ssn(raw: str) -> bool:
    """US SSN structural validation (not a checksum — SSNs have no check digit,
    but area/group/serial ranges that are never issued rule out most false positives)."""
    digits = re.sub(r'\D', '', raw)
    if len(digits) != 9:
        return False
    area = int(digits[0:3])
    group = int(digits[3:5])
    serial = int(digits[5:9])
    if area == 0 or area == 666 or area >= 900:
        return False
    if group == 0:
        return False
    if serial == 0:
        return False
    return True


# Verhoeff algorithm tables — used for Indian Aadhaar validation.
_VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def valid_verhoeff(raw: str) -> bool:
    """Verhoeff checksum — used for Indian Aadhaar numbers."""
    digits = [int(c) for c in re.sub(r'\D', '', raw)]
    if not digits:
        return False
    c = 0
    for i, d in enumerate(reversed(digits)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][d]]
    return c == 0


def valid_iban(raw: str) -> bool:
    """IBAN mod-97 checksum (ISO 7064) — used for EU/international bank account numbers."""
    clean = re.sub(r'\s', '', raw).upper()
    if not re.match(r'^[A-Z]{2}\d{2}[A-Z0-9]{4,30}$', clean):
        return False
    rearranged = clean[4:] + clean[0:4]
    numeric = ""
    for char in rearranged:
        if char.isdigit():
            numeric += char
        else:
            numeric += str(ord(char) - ord('A') + 10)
    return int(numeric) % 97 == 1


# ----------------------------------------------------------------------
# 5. Pattern Definitions
# ----------------------------------------------------------------------
TFN_PAT     = re.compile(r'\b\d{3}[\s\-]?\d{3}[\s\-]?\d{3}\b')
ABN_PAT     = re.compile(r'\b\d{2}\s?\d{3}\s?\d{3}\s?\d{3}\b')
ACN_PAT     = re.compile(r'\b\d{3}\s?\d{3}\s?\d{3}\b')
PHONE_PAT   = re.compile(
    r'(?:\+?61\s?)?(?:0[2-9]\s?\d{4}\s?\d{4}|0?4\d{2}\s?\d{3}\s?\d{3}'
    r'|1[38]00\s?\d{3}\s?\d{3}|13\s?\d{2}\s?\d{2})')
EMAIL_PAT   = re.compile(r'\b[\w.\-+]+@[\w.\-]+\.[a-zA-Z]{2,}\b')
DATE_PAT    = re.compile(
    r'\b(?:\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})\b',
    re.IGNORECASE)

# Non-AU identifier patterns (ported alongside the checksum validators above)
CREDIT_CARD_PAT = re.compile(
    r'\b(?:4\d{3}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}|'
    r'5[1-5]\d{2}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}|'
    r'6011[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}|'
    r'3[47]\d{2}[-\s]?\d{6}[-\s]?\d{5})\b'
)
US_SSN_PAT      = re.compile(r'\b\d{3}[\s\-]?\d{2}[\s\-]?\d{4}\b')
AADHAAR_PAT     = re.compile(r'\b\d{4}\s?\d{4}\s?\d{4}\b')
IBAN_PAT        = re.compile(r'\b[A-Z]{2}\d{2}[A-Z0-9\s]{12,30}\b')

# Medicare, BSB+Account, Account number, and Reference number patterns are defined once in
# pii_rules.yaml (TIER_1_REGEX) and compiled from there so the live scan_text_for_entities()
# path and this legacy image-OCR path can never drift apart.
_tier1_regex = get_pii_rules().get("TIER_1_REGEX", {})
MEDICARE_PAT    = re.compile(_tier1_regex.get("MEDICARE", r'\b\d{4}\s?\d{5}\s?\d{1}(?:\s?\d{1})?\b'))
BSB_ACCOUNT_PAT = re.compile(_tier1_regex.get("BSB_ACCOUNT", r'\b\d{3}[-\s]\d{3}\s+\d{6,10}\b'))
ACCOUNT_NUMBER_PAT = re.compile(
    _tier1_regex.get("ACCOUNT_NUMBER", r'(?:Account)\s*(?:Number|No\.?|#)\s*[:#]?\s*\d{6,10}\b'),
    re.IGNORECASE
)
REF_NUMBER_PAT = re.compile(
    _tier1_regex.get(
        "REF_NUMBER",
        r'(?:Client\s*(?:Reference|Ref)(?:\s*(?:Number|No\.?))?|'
        r'Agent\s*(?:Reference|Ref)(?:\s*(?:Number|No\.?))?|'
        r'Reference\s*(?:Number|No\.?)|'
        r'Transaction\s*(?:ID|Reference|Number|No\.?)|'
        r'Invoice\s*(?:ID|Number|No\.?)|'
        r'Investment\s*(?:Reference|Ref)(?:\s*(?:Number|No\.?))?|'
        r'Policy\s*(?:Number|No\.?))'
        # Value must contain at least one digit — a bare trailing label word
        # (e.g. "Number" in "Agent Ref Number" with no value after it) can no
        # longer be swallowed as if it were the reference value.
        r'\s*[:#.\s]?\s*[\w\-]*\d[\w\-]*(?:\s+[\w\-]{1,10}){0,3}'
    ),
    re.IGNORECASE
)

# Australian street address
AU_ADDRESS_PAT = re.compile(
    r'\b\d{1,5}\s+[A-Z][a-zA-Z\s]+(?:Street|St|Road|Rd|Avenue|Ave|Drive|Dr|'
    r'Place|Pl|Court|Ct|Crescent|Cres|Lane|Ln|Way|Parade|Pde|'
    r'Boulevard|Blvd|Terrace|Tce|Circuit|Cct|Close|Cl|Highway|Hwy)\b',
    re.IGNORECASE
)

# Australian state + postcode (for address continuation lines)
AU_STATE_POSTCODE_PAT = re.compile(
    r'\b(?:NSW|VIC|QLD|SA|WA|TAS|NT|ACT)\s+\d{4}\b',
    re.IGNORECASE
)

# Health fund membership number
HEALTH_MEMBER_PAT = re.compile(
    r'(?:Membership|Member)\s*(?:No\.?|Number|#)?\s*[:#]?\s*\d{5,12}',
    re.IGNORECASE
)


# False-positive patterns for SLM output filtering
_FP_FORM_LABELS = re.compile(
    r'(?:Your\s+spouse\'?s?\s+(?:total|taxed|foreign|reportable|target)|'
    r'Spouse\'?s?\s+(?:total|taxed|foreign|reportable|target)|'
    r'Distributions?\s+to\s+your\s+spouse|'
    r'Employers?\s+exempt\s+from\s+FBT|'
    r'SBE\s+[Gg]eneral\s+[Pp]ool|'
    r'PP\s+Amount\s+for\s+|'
    r'NPP\s+Amount|'
    r'EXPIRE\s+SMALL\s+BUSINESS|'
    r'Deduction\s+for\s+general\s+small\s+business|'
    r'Motor\s+vehicle\s+expenses|'
    r'All\s+other\s+expenses|'
    r'Asset\s+[Dd]epreciation\s+for\s+period|'
    r'[Dd]ecline\s+in\s+value)',
    re.IGNORECASE
)

_FP_LEGAL_BOILERPLATE = re.compile(
    r'(?:Tax\s+returns?\s+are\s+assessed|'
    r'This\s+declaration\s+is\s+to\s+be\s+completed|'
    r'Self[\-\s]?Assessment\s+basis|'
    r'Electronic\s+Lodgment\s+Service|'
    r'I\s+have\s+kept\s+appropriate|'
    r'Name\s+of\s+supplier\s+and\s+amount|'
    r'nature\s+of\s+goods.?services|'
    r'Specific\s+instructions\s+on\s+the\s+understanding|'
    r'privacy,?\s+go\s+to\s+ato\.gov\.au|'
    r'bank\s+statements?\s+online\s+through|'
    r'Enjoy\s+the\s+convenience\s+of\s+download|'
    r'I\s+will\s+be\s+able\s+to\s+produce\s+such)',
    re.IGNORECASE
)

_FP_GENERIC_DATES = re.compile(
    r'(?:FY\s?\d{4}|'
    r'\d{4}[-\u2013]\d{2,4}|'
    r'[Aa]s\s+at\s+\d|'
    r'(?:1|01)\s*(?:July|Jul)\s*\d{4}|'
    r'(?:30|31)\s*(?:June|Jun)\s*\d{4}|'
    r'for\s+period\s+|'
    r'PRIOR\s+YEAR|'
    r'TAX\s+RETURN\s+DETAILS|'
    r'ATO\s+INTEREST)',
    re.IGNORECASE
)


def _is_false_positive(entity_value: str, entity_token: str, ocr_line: str) -> bool:
    """Check if an SLM entity is a likely false positive that should not be redacted."""
    # 1. Form headings and field labels
    if _FP_FORM_LABELS.search(ocr_line):
        return True

    # 2. Legal boilerplate and standard instructions
    if _FP_LEGAL_BOILERPLATE.search(ocr_line):
        return True

    # 3. Generic dates misidentified as DOB
    if entity_token == '[DOB]' and _FP_GENERIC_DATES.search(ocr_line):
        return True

    # 4. Financial year / statement dates tagged as DOB
    if entity_token == '[DOB]':
        if re.search(r'\b\d{4}[-\u2013]\d{2,4}\b', entity_value):
            return True
        if re.search(r'[Aa]s\s+at\s+', ocr_line):
            return True
        if re.search(r'(?:From|To)\s+Statement\s+Date', ocr_line, re.IGNORECASE):
            return True
        if re.search(r'^Date\s+and\s+Time', ocr_line, re.IGNORECASE):
            return True

    # 5. Spouse form labels tagged as FAMILY_INFO or PERSON
    if entity_token in ('[FAMILY_INFO]', '[PERSON]', '[SENSITIVE_INFO]'):
        if re.search(r"[Yy]our\s+spouse'?s?\s+(?:total|taxed|foreign|reportable|target)",
                      ocr_line):
            return True

    # 6. Standalone financial figures (no name context)
    if entity_token == '[FINANCIAL_STATUS]':
        if re.match(r'^[\$\d,.\s%]+$', entity_value.strip()):
            return True

    # 7. Spouse details section headers
    if entity_token in ('[MARITAL_STATUS]', '[FAMILY_INFO]'):
        if re.search(r'[Ss]pouse\s+details\s*[-\u2013]?\s*married\s+or\s+de\s+facto',
                      ocr_line):
            return True

    return False


def _looks_like_address_continuation(text: str) -> bool:
    """Check if a text line looks like the continuation of an Australian address."""
    t = text.strip()
    if not t:
        return False
    # State + postcode
    if AU_STATE_POSTCODE_PAT.search(t):
        return True
    # Suburb followed by state abbreviation
    if re.search(r'^[A-Z][a-zA-Z\s]+,?\s*(?:NSW|VIC|QLD|SA|WA|TAS|NT|ACT)',
                  t, re.IGNORECASE):
        return True
    # Just a 4-digit postcode
    if re.match(r'^\d{4}$', t):
        return True
    # PO Box
    if re.search(r'\bP\.?O\.?\s*Box\b', t, re.IGNORECASE):
        return True
    return False


# ----------------------------------------------------------------------
# 6. Redaction Engine
# ----------------------------------------------------------------------
def redact_page(pil_image: Image.Image, page_num: int) -> Tuple:
    """Redact a single page using regex + SLM + spaCy NER.
    
    Returns:
        (redacted_image, found_entities, ocr_cache)
        where ocr_cache = (lines_text, line_bboxes, redacted_indices)
    """
    print(f"\n  -- Page {page_num + 1} --")

    if ocr_engine is None:
        print("    OCR engine not initialized.")
        return pil_image, [], ([], [], set())

    # ── Image size guard ──────────────────────────────────────────────
    img = pil_image.convert('RGB')
    orig_w, orig_h = img.size
    scale_x = scale_y = 1.0

    if max(orig_w, orig_h) > MAX_IMG_SIDE:
        scale     = MAX_IMG_SIDE / max(orig_w, orig_h)
        new_w     = int(orig_w * scale)
        new_h     = int(orig_h * scale)
        img       = img.resize((new_w, new_h), Image.LANCZOS)
        scale_x   = orig_w / new_w
        scale_y   = orig_h / new_h
        print(f"    Resized for OCR: ({orig_w}×{orig_h}) → ({new_w}×{new_h})")

    # ── Run OCR ───────────────────────────────────────────────────────
    try:
        arr            = np.array(img, dtype=np.uint8)
        predict_result = ocr_engine.predict(arr)
    except Exception as e:
        print(f"    OCR failed: {e}")
        import traceback; traceback.print_exc()
        return pil_image, [], ([], [], set())

    # ── Parse OCR output ──────────────────────────────────────────────
    ocr_lines = parse_ocr_result(predict_result)


    if not ocr_lines:
        print("    No text detected on page.")
        return pil_image, [], ([], [], set())

    print(f"    OCR detected {len(ocr_lines)} text lines")

    # Build working lists — scale bboxes back to original image coords
    lines_text:  List[str]            = []
    line_bboxes: List[Tuple[int,...]] = []

    for (bbox_pts, text, _confidence) in ocr_lines:
        xs = [p[0] * scale_x for p in bbox_pts]
        ys = [p[1] * scale_y for p in bbox_pts]
        lines_text.append(text)
        line_bboxes.append((
            int(min(xs)), int(min(ys)),
            int(max(xs)), int(max(ys))
        ))
    full_page_text = " ".join(lines_text)

    # ── Prepare drawing canvas (original resolution) ──────────────────
    img_cv = cv2.cvtColor(np.array(pil_image.convert('RGB')), cv2.COLOR_RGB2BGR)
    h_img, w_img    = img_cv.shape[:2]
    redaction_boxes: List[List[int]] = []
    redacted_indices: Set[int]       = set()
    found_entities: List[Dict]       = []   # Track entities for cross-page consistency

    def add_box(bbox: Tuple, label: str, value: str = ""):
        x1, y1, x2, y2 = bbox
        rx1 = max(0,     int(x1) - 2)
        ry1 = max(0,     int(y1) - 2)
        rx2 = min(w_img, int(x2) + 2)
        ry2 = min(h_img, int(y2) + 2)
        redaction_boxes.append([rx1, ry1, rx2, ry2])
        print(f"      [{label}] {value[:80]}")

    # ------------------------------------------------------------------
    # PASS 1 — Fast regex + checksum validators
    # ------------------------------------------------------------------
    for i, (bbox, text) in enumerate(zip(line_bboxes, lines_text)):
        if i in redacted_indices:
            continue
        t = text.strip()

        m = TFN_PAT.search(t)
        if m and valid_tfn(m.group()):
            add_box(bbox, "TFN", t)
            found_entities.append({'value': m.group(), 'token': '[GOVERNMENT_ID]'})
            redacted_indices.add(i); continue

        m = MEDICARE_PAT.search(t)
        if m and valid_medicare(m.group()):
            add_box(bbox, "Medicare", t)
            found_entities.append({'value': m.group(), 'token': '[GOVERNMENT_ID]'})
            redacted_indices.add(i); continue

        m = ABN_PAT.search(t)
        if m and valid_abn(m.group()):
            add_box(bbox, "ABN", t)
            found_entities.append({'value': m.group(), 'token': '[BUSINESS_ID]'})
            redacted_indices.add(i); continue

        m = ACN_PAT.search(t)
        if m and valid_acn(m.group()):
            add_box(bbox, "ACN", t)
            found_entities.append({'value': m.group(), 'token': '[BUSINESS_ID]'})
            redacted_indices.add(i); continue

        m = CREDIT_CARD_PAT.search(t)
        if m and valid_luhn(m.group()):
            add_box(bbox, "CreditCard", t)
            found_entities.append({'value': m.group(), 'token': '[BANK_DETAILS]'})
            redacted_indices.add(i); continue

        m = US_SSN_PAT.search(t)
        if m and valid_us_ssn(m.group()):
            add_box(bbox, "US-SSN", t)
            found_entities.append({'value': m.group(), 'token': '[GOVERNMENT_ID]'})
            redacted_indices.add(i); continue

        m = AADHAAR_PAT.search(t)
        if m and valid_verhoeff(m.group()):
            add_box(bbox, "Aadhaar", t)
            found_entities.append({'value': m.group(), 'token': '[GOVERNMENT_ID]'})
            redacted_indices.add(i); continue

        m = IBAN_PAT.search(t)
        if m and valid_iban(m.group()):
            add_box(bbox, "IBAN", t)
            found_entities.append({'value': m.group(), 'token': '[BANK_DETAILS]'})
            redacted_indices.add(i); continue

        if PHONE_PAT.search(t):
            add_box(bbox, "Phone", t)
            found_entities.append({'value': PHONE_PAT.search(t).group(), 'token': '[PHONE]'})
            redacted_indices.add(i); continue

        if EMAIL_PAT.search(t):
            add_box(bbox, "Email", t)
            found_entities.append({'value': EMAIL_PAT.search(t).group(), 'token': '[EMAIL]'})
            redacted_indices.add(i); continue

        # BSB + Account number pair
        if BSB_ACCOUNT_PAT.search(t):
            add_box(bbox, "BSB+Acct", t)
            found_entities.append({'value': t, 'token': '[BANK_DETAILS]'})
            redacted_indices.add(i); continue

        # Reference / ID numbers with labels
        if REF_NUMBER_PAT.search(t):
            add_box(bbox, "RefNumber", t)
            found_entities.append({'value': t, 'token': '[GOVERNMENT_ID]'})
            redacted_indices.add(i); continue

        # Account number with label (split out of REF_NUMBER_PAT so it gets its own token)
        if ACCOUNT_NUMBER_PAT.search(t):
            add_box(bbox, "AccountNumber", t)
            found_entities.append({'value': t, 'token': '[ACCOUNT_NUMBER]'})
            redacted_indices.add(i); continue

        # Contextual Date Search
        if DATE_PAT.search(t):
            # Send to REVIEW queue directly instead of auto-redacting
            val = DATE_PAT.search(t).group()
            review_queue.append((i, "[DOB]", val, bbox))
            # DO NOT continue, allow GLiNER to also see the text if needed

        # Australian street address - DISABLED PER USER REQUEST
        # if AU_ADDRESS_PAT.search(t):
        #     add_box(bbox, "Address", t)
        #     found_entities.append({'value': t, 'token': '[ADDRESS]'})
        #     redacted_indices.add(i)
        #     # Check next lines for address continuation
        #     for adj in range(1, 3):
        #         adj_idx = i + adj
        #         if adj_idx < len(lines_text) and adj_idx not in redacted_indices:
        #             if _looks_like_address_continuation(lines_text[adj_idx]):
        #                 add_box(line_bboxes[adj_idx], "Address cont.",
        #                         lines_text[adj_idx])
        #                 found_entities.append({'value': lines_text[adj_idx],
        #                                        'token': '[ADDRESS]'})
        #                 redacted_indices.add(adj_idx)
        #     continue

        # Health fund membership number
        if HEALTH_MEMBER_PAT.search(t):
            add_box(bbox, "HealthMember", t)
            found_entities.append({'value': t, 'token': '[SENSITIVE_INFO]'})
            redacted_indices.add(i); continue

        # State + postcode (standalone address continuation line) - DISABLED PER USER REQUEST
        # if AU_STATE_POSTCODE_PAT.search(t) and len(t) < 30:
        #     add_box(bbox, "StatePostcode", t)
        #     found_entities.append({'value': t, 'token': '[ADDRESS]'})
        #     redacted_indices.add(i); continue

    # ------------------------------------------------------------------
    # PASS 2 — Layer 3: Contextual Risk Scoring Engine (spaCy + Regex Sweeps)
    # ------------------------------------------------------------------
    review_queue: List[Tuple[int, str, str, Tuple]] = [] # (line_idx, entity_token, entity_value, bbox)
    if gliner_model is not None:
        try:
            # Removed "ADDRESS" from labels per user request
            labels = ["PERSON", "ORGANIZATION", "FINANCIAL", "COMPANY"]
            all_entities = []
            chunks = list(_chunk_text_with_overlap(full_page_text, max_words=200, overlap=50))
            for i, (chunk_text, char_offset) in enumerate(chunks, 1):
                chunk_ents = gliner_model.predict_entities(chunk_text, labels)
                found_labels = [e["text"] for e in chunk_ents]
                print(f"      -> [GLiNER] Processing chunk {i}/{len(chunks)} - Found: {found_labels}")
                for ent in chunk_ents:
                    # Adjust offsets relative to the full page string
                    ent["start"] += char_offset
                    ent["end"] += char_offset
                    all_entities.append(ent)
            
            # Deduplicate identical entity spans
            unique_entities = {}
            for ent in all_entities:
                key = (ent["start"], ent["end"], ent["label"])
                if key not in unique_entities or ent["score"] > unique_entities[key]["score"]:
                    unique_entities[key] = ent
                    
            entities = list(unique_entities.values())
            offsets: List[Tuple[int, int]] = []
            pos = 0
            for t in lines_text:
                offsets.append((pos, pos + len(t)))
                pos += len(t) + 1

            matched_ent_indices: Set[int] = set()

            for ent_idx, ent in enumerate(entities):
                ent_label = ent["label"].upper()
                if ent_label not in {'PERSON', 'ORGANIZATION', 'COMPANY'}:
                    continue

                ent_text = ent["text"].strip()
                ent_text_lower = ent_text.lower()
                
                if ent_text_lower in NER_BLOCKLIST:
                    continue
                if len(ent_text) < 3 or not any(c.isalpha() for c in ent_text):
                    continue

                # Find line index
                line_idx = -1
                for i, (ls, le) in enumerate(offsets):
                    if ent["start"] < le and ent["end"] > ls:
                        line_idx = i
                        break
                
                if line_idx == -1 or line_idx in redacted_indices or ent_idx in matched_ent_indices:
                    continue
                
                # Context snippet (line before, current line, line after)
                start_l = max(0, line_idx - 1)
                end_l = min(len(lines_text), line_idx + 2)
                snippet = " ".join(lines_text[start_l:end_l])
                snippet_lower = snippet.lower()

                if ent["score"] < 0.70:
                    continue  # Ignore low confidence matches entirely

                decision = "REVIEW"
                
                if ent_label == 'PERSON':
                    if any(kw in snippet_lower for kw in ['account holder', 'beneficiary', 'borrower', 'name:', 'tenant', 'next of kin']):
                        decision = "AUTO_MASK"
                    elif ent["score"] > GLINER_CONFIDENCE_THRESHOLD:
                        decision = "AUTO_MASK"
                elif ent_label in ('ORGANIZATION', 'COMPANY'):
                    # Check AUTO_MASK for ORG
                    if any(kw in snippet_lower for kw in ['employer', 'health fund', 'member number']):
                        decision = "AUTO_MASK"
                    # Check AUTO_KEEP for ORG
                    elif any(kw in snippet_lower for kw in ['bank', 'branch', 'item ', 'section ', 'pty ltd', ' pty ']):
                        decision = "AUTO_KEEP"
                    elif ent["score"] > GLINER_CONFIDENCE_THRESHOLD:
                        decision = "AUTO_MASK"

                mapped_label = 'ORG' if ent_label in ('ORGANIZATION', 'COMPANY') else ent_label
                if decision == "AUTO_MASK":
                    add_box(line_bboxes[line_idx], f"{mapped_label} (AUTO_MASK)", ent_text)
                    redacted_indices.add(line_idx)
                    found_entities.append({'value': ent_text, 'token': f'[{mapped_label}]'})
                elif decision == "REVIEW":
                    review_queue.append((line_idx, f"[{mapped_label}]", ent_text, line_bboxes[line_idx]))
                
                matched_ent_indices.add(ent_idx)

            # False Negative Sweep: Capitalized multi-word phrases after triggers
            fn_triggers = [r"Tenant\s*name\s*[:\n]\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)",
                           r"Next\s*of\s*kin\s*[:\n]\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)",
                           r"Spouse\s*[:\n]\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)"]
            for i, line_text in enumerate(lines_text):
                if i in redacted_indices: continue
                for pat in fn_triggers:
                    m = re.search(pat, line_text)
                    if m:
                        val = m.group(1)
                        # Add to REVIEW queue if not already redacted
                        start_l = max(0, i - 1)
                        end_l = min(len(lines_text), i + 2)
                        snippet = " ".join(lines_text[start_l:end_l])
                        review_queue.append((i, "[PERSON]", val, line_bboxes[i]))
                        break

        except Exception as e:
            print(f"    Layer 3 failed: {e}")
            import traceback
            traceback.print_exc()

    # ------------------------------------------------------------------
    # PASS 3 — Layer 4: Enterprise Privacy Decision Engine (LLM)
    # ------------------------------------------------------------------
    if review_queue:
        print(f"    Layer 4: Passing {len(review_queue)} entities to LLM Decision Engine...")
        for line_idx, entity_token, entity_value, bbox in review_queue:
            if line_idx in redacted_indices:
                continue
            
            start_l = max(0, line_idx - 1)
            end_l = min(len(lines_text), line_idx + 2)
            snippet = " ".join(lines_text[start_l:end_l])
            
            # Apply Date and Financial Safe Default Override for PDF Pipeline
            candidate_dummy = {
                "type": entity_token.strip("[]"),
                "value": entity_value,
                "source": "DATE_SWEEP" if entity_token == "[DOB]" else "GLINER"
            }
            override = apply_date_financial_override(candidate_dummy, snippet)
            if override == "SKIP":
                print(f"      [SAFE_SKIP OVERRIDE] {entity_token} '{entity_value}' | Context: '{snippet[:50]}'")
                log_forensic_decision(
                    {"id": f"pdf_rev_{line_idx}", "type": entity_token.strip("[]"), "value": entity_value, "context": snippet, "pre_decision": "SKIP", "source": "PDF_PIPELINE"},
                    "SKIP", "SAFE_DEFAULT_SKIP"
                )
                continue
            elif override == "REDACT":
                print(f"      [SAFE_REDACT OVERRIDE] {entity_token} '{entity_value}' | Context: '{snippet[:50]}'")
                add_box(bbox, f"OVERRIDE {entity_token}", entity_value)
                redacted_indices.add(line_idx)
                found_entities.append({'value': entity_value, 'token': entity_token})
                log_forensic_decision(
                    {"id": f"pdf_rev_{line_idx}", "type": entity_token.strip("[]"), "value": entity_value, "context": snippet, "pre_decision": "REDACT", "source": "PDF_PIPELINE"},
                    "REDACT", "SAFE_DEFAULT_REDACT"
                )
                continue
            
            decision = _call_ollama_for_entity(entity_token, entity_value, snippet)
            if decision == "MASK":
                add_box(bbox, f"LLM {entity_token}", entity_value)
                redacted_indices.add(line_idx)
                found_entities.append({'value': entity_value, 'token': entity_token})
                log_forensic_decision(
                    {"id": f"pdf_rev_{line_idx}", "type": entity_token.strip("[]"), "value": entity_value, "context": snippet, "pre_decision": None, "source": "PDF_PIPELINE"},
                    "REDACT", "LLM_CONTEXTUAL_REDACT"
                )
            else:
                print(f"      [LLM KEEP] {entity_token} '{entity_value}'")
                log_forensic_decision(
                    {"id": f"pdf_rev_{line_idx}", "type": entity_token.strip("[]"), "value": entity_value, "context": snippet, "pre_decision": None, "source": "PDF_PIPELINE"},
                    "SKIP", "LLM_CONTEXTUAL_KEEP"
                )
    # ------------------------------------------------------------------
    # APPLY — Draw black rectangles
    # ------------------------------------------------------------------
    for (x1, y1, x2, y2) in redaction_boxes:
        if x2 > x1 and y2 > y1:
            cv2.rectangle(img_cv, (x1, y1), (x2, y2), (0, 0, 0), cv2.FILLED)

    print(f"    Redacted {len(redaction_boxes)} regions on page {page_num + 1}")
    result_image = Image.fromarray(cv2.cvtColor(img_cv, cv2.COLOR_BGR2RGB))
    return result_image, found_entities, (lines_text, line_bboxes, redacted_indices)




# ----------------------------------------------------------------------
# Masking Engine
# ----------------------------------------------------------------------
def get_candidate_token(base_token: str, original_value: str, secret_map: dict, counters: dict) -> str:
    original_value_lower = original_value.lower()
    # Check if value already has a candidate
    if original_value_lower in secret_map:
        return secret_map[original_value_lower]["candidate"]
    
    # Generate new candidate
    cat_name = base_token.strip("[]")
    counters[cat_name] = counters.get(cat_name, 0) + 1
    new_candidate = f"<*{cat_name}_{counters[cat_name]}*>"
    
    secret_map[original_value_lower] = {
        "candidate": new_candidate,
        "original_value": original_value
    }
    return new_candidate

def _chunk_text_with_overlap(text: str, max_words: int = 200, overlap: int = 50):
    """
    Yields (chunk_string, chunk_char_offset).
    Splits text into sliding windows of max_words with a given overlap.
    Preserves exact original character formatting.
    """
    if not text.strip():
        return
        
    # Find all word starting positions
    word_starts = [m.start() for m in re.finditer(r'\S+', text)]
    if not word_starts:
        return
        
    word_count = len(word_starts)
    start_idx = 0
    
    while start_idx < word_count:
        end_idx = min(start_idx + max_words, word_count)
        
        char_start = word_starts[start_idx]
        if end_idx < word_count:
            char_end = word_starts[end_idx]
        else:
            char_end = len(text)
            
        chunk_str = text[char_start:char_end].strip()
        yield chunk_str, char_start
        
        if end_idx == word_count:
            break
            
        start_idx += (max_words - overlap)

import hashlib
from datetime import datetime

def log_forensic_decision(candidate: dict, decision: str, reason_code: str):
    log_path = SECRETS_FOLDER / "pii_forensic_log.jsonl"
    record = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "id": candidate["id"],
        "type": candidate["type"],
        "decision": decision,
        "pre_decision": candidate.get("pre_decision"),
        "source": candidate.get("source", "UNKNOWN"),
        "reason_code": reason_code,
        "hash": hashlib.sha256(candidate["value"].encode("utf-8")).hexdigest(),
        "context_length": len(candidate.get("context", ""))
    }
    # Log raw value only if redacted
    if decision == "REDACT":
        record["raw_value"] = candidate["value"]
    else:
        record["raw_value"] = None
        
    SECRETS_FOLDER.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

def get_grid_context(df: pd.DataFrame, row_idx: int, col_idx: int) -> str:
    """Extract surrounding cells to provide tabular context."""
    context_parts = []
    if row_idx > 0:
        context_parts.append(f"Above: {df.iloc[row_idx-1, col_idx]}")
    if col_idx > 0:
        context_parts.append(f"Left: {df.iloc[row_idx, col_idx-1]}")
    if col_idx < len(df.columns)-1:
        context_parts.append(f"Right: {df.iloc[row_idx, col_idx+1]}")
    if row_idx < len(df)-1:
        context_parts.append(f"Below: {df.iloc[row_idx+1, col_idx]}")
    return f"Grid Context: {' | '.join(context_parts)} | Target: {df.iloc[row_idx, col_idx]}"

# Per-pattern checksum guards for TIER_1_REGEX entries that need more than a shape match.
# A regex hit whose validator returns False is dropped instead of auto-redacted.
TIER_1_VALIDATORS = {
    "MEDICARE": valid_medicare,
}

def extract_regex_candidates(df: pd.DataFrame) -> list:
    global_rules = get_pii_rules()
    regex_map = global_rules.get("TIER_1_REGEX", {})
    candidates = []

    compiled_regexes = {}
    for r_name, r_pat in regex_map.items():
        compiled_regexes[r_name] = re.compile(r_pat, re.IGNORECASE)

    for row_idx, row in df.iterrows():
        for col_idx, cell in enumerate(row):
            if pd.isna(cell):
                continue
            cell_str = str(cell).strip()
            if not cell_str:
                continue

            for pattern_name, regex in compiled_regexes.items():
                validator = TIER_1_VALIDATORS.get(pattern_name)
                for m in regex.finditer(cell_str):
                    val = m.group()
                    if validator and not validator(val):
                        continue
                    candidates.append({
                        "id": f"regex_{uuid.uuid4().hex[:8]}",
                        "type": pattern_name,
                        "value": val,
                        "context": f"Row {row_idx}, Col {col_idx}",
                        "token_base": f"[{pattern_name}]",
                        "pre_decision": "REDACT",
                        "source": "REGEX",
                        "cell_loc": (row_idx, col_idx)
                    })
    return candidates

def apply_date_financial_override(candidate: dict, context_snippet: str) -> str:
    """
    Implements the 'Safe Default' rule for DATE and FINANCIAL candidates.
    Returns: 'SKIP' (bypass LLM), 'REDACT' (bypass LLM), or None (send to LLM).
    """
    entity_type = candidate.get("type")
    source = candidate.get("source")
    
    if entity_type not in ["DATE", "FINANCIAL", "DOB", "FINANCIAL_STATUS"]:
        return None
        
    if source == "STRUCTURAL":
        return None
        
    # DEBUG: Log what we're evaluating
    val_str = candidate.get("value", "")
    print(f"        [DEBUG] OVERRIDE CHECK: {entity_type}='{val_str[:20]}' | Context: '{context_snippet[:50].strip()}'")
        
    # User requested: "do not redact any financial status"
    if entity_type in ["FINANCIAL", "FINANCIAL_STATUS"]:
        print(f"        [DEBUG] OVERRIDE: SKIP (financial status disabled)")
        return "SKIP"
        
    if not context_snippet or len(context_snippet.strip()) < 5:
        # Fallback for empty context (e.g., Tesseract standalone cell)
        print(f"        [DEBUG] OVERRIDE: SKIP (empty context)")
        return "SKIP"
        
    # Strict birth/dob triggers for date candidate
    date_triggers = r'\b(birth|dob|born|d\.o\.b|date of birth)\b'
    
    if re.search(date_triggers, context_snippet, re.IGNORECASE):
        print(f"        [DEBUG] OVERRIDE: Sending to LLM (trigger found)")
        return None
    else:
        print(f"        [DEBUG] OVERRIDE: SKIP (no trigger)")
        return "SKIP"

_JUNK_VALUES = {"n/a", "na", "none", "nil", "null", "test", "test account", "unknown", ""}

def _allowlist_hit(text_lower: str, allowlist_lower: list) -> bool:
    """
    Whole-word allowlist match. Using substring containment here would let a short
    entry like "medicare" suppress an actual Medicare membership number that merely
    has the word "Medicare" next to it (e.g. "Medicare card 1234567890") — the
    allowlist is meant to protect the org name, not values that happen to sit near it.
    """
    return any(re.search(rf'\b{re.escape(aw)}\b', text_lower) for aw in allowlist_lower)

def normalize_label(text: str) -> str:
    """
    Hardened normalization for structural labels.
    Handles colons, pipes, parentheses, suffixes, and extra whitespace.
    """
    if not text:
        return ""
    
    # Step 1: Lowercase
    text = text.lower()
    
    # Step 2: Remove common punctuation (colons, pipes, slashes, brackets)
    text = re.sub(r'[:;|/\\()\[\]]', ' ', text)
    
    # Step 3: Remove common filler suffixes (MUST be at the end of the string)
    # This handles "Family name and suffix" -> "family name"
    text = re.sub(r'\s*(and suffix|\(s\)|optional|\(optional\))\s*$', '', text)
    
    # Step 4: Remove extra whitespace
    text = ' '.join(text.split())
    
    return text.strip()

def extract_structural_candidates(df: pd.DataFrame, exclude_cells: set, allowlist_lower: list = None) -> list:
    global_rules = get_pii_rules()
    label_map = {}
    for entity_type, labels in global_rules.get("STRUCTURAL_LABELS", {}).items():
        for lab in labels:
            norm_key = normalize_label(lab)
            label_map[norm_key] = entity_type
            
    if allowlist_lower is None:
        allowlist_lower = []
        
    def get_pre_decision(val: str) -> str:
        val_lower = val.lower()
        if _allowlist_hit(val_lower, allowlist_lower):
            return "SKIP"
        return "REDACT"
            
    candidates = []
    unmatched_labels = set()
    for row_idx in range(len(df)):
        for col_idx in range(len(df.columns)):
            if (row_idx, col_idx) in exclude_cells:
                continue
                
            cell_raw = df.iloc[row_idx, col_idx]
            if pd.isna(cell_raw):
                continue
            cell_value = str(cell_raw).strip()
            norm_cell = normalize_label(cell_value)
            
            # Same-cell extraction (e.g. "Given names: John")
            if ":" in cell_value:
                parts = cell_value.split(":", 1)
                label_part = parts[0].strip()
                norm_label_part = normalize_label(label_part)
                value_part = parts[1].strip()
                if norm_label_part in label_map and value_part:
                    if value_part.lower() not in _JUNK_VALUES and len(value_part) >= 2:
                        pre_dec = get_pre_decision(value_part)
                        candidates.append({
                            "id": f"struct_{uuid.uuid4().hex[:8]}",
                            "type": label_map[norm_label_part],
                            "value": value_part,
                            "context": cell_value,
                            "token_base": f"[{label_map[norm_label_part]}]",
                            "pre_decision": pre_dec,
                            "source": "STRUCTURAL",
                            "cell_loc": (row_idx, col_idx)
                        })
                        continue
                        
            matched_entity_type = label_map.get(norm_cell)
            if not matched_entity_type:
                # To reduce noise, only log it if it looks somewhat like a label (e.g. capitalized, relatively short)
                if len(cell_value) < 40 and ":" in cell_value:
                    unmatched_labels.add((norm_cell, cell_value, row_idx, col_idx))
                continue
                
            # Rule 1: Below
            if row_idx + 1 < len(df):
                if (row_idx + 1, col_idx) not in exclude_cells:
                    target_cell = df.iloc[row_idx + 1, col_idx]
                    if pd.notna(target_cell):
                        target_val = str(target_cell).strip()
                        if target_val and target_val.lower() not in _JUNK_VALUES and normalize_label(target_val) not in label_map and len(target_val) >= 2:
                            pre_dec = get_pre_decision(target_val)
                            candidates.append({
                                "id": f"struct_{uuid.uuid4().hex[:8]}",
                                "type": matched_entity_type,
                                "value": target_val,
                                "context": get_grid_context(df, row_idx + 1, col_idx),
                                "token_base": f"[{matched_entity_type}]",
                                "pre_decision": pre_dec,
                                "source": "STRUCTURAL",
                                "cell_loc": (row_idx + 1, col_idx)
                            })
                            
            # Rule 2: Right (Bounded Lookahead)
            max_lookahead = global_rules.get("STRUCTURAL_SETTINGS", {}).get("max_lookahead", 5)
            value_found = None
            found_col_idx = -1
            for offset in range(1, max_lookahead + 1):
                target_col = col_idx + offset
                if target_col >= len(df.columns):
                    break
                if (row_idx, target_col) in exclude_cells:
                    continue
                target_cell = df.iloc[row_idx, target_col]
                if pd.notna(target_cell):
                    target_val = str(target_cell).strip()
                    if not target_val:
                        continue
                        
                    # CRITICAL SAFETY: If this cell is another structural label, stop scanning
                    if normalize_label(target_val) in label_map:
                        break
                        
                    if target_val.lower() not in _JUNK_VALUES and len(target_val) >= 2:
                        value_found = target_val
                        found_col_idx = target_col
                        break
            
            if value_found:
                if not any(c["value"] == value_found and c["cell_loc"] == (row_idx, found_col_idx) for c in candidates):
                    if found_col_idx > col_idx + 1:
                        print(f"        [DEBUG] STRUCTURAL_LOOKAHEAD: Label '{cell_value}' found value '{value_found}' at offset {found_col_idx - col_idx}")
                    pre_dec = get_pre_decision(value_found)
                    candidates.append({
                        "id": f"struct_{uuid.uuid4().hex[:8]}",
                        "type": matched_entity_type,
                        "value": value_found,
                        "context": get_grid_context(df, row_idx, found_col_idx),
                        "token_base": f"[{matched_entity_type}]",
                        "pre_decision": pre_dec,
                        "source": "STRUCTURAL",
                        "cell_loc": (row_idx, found_col_idx)
                    })
                                
    unmatched_counts = {}
    for norm_cell, raw_val, r_idx, c_idx in unmatched_labels:
        unmatched_counts[norm_cell] = unmatched_counts.get(norm_cell, 0) + 1
        
    if unmatched_counts:
        for label, count in unmatched_counts.items():
            if count > 5:
                print(f"CRITICAL STRUCTURAL MISMATCH: '{label}' appeared {count} times. Update pii_rules.yaml!")
        with open(SECRETS_FOLDER / "unmatched_structural_labels_debug.log", "a", encoding="utf-8") as f:
            for item in unmatched_labels:
                f.write(f"{item}\n")
                
    return candidates

def deduplicate_candidates(candidates: list) -> list:
    """Deduplicates candidates with priority REGEX > STRUCTURAL > GLINER."""
    # Priority values: lower is better
    priority_map = {"REGEX": 1, "STRUCTURAL": 2, "GLINER": 3, "DATE_SWEEP": 4, "FALSE_NEGATIVE_SWEEP": 5}
    seen_texts = {}
    for c in candidates:
        key = (c['value'].strip().lower(), c['type'])
        c_priority = priority_map.get(c.get("source", "GLINER"), 99)
        
        if key not in seen_texts:
            seen_texts[key] = c
        else:
            existing_priority = priority_map.get(seen_texts[key].get("source", "GLINER"), 99)
            if c_priority < existing_priority:
                seen_texts[key] = c
    return list(seen_texts.values())

def propagate_context_seeds(text: str, candidates: list, found: dict,
                             secret_map: dict, candidate_counters: dict, stats: dict) -> None:
    """Ported from PII_Code_19 Aug's context_detector._propagate_seeds().

    Once a PERSON or COMPANY/ORGANIZATION value has actually been redacted
    (i.e. its lowercased value is already a key in `found`), also scan the
    rest of the document text for bare individual name parts (first/last
    name) or a de-suffixed organization name, and redact those too. This
    catches a second/later mention that drops the surname (e.g. "Sarah" on
    its own after "Sarah Connor" was redacted) or the "Ltd"/"Corp" suffix —
    something apply_masking()'s existing exact-value word-boundary match
    does not catch, since it only re-matches the *exact* string that was
    originally found.

    Mutates `found`/`stats` in place. Per wiki/plan-compare-wiki.md Q4, these
    propagated entities are auto-redacted (same as everything else today)
    but logged under a distinct reason_code (CONTEXT_SEED_PROPAGATION) so a
    future Critique Agent pass can specifically re-verify them.
    """
    seeds_by_type: Dict[str, Set[str]] = {}

    for c in candidates:
        val = str(c.get("value", "")).strip()
        if not val or val.lower() not in found:
            continue  # only propagate from values that were actually redacted
        ctype = c.get("type")
        if ctype not in ("PERSON", "COMPANY", "ORGANIZATION"):
            continue

        seeds_by_type.setdefault(ctype, set())

        if ctype == "PERSON":
            for part in val.split():
                if len(part) > 3 and part[0].isupper() and part.lower() not in NER_BLOCKLIST:
                    seeds_by_type[ctype].add(part)

        if ctype in ("COMPANY", "ORGANIZATION"):
            for suffix in (" Ltd", " Limited", " Corp", " Corporation", " Inc", " Co.", " Co", " Pty Ltd", " Pty"):
                if val.endswith(suffix):
                    cleaned = val[:-len(suffix)].strip()
                    if len(cleaned) > 3:
                        seeds_by_type[ctype].add(cleaned)

    if not seeds_by_type:
        return

    token_map = {"PERSON": "[PERSON]", "COMPANY": "[BUSINESS_ID]", "ORGANIZATION": "[ORG]"}
    for ctype, values in seeds_by_type.items():
        token_base = token_map[ctype]
        for val in values:
            val_lower = val.lower()
            if val_lower in found:
                continue  # already redacted, as its own candidate or an earlier propagated seed
            if not re.search(rf'\b{re.escape(val)}\b', text):
                continue
            cand_token = get_candidate_token(token_base, val, secret_map, candidate_counters)
            found[val_lower] = cand_token
            stats["total"] += 1
            stats["redact"] += 1
            log_forensic_decision(
                {"id": f"cand_{uuid.uuid4().hex[:8]}", "type": ctype, "value": val,
                 "context": "", "pre_decision": None, "source": "CONTEXT_SEED_PROPAGATION"},
                "REDACT", "CONTEXT_SEED_PROPAGATION",
            )


def scan_text_for_entities(df: pd.DataFrame, secret_map: dict, candidate_counters: dict) -> Dict[str, str]:
    """Use Tier 0 Regex, Tier 0 Structural, Tier 2 GLiNER and Batched SLM to find contextual entities."""
    found = {}
    
    global_rules = get_pii_rules()
    allowlist_phrases = global_rules.get('ALLOWLIST', [])
    allowlist_lower = [p.lower() for p in allowlist_phrases]
    
    stats = {"total": 0, "skip_allowlist": 0, "skip_llm": 0, "redact": 0}
    
    # 1. RUN REGEX FIRST (Absolute Authority)
    regex_candidates = extract_regex_candidates(df)
    for c in regex_candidates:
        stats["total"] += 1
        cand_token = get_candidate_token(c["token_base"], c["value"], secret_map, candidate_counters)
        found[c["value"].lower()] = cand_token
        stats["redact"] += 1
        log_forensic_decision(c, "REDACT", "DETERMINISTIC_PATTERN")
        
    regex_hit_cells = set(c["cell_loc"] for c in regex_candidates)
    
    # 2. Run Structural Scanner
    structural_candidates = extract_structural_candidates(df, exclude_cells=regex_hit_cells, allowlist_lower=allowlist_lower)
    
    # Reconstruct text for GLiNER, skipping regex hit cells
    row_strings = []
    for row_idx, row_data in df.iterrows():
        valid_cells = []
        for col_idx, cell_value in enumerate(row_data):
            if pd.notna(cell_value) and (row_idx, col_idx) not in regex_hit_cells:
                valid_cells.append(str(cell_value))
        row_str = " | ".join(valid_cells)
        if row_str.strip():
            row_strings.append(row_str)
            
    text = "\n".join(row_strings)
    
    candidates = structural_candidates
    
    if gliner_model is not None and text.strip():
        text_lower = text.lower()
        # Removed "ADDRESS" from labels per user request
        labels = ["PERSON", "ORGANIZATION", "FINANCIAL", "COMPANY"]
        
        all_entities = []
        chunks = list(_chunk_text_with_overlap(text, max_words=200, overlap=50))
        for i, (chunk_text, char_offset) in enumerate(chunks, 1):
            chunk_ents = gliner_model.predict_entities(chunk_text, labels)
            for ent in chunk_ents:
                ent["start"] += char_offset
                ent["end"] += char_offset
            found_labels = [e["text"] for e in chunk_ents]
            print(f"      -> [GLiNER] Processing chunk {i}/{len(chunks)} - Found: {found_labels}")
            all_entities.extend(chunk_ents)
            
        unique_entities = {}
        for ent in all_entities:
            key = (ent["start"], ent["end"], ent["label"])
            if key not in unique_entities or ent["score"] > unique_entities[key]["score"]:
                unique_entities[key] = ent
                
        entities = list(unique_entities.values())
        
        for ent in entities:
            ent_label = ent["label"].upper()
            ent_text = ent["text"]
            
            if len(ent_text) <= 3 or ent_text.lower() in NER_BLOCKLIST:
                continue
                
            token_map = {"PERSON": "[PERSON]", "ORGANIZATION": "[ORG]", "ADDRESS": "[ADDRESS]", "COMPANY": "[BUSINESS_ID]", "FINANCIAL": "[FINANCIAL_STATUS]"}
            token_base = token_map.get(ent_label, f"[{ent_label}]")
            
            # Avoid duplicating structural candidates
            if any(c["value"].lower() == ent_text.lower() for c in candidates):
                continue
            
            # Context and UUID
            context = get_sentence_context(text, ent["start"], ent["end"])
            cand_id = f"cand_{uuid.uuid4().hex[:8]}"
            pre_decision = None
            
            if _allowlist_hit(ent_text.lower(), allowlist_lower):
                pre_decision = "SKIP"
                stats["skip_allowlist"] += 1
                
            stats["total"] += 1
            candidates.append({
                "id": cand_id,
                "type": ent_label,
                "value": ent_text,
                "context": context,
                "token_base": token_base,
                "pre_decision": pre_decision,
                "source": "GLINER"
            })
                
        # Contextual Date Sweeps
        for m in DATE_PAT.finditer(text):
            val = m.group()
            if val.lower() not in found and not any(c["value"].lower() == val.lower() for c in candidates):
                cand_id = f"cand_{uuid.uuid4().hex[:8]}"
                context = get_sentence_context(text, m.start(), m.end())
                stats["total"] += 1
                candidates.append({
                    "id": cand_id,
                    "type": "DATE",
                    "value": val,
                    "context": context,
                    "token_base": "[DATE]",
                    "pre_decision": None,
                    "source": "DATE_SWEEP"
                })

        # False Negative Sweep
        fn_triggers = [r"Tenant\s*name\s*[:\n]\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)",
                       r"Next\s*of\s*kin\s*[:\n]\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)",
                       r"Spouse\s*[:\n]\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)"]
        for pat in fn_triggers:
            for m in re.finditer(pat, text):
                val = m.group(1)
                if val.lower() not in found and not any(c["value"].lower() == val.lower() for c in candidates):
                    cand_id = f"cand_{uuid.uuid4().hex[:8]}"
                    context = get_sentence_context(text, m.start(1), m.end(1))
                    stats["total"] += 1
                    candidates.append({
                        "id": cand_id,
                        "type": "PERSON",
                        "value": val,
                        "context": context,
                        "token_base": "[PERSON]",
                        "pre_decision": None,
                        "source": "FALSE_NEGATIVE_SWEEP"
                    })
                    
        # Final explicit deduplication before batching
        candidates = deduplicate_candidates(candidates)
        
        # Apply Date and Financial Safe Default Override
        for c in candidates:
            # Only apply override if the LLM is normally going to see it (pre_decision is None)
            if c.get("pre_decision") is None:
                override = apply_date_financial_override(c, c.get("context", ""))
                if override:
                    c["pre_decision"] = override
                    if override == "SKIP":
                        print(f"        [DEBUG] SAFE_SKIP: {c['type']}='{c['value'][:30]}' | Context: '{c.get('context', '')[:50]}'")
                    
        # Batch to LLM
        if SLM_USE_OLLAMA and candidates:
            # Only send candidates to LLM that are undecided (pre_decision is None)
            llm_candidates = [c for c in candidates if c.get("pre_decision") is None]
            redact_ids = _call_ollama_batched(llm_candidates) if llm_candidates else []
            redact_set = set(redact_ids)
            
            for c in candidates:
                pre_dec = c.get("pre_decision")
                if pre_dec == "REDACT":
                    cand = get_candidate_token(c["token_base"], c["value"], secret_map, candidate_counters)
                    found[c["value"].lower()] = cand
                    stats["redact"] += 1
                    log_forensic_decision(c, "REDACT", "DETERMINISTIC_STRUCTURAL" if c.get("source") == "STRUCTURAL" else "PRE_DECISION_REDACT")
                elif pre_dec == "SKIP":
                    log_forensic_decision(c, "SKIP", "SAFE_DEFAULT_SKIP" if c.get("source") in ["DATE_SWEEP", "GLINER"] else "EXACT_MATCH_WHITELIST")
                else: # pre_decision is None, went to LLM
                    if c["id"] in redact_set:
                        cand = get_candidate_token(c["token_base"], c["value"], secret_map, candidate_counters)
                        found[c["value"].lower()] = cand
                        stats["redact"] += 1
                        log_forensic_decision(c, "REDACT", "LLM_CONTEXTUAL_REDACT")
                    else:
                        stats["skip_llm"] += 1
                        log_forensic_decision(c, "SKIP", "LLM_CONTEXTUAL_KEEP")
        else:
            # Fallback if no LLM
            for c in candidates:
                pre_dec = c.get("pre_decision")
                if pre_dec == "REDACT" or pre_dec is None:
                    cand = get_candidate_token(c["token_base"], c["value"], secret_map, candidate_counters)
                    found[c["value"].lower()] = cand
                    stats["redact"] += 1
                    log_forensic_decision(c, "REDACT", "FALLBACK_NO_LLM")
                else:
                    log_forensic_decision(c, "SKIP", "EXACT_MATCH_WHITELIST")

        # Context seed propagation — bare name-parts / de-suffixed org names
        # elsewhere in the document, derived from values already redacted above.
        propagate_context_seeds(text, candidates, found, secret_map, candidate_counters, stats)

    # Inject stats into the global dictionary if we are processing a full doc
    if not hasattr(scan_text_for_entities, "last_stats"):
        scan_text_for_entities.last_stats = []
    scan_text_for_entities.last_stats.append(stats)
                    
    return found


def apply_masking(text: str, global_entities: Dict[str, str], secret_map: dict, candidate_counters: dict) -> Tuple[str, List[Dict]]:
    """
    Mask a specific cell/string. Applies Presidio first, then global consistency entities.
    Returns (masked_text, list of applied audit events).
    """
    if not text or not str(text).strip():
        return text, []

    masked = str(text)
    events = []

    # 1. Presidio Pass (Local to this cell)
    # DECOMMISSIONED in Phase 2

    # 2. Global Consistency Pass — cheap substring pre-filter before the
    # (much more expensive) word-boundary regex: for the overwhelming
    # majority of (entity, cell) pairs the value simply isn't present at
    # all, and `in` is a fast C-level check vs. compiling+running a regex.
    # Only pairs that pass it pay for the regex, which then correctly avoids
    # partial-word matches (e.g. "Jon" inside "Jonathan") via \b boundaries.
    for val_lower, tok in global_entities.items():
        if len(val_lower) < 3:
            continue
        if val_lower not in masked.lower():
            continue
        pattern = rf'\b{re.escape(val_lower)}\b'
        match = re.search(pattern, masked, flags=re.IGNORECASE)
        if not match:
            continue
        new_masked = re.sub(pattern, tok, masked, flags=re.IGNORECASE)
        if new_masked != masked:
            events.append({'value': match.group(0), 'token': tok})
            masked = new_masked

    return masked, events


# ----------------------------------------------------------------------
# 7. Main Execution Flow (Docling)
# ----------------------------------------------------------------------
def _apply_token_map(df_redacted: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    """Replace every token occurrence in a redacted DataFrame with its original value."""
    # object dtype so every cell can hold a string, even in columns pandas
    # inferred as numeric (e.g. all-float "Age" column) — writing a str back
    # into a float64 column raises TypeError on pandas 2.2+ instead of upcasting.
    df_unmasked = df_redacted.astype(object)
    for i in range(df_unmasked.shape[0]):
        for j in range(df_unmasked.shape[1]):
            val = df_unmasked.iat[i, j]
            if pd.notna(val):
                val_str = str(val)
                for cand, orig in mapping.items():
                    if cand in val_str:
                        val_str = val_str.replace(cand, orig)
                df_unmasked.iat[i, j] = val_str
    return df_unmasked


def unmask_documents():
    """Legacy folder-scan rehydration: matches secrets/pii_secret_log_*.xlsx to
    redacted output by filename stem. Only works for files produced by an
    ad-hoc CLI run (no --manifest) — manifest-driven web uploads use
    unmask_file() / the DB instead.

    Unlike unmask_file(), this path has no DB row to read the true
    output_format from (ad-hoc runs have no manifest/DB attribution at all),
    so a redacted .xlsx is always treated as the flattened "Excel" format —
    the historical behaviour, unchanged. Unambiguous as-is formats (.docx,
    .doc, .pdf, .xls) are dispatched to unmask_as_is()."""
    print("=" * 60)
    print("Unmasking Redacted Documents")
    print("=" * 60)
    redacted_files = list(OUTPUT_REDACTED_FOLDER.glob("*_redacted.*"))
    if not redacted_files:
        print("No redacted files found to unmask.")
        return

    for red_path in redacted_files:
        stem = red_path.stem[:-len("_redacted")] if red_path.stem.endswith("_redacted") else red_path.stem
        secret_path = SECRETS_FOLDER / f"pii_secret_log_{stem}.xlsx"

        if not secret_path.exists():
            print(f"Skipping {red_path.name}: Secret file not found.")
            continue

        print(f"Unmasking: {red_path.name}")
        df_secret = pd.read_excel(secret_path, engine='openpyxl')
        mapping = {}
        if not df_secret.empty:
            for _, row in df_secret.iterrows():
                mapping[row['Candidate']] = str(row['Original Value'])

        if red_path.suffix.lower() == ".xlsx":
            unmasked_path = UNMASKED_FOLDER / f"{stem}_unmasked.xlsx"
            df_redacted = pd.read_excel(red_path, header=None, engine='openpyxl')
            df_unmasked = _apply_token_map(df_redacted, mapping) if mapping else df_redacted
            df_unmasked.to_excel(unmasked_path, index=False, header=False, engine='openpyxl')
        else:
            unmasked_path = UNMASKED_FOLDER / f"{stem}_unmasked{red_path.suffix.lower()}"
            try:
                unmask_as_is(red_path, unmasked_path, mapping)
            except Exception as e:
                print(f"  [ERROR] As-is rehydration failed for {red_path.name}: {e}")
                continue

        print(f"  -> Saved {unmasked_path.name}")


def unmask_file(file_id: str):
    """DB-backed rehydration for one file, looked up by its pii_files.file_id.
    Used by the web /rehydrate endpoint (and directly via --unmask-file).
    Dispatches on pii_files.output_format — "excel" always reads/writes flattened
    .xlsx; "as_is" restores the original file format via unmask_as_is()."""
    print("=" * 60)
    print(f"Rehydrating file: {file_id}")
    print("=" * 60)

    file_row = db.get_file(file_id)
    if not file_row:
        print(f"No file found for file_id={file_id}")
        return

    redacted_path = Path(file_row["redacted_path"]) if file_row["redacted_path"] else None
    if not redacted_path or not redacted_path.exists():
        print(f"Redacted output not found: {redacted_path}")
        return

    # mapping only ever contains tokens with exactly one distinct source
    # value — conflicting tokens (2+ distinct values, e.g. legacy rows from
    # before token allocation was job-scoped) are left out on purpose, so
    # every writer below leaves them in the output untouched as <*TOKEN*>
    # rather than guessing. Surfaced to the caller via a sidecar file since
    # this function has no return value (see the --unmask-file CLI path /
    # the web /rehydrate endpoint, which shells out to this script).
    mapping, conflicts = db.get_redaction_map(file_id=file_id)
    if conflicts:
        print(f"[WARNING] {len(conflicts)} token(s) left unresolved (ambiguous across files): "
              f"{[c['token'] for c in conflicts]}")
        UNMASKED_FOLDER.mkdir(parents=True, exist_ok=True)
        conflicts_path = UNMASKED_FOLDER / f"{file_id}_rehydrate_conflicts.json"
        conflicts_path.write_text(json.dumps({"conflicts": conflicts}), encoding="utf-8")

    stem = Path(file_row["filename"]).stem
    output_format = file_row.get("output_format", "excel")

    if output_format == "as_is":
        unmasked_path = UNMASKED_FOLDER / f"{stem}_unmasked{redacted_path.suffix.lower()}"
        try:
            if redacted_path.suffix.lower() in IMAGE_EXTENSIONS:
                # Pixel redaction leaves no searchable token text, so this
                # restores the securely stored original instead of the
                # generic token-replace path used by every other format.
                unmask_image_as_is(file_row, unmasked_path)
            else:
                unmask_as_is(redacted_path, unmasked_path, mapping)
        except Exception as e:
            print(f"[ERROR] As-is rehydration failed for {file_id}: {e}")
            return
    else:
        unmasked_path = UNMASKED_FOLDER / f"{stem}_unmasked.xlsx"
        df_redacted = pd.read_excel(str(redacted_path), header=None, engine='openpyxl')
        df_unmasked = _apply_token_map(df_redacted, mapping) if mapping else df_redacted
        df_unmasked.to_excel(str(unmasked_path), index=False, header=False, engine='openpyxl')

    print(f"  -> Saved {unmasked_path.name}")


# ----------------------------------------------------------------------
# Column-alignment fix helpers
# ----------------------------------------------------------------------
_MONEY_PAT_ALIGN = re.compile(r'^\$[\d,]+\.?\d*$')
_AMOUNT_DEST_KW  = frozenset({'amount', 'value', 'total', 'sum', 'price', 'fee', 'income', 'gross', 'cost'})
_AMOUNT_SRC_EXCL = frozenset({'amount', 'value', 'total', 'sum', 'price', 'fee', 'income', 'gross',
                               'cost', 'tax', 'withheld', 'paid', 'label'})


def _fix_docling_table_alignment(df):
    """
    Correct systematic left-shift in Docling/PyMuPDF table output.

    Docling sometimes packs data cells into the leftmost available columns,
    skipping intentionally-empty intermediate columns (e.g. a blank 'Type'
    column).  This function detects the pattern:

      header_col N  → 'Amount' (or similar amount label), ALL data rows empty
      header_col N-1 → 'Type' (or anything non-amount),  data rows have $values

    and moves the misplaced $values from col N-1 into col N.
    Only applied when the DataFrame has named (non-integer) columns.
    """
    if df.empty or df.shape[1] < 2:
        return df
    if all(isinstance(c, int) for c in df.columns):
        return df

    df = df.copy()
    col_names = list(df.columns)

    for col_pos in range(len(col_names) - 1, 0, -1):
        col_name = str(col_names[col_pos]).strip()
        if not col_name or col_name.lower() == 'nan':
            continue

        # This column must look like an amount destination
        if not any(kw in col_name.lower() for kw in _AMOUNT_DEST_KW):
            continue

        # This column must be entirely empty in all data rows
        if not df.iloc[:, col_pos].apply(lambda x: pd.isna(x) or str(x).strip() == '').all():
            continue

        # Preceding column must have at least one $-money value
        prev_pos = col_pos - 1
        prev_vals = df.iloc[:, prev_pos].apply(lambda x: str(x).strip() if pd.notna(x) else '')
        if not prev_vals.apply(lambda v: bool(_MONEY_PAT_ALIGN.match(v))).any():
            continue

        # Preceding column header must NOT itself be an amount-type
        prev_name = str(col_names[prev_pos]).strip().lower()
        if any(kw in prev_name for kw in _AMOUNT_SRC_EXCL):
            continue

        # Apply fix: shift $values right one column, clear the source column
        df.iloc[:, col_pos] = df.iloc[:, prev_pos]
        df.iloc[:, prev_pos] = None

    return df


def _fix_raw_excel_alignment(df):
    """
    Correct column shifts that are already baked into a raw Excel file.

    Scans for rows where:
      • A cell at position K contains the word 'Amount' (the header row)
      • Subsequent data rows have a $-value at position K-1 and NaN at K

    Moves the $-value from K-1 to K for each affected data row.
    This repairs raw Excel files that were created before the Phase-1 fix.
    """
    if df.empty or df.shape[1] < 2:
        return df

    AMOUNT_LABEL = re.compile(r'\bamount\b', re.IGNORECASE)
    df = df.astype(object).copy()   # keep strings intact when assigning across dtype=float cols
    n_rows, n_cols = df.shape
    i = 0

    while i < n_rows - 1:
        row = df.iloc[i]
        non_nan = [(j, str(v).strip()) for j, v in enumerate(row)
                   if pd.notna(v) and str(v).strip() and str(v).strip().lower() != 'nan']

        if len(non_nan) < 2:
            i += 1
            continue

        # Find an "Amount" label in this row
        amount_pos = next((j for j, v in non_nan if AMOUNT_LABEL.search(v)), None)
        if amount_pos is None or amount_pos == 0:
            i += 1
            continue

        prev_pos = amount_pos - 1
        shift_rows = []
        no_match = 0
        j = i + 1

        while j < n_rows:
            drow = df.iloc[j]
            d_non_nan = [(k, str(v).strip()) for k, v in enumerate(drow)
                         if pd.notna(v) and str(v).strip() and str(v).strip().lower() != 'nan']

            # Stop when we hit another header-density row
            if len(d_non_nan) >= len(non_nan):
                break

            val_at_amount = str(df.iloc[j, amount_pos]).strip() if amount_pos < n_cols else ''
            val_at_prev   = str(df.iloc[j, prev_pos]).strip()   if prev_pos  < n_cols else ''

            if (not val_at_amount or val_at_amount.lower() == 'nan') \
                    and _MONEY_PAT_ALIGN.match(val_at_prev):
                shift_rows.append(j)
                no_match = 0
            else:
                no_match += 1
                if no_match >= 3:
                    break

            j += 1

        if shift_rows:
            print(f"    [ColFix] Shifting {len(shift_rows)} row(s): "
                  f"col {prev_pos} -> col {amount_pos} (header at row {i})")
            for r in shift_rows:
                df.iloc[r, amount_pos] = df.iloc[r, prev_pos]
                df.iloc[r, prev_pos]   = None

        i = j if j > i + 1 else i + 1

    return df


def _docling_grid_to_dataframe(item):
    """
    Build a DataFrame using each TableCell's start_row/col_offset_idx metadata,
    which preserves genuinely empty columns that export_to_dataframe() discards
    by left-packing all non-empty values.

    Strategy: iterate item.data.table_cells (flat list, always populated) and
    place each cell's text at its declared (row, col) position in a full
    n_rows × n_cols matrix. None entries stay None → empty Excel cells.
    """
    try:
        data = getattr(item, 'data', None)
        if data is None:
            return None

        n_cols = getattr(data, 'num_cols', 0)
        n_rows_total = getattr(data, 'num_rows', 0)
        table_cells = getattr(data, 'table_cells', None) or []

        if not table_cells or n_cols == 0 or n_rows_total == 0:
            return None

        # Full n_rows × n_cols matrix; empty cells stay None
        text_matrix = [[None] * n_cols for _ in range(n_rows_total)]
        col_header_row = -1

        for cell in table_cells:
            r = getattr(cell, 'start_row_offset_idx', None)
            c = getattr(cell, 'start_col_offset_idx', None)
            if r is None or c is None or r >= n_rows_total or c >= n_cols:
                continue
            cell_text = (getattr(cell, 'text', '') or '').strip()
            if cell_text:
                text_matrix[r][c] = cell_text
            if getattr(cell, 'column_header', False):
                col_header_row = max(col_header_row, r)

        if col_header_row >= 0:
            col_names = [
                (text_matrix[col_header_row][c] or f'col_{c}')
                for c in range(n_cols)
            ]
            data_rows = text_matrix[col_header_row + 1:]
        else:
            col_names = list(range(n_cols))
            data_rows = text_matrix

        if not data_rows:
            return None

        df = pd.DataFrame(data_rows, columns=col_names)
        print(f"      [Grid] Built {df.shape[0]}×{df.shape[1]} DataFrame "
              f"from cell positions ({len(table_cells)} cells, {n_cols} cols)")
        return df

    except Exception as e:
        print(f"      [Grid] Error: {e}")
        return None


def pdf_to_excel_docling(pdf_path, output_xlsx_path):
    """Convert PDF to structured Excel using Granite Docling (GPU) or Hybrid CPU fallback."""
    import unicodedata
    import torch

    def _normalize(text):
        if not text: return ""
        return unicodedata.normalize('NFKC', str(text)).strip()

    print(f"    [Phase 1] Initializing extraction for {pdf_path.name}...")
    
    # 1. Open with PyMuPDF just to get page count
    try:
        pdf_doc = fitz.open(str(pdf_path))
        total_pages = pdf_doc.page_count
        pdf_doc.close()
    except Exception as e:
        print(f"    Failed to open PDF with PyMuPDF: {e}")
        pd.DataFrame().to_excel(str(output_xlsx_path), index=False, header=False, engine='openpyxl')
        return

    # 2. Decide Pipeline (Granite VLM on GPU vs Heron on CPU)
    use_vlm = torch.cuda.is_available()
    converter = None

    raw_excel_rows_data = []
    current_excel_row = 0
    table_count = 0
    text_count = 0

    if use_vlm:
        try:
            from docling.document_converter import DocumentConverter, PdfFormatOption
            from docling.datamodel.base_models import InputFormat
            from docling.pipeline.vlm_pipeline import VlmPipeline, VlmPipelineOptions
            from docling.models.docling_model_provider import vlm_model_specs
            
            print("    [Phase 1] GPU detected. Using Granite-Docling 258M VLM Pipeline...")
            pipeline_options = VlmPipelineOptions(vlm_options=vlm_model_specs.GRANITEDOCLING_TRANSFORMERS)
            converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(
                        pipeline_cls=VlmPipeline,
                        pipeline_options=pipeline_options,
                    ),
                }
            )
        except Exception as e:
            print(f"    [WARNING] Failed to load VLM Pipeline (Dependencies/OOM): {e}")
            print("    Falling back to CPU Layout Heron pipeline...")
            use_vlm = False
            
    if not use_vlm:
        print("    [Phase 1] Using CPU. Initializing Docling Layout Heron OCR...")
        try:
            from docling.document_converter import DocumentConverter, PdfFormatOption
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
            pipeline_options = PdfPipelineOptions(do_table_structure=True)
            pipeline_options.table_structure_options.mode = TableFormerMode.ACCURATE
            pipeline_options.table_structure_options.do_cell_matching = True
            pipeline_options.do_ocr = True
            converter = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
            )
        except Exception as e:
            print(f"    [Phase 1] Docling CPU initialization FAILED: {e}")
            pd.DataFrame().to_excel(str(output_xlsx_path), index=False, header=False, engine='openpyxl')
            return
    
    # 3. Process with Docling (VLM or Heron)
    from docling_core.types.doc.labels import DocItemLabel
    CHUNK_SIZE = 3
    
    for start_page in range(1, total_pages + 1, CHUNK_SIZE):
        end_page = min(start_page + CHUNK_SIZE - 1, total_pages)
        print(f"        -> Processing pages {start_page} to {end_page} of {total_pages}...")
        
        try:
            result = converter.convert(str(pdf_path), page_range=(start_page, end_page))
            doc = result.document
        except Exception as e:
            print(f"    [Phase 1] Docling conversion FAILED for pages {start_page}-{end_page}: {e}")
            if use_vlm:
                print("    [ERROR] VLM Pipeline crashed (possibly OOM). Cannot fallback safely mid-document. Returning empty.")
            continue
            
        for item, _ in doc.iterate_items():
            if item.label == DocItemLabel.TABLE:
                try:
                    df_table = _docling_grid_to_dataframe(item)
                    if df_table is None:
                        df_table = item.export_to_dataframe(doc=doc)
                    df_table = _fix_docling_table_alignment(df_table)
                    if df_table.empty: raise ValueError("Empty table")
                    if not all(isinstance(c, int) for c in df_table.columns):
                        header_row = {'excel_row': current_excel_row}
                        for col_idx, col_name in enumerate(df_table.columns):
                            normalized_col = _normalize(str(col_name))
                            if normalized_col:
                                header_row[col_idx] = normalized_col
                        if len(header_row) > 1:  # Only add header if it has actual content
                            raw_excel_rows_data.append(header_row)
                            current_excel_row += 1

                    for _, row in df_table.iterrows():
                        row_dict = {'excel_row': current_excel_row}
                        for col_idx, val in enumerate(row):
                            cell_val = _normalize(str(val)) if pd.notna(val) else ""
                            cell_val = cell_val.replace('\n', ' ').replace('\r', '')
                            if cell_val: row_dict[col_idx] = cell_val
                        if len(row_dict) > 1:  # Only add row if it has actual cell data
                            raw_excel_rows_data.append(row_dict)
                            current_excel_row += 1
                    table_count += 1
                except Exception as e:
                    fallback_text = getattr(item, 'text', '') or ''
                    for line in fallback_text.strip().splitlines():
                        line = _normalize(line)
                        if line:
                            raw_excel_rows_data.append({'excel_row': current_excel_row, 0: line})
                            current_excel_row += 1
            elif hasattr(item, 'text') and item.text and item.text.strip():
                text = _normalize(item.text)
                for line in text.splitlines():
                    line = line.strip()
                    if line:
                        raw_excel_rows_data.append({'excel_row': current_excel_row, 0: line})
                        current_excel_row += 1
                        text_count += 1

    print(f"    Docling extracted {table_count} tables, {text_count} text lines")

    df_raw = pd.DataFrame(raw_excel_rows_data)
    
    # Verify Docling data has ACTUAL content (not just empty/phantom rows)
    has_real_content = False
    if not df_raw.empty:
        cols = [c for c in df_raw.columns if c != 'excel_row']
        cols = sorted(cols)
        if cols:  # Must have at least one data column
            df_raw = df_raw[cols]
            # Check if any cell in the entire DataFrame is non-empty
            has_real_content = df_raw.apply(lambda row: row.astype(str).str.strip().str.len().sum() > 0, axis=1).any()
    
    if has_real_content:
        df_raw.to_excel(str(output_xlsx_path), index=False, header=False, engine='openpyxl')
        print(f"    Saved Raw Excel: {output_xlsx_path.name} ({len(df_raw)} rows)")
    else:
        # ============================================================
        # TIER 2: PyMuPDF OCR + find_tables(strategy="text")
        # ============================================================
        print(f"    [WARNING] Docling extracted 0 usable data. Falling back to PyMuPDF OCR...")
        pymupdf_rows = []
        current_row = 0
        tables_found = 0
        tier_used = "PyMuPDF"

        try:
            doc = fitz.open(str(pdf_path))
            for page_num, page in enumerate(doc):
                page_label = f"Page {page_num + 1}/{doc.page_count}"

                # Step 1: Check for native text
                native_text = page.get_text().strip()

                # Step 2: If no native text, inject OCR text layer
                if not native_text:
                    try:
                        page.get_text("ocr")
                        print(f"        [{page_label}] PyMuPDF OCR injected text layer.")
                    except Exception as ocr_err:
                        print(f"        [{page_label}] PyMuPDF OCR unavailable: {ocr_err}")

                # Step 3: Try geometric table detection on native or OCR'd text
                try:
                    tabs = page.find_tables(
                        strategy="text",
                        snap_tolerance=5,
                        join_tolerance=5,
                        edge_min_length=3,
                    )
                except Exception:
                    tabs = None

                if tabs and tabs.tables:
                    for tab in tabs.tables:
                        tables_found += 1
                        try:
                            df = tab.to_pandas()
                            df = _fix_docling_table_alignment(df)
                        except Exception:
                            continue

                        # Add headers (only if they have content)
                        header_dict = {'excel_row': current_row}
                        for col_idx, col_name in enumerate(df.columns):
                            normalized_col = _normalize(str(col_name))
                            if normalized_col:
                                header_dict[col_idx] = normalized_col
                        if len(header_dict) > 1:
                            pymupdf_rows.append(header_dict)
                            current_row += 1

                        # Add data rows (only if they have content)
                        for _, row in df.iterrows():
                            row_dict = {'excel_row': current_row}
                            for col_idx, cell in enumerate(row):
                                cell_val = _normalize(str(cell)) if pd.notna(cell) else ""
                                cell_val = cell_val.replace('\n', ' ').replace('\r', '')
                                if cell_val:
                                    row_dict[col_idx] = cell_val
                            if len(row_dict) > 1:
                                pymupdf_rows.append(row_dict)
                                current_row += 1

                    print(f"        [{page_label}] Extracted {tables_found} table(s) via find_tables.")
                else:
                    # No tables found — try plain text lines
                    text = page.get_text()
                    if text and text.strip():
                        for line in text.splitlines():
                            line = _normalize(line.strip())
                            if line:
                                pymupdf_rows.append({'excel_row': current_row, 0: line})
                                current_row += 1
                        print(f"        [{page_label}] Extracted {current_row} text line(s).")
                    else:
                        print(f"        [{page_label}] No text found by PyMuPDF.")

            doc.close()
        except Exception as e:
            import traceback
            print(f"    [ERROR] PyMuPDF Tier 2 failed: {e}")
            traceback.print_exc()

        # ============================================================
        # TIER 3: pytesseract with coordinate-based column alignment
        # Uses image_to_data() to get word pixel positions, then assigns each
        # word to the nearest column anchor so empty columns stay empty.
        # ============================================================
        if not pymupdf_rows:
            print(f"    [WARNING] PyMuPDF extracted 0 rows. Falling back to pytesseract OCR...")
            tier_used = "pytesseract"
            try:
                import pytesseract
                from PIL import Image
                import io as io_module
                import shutil
                _tess = shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
                pytesseract.pytesseract.tesseract_cmd = _tess

                doc = fitz.open(str(pdf_path))
                for page_num, page in enumerate(doc):
                    page_label = f"Page {page_num + 1}/{doc.page_count}"
                    pix = page.get_pixmap(dpi=300)
                    img = Image.open(io_module.BytesIO(pix.tobytes("png")))

                    # Use image_to_data to get per-word bounding boxes
                    tsv = pytesseract.image_to_data(
                        img,
                        config='--psm 6',
                        output_type=pytesseract.Output.DICT
                    )

                    # Collect valid words with their pixel centres
                    words = []
                    for i in range(len(tsv['text'])):
                        txt = (tsv['text'][i] or '').strip()
                        try:
                            conf = int(tsv['conf'][i])
                        except (ValueError, TypeError):
                            conf = -1
                        if conf > 20 and txt:
                            cx = tsv['left'][i] + tsv['width'][i] // 2
                            cy = tsv['top'][i] + tsv['height'][i] // 2
                            words.append({'text': txt, 'cx': cx, 'cy': cy,
                                          'left': tsv['left'][i]})

                    page_lines = 0
                    if words:
                        # Group words into text lines by cy (±12px tolerance)
                        words.sort(key=lambda w: w['cy'])
                        text_lines = []
                        Y_TOL = 12
                        for w in words:
                            placed = False
                            for tl in text_lines:
                                if abs(w['cy'] - tl['cy']) <= Y_TOL:
                                    tl['words'].append(w)
                                    # keep median cy
                                    tl['cy'] = sum(x['cy'] for x in tl['words']) // len(tl['words'])
                                    placed = True
                                    break
                            if not placed:
                                text_lines.append({'cy': w['cy'], 'words': [w]})

                        text_lines.sort(key=lambda tl: tl['cy'])

                        # Build column anchor list from x-position clusters across all lines.
                        # Cluster: if two cx values are within COL_MERGE_PX, merge them.
                        all_cx = sorted(w['cx'] for tl in text_lines for w in tl['words'])
                        COL_MERGE_PX = 60
                        col_anchors = []
                        for cx in all_cx:
                            if col_anchors and cx - col_anchors[-1] <= COL_MERGE_PX:
                                # Merge: shift anchor to midpoint
                                col_anchors[-1] = (col_anchors[-1] + cx) // 2
                            else:
                                col_anchors.append(cx)

                        def nearest_col(cx):
                            return min(range(len(col_anchors)),
                                       key=lambda i: abs(col_anchors[i] - cx))

                        for tl in text_lines:
                            tl['words'].sort(key=lambda w: w['left'])
                            row_cells = {}
                            for w in tl['words']:
                                ci = nearest_col(w['cx'])
                                if ci in row_cells:
                                    row_cells[ci] += ' ' + w['text']
                                else:
                                    row_cells[ci] = w['text']

                            # Filter OCR junk (pure punctuation/digits, short)
                            clean = {ci: _normalize(v.strip())
                                     for ci, v in row_cells.items()
                                     if v.strip() and not (
                                         re.match(r'^[\d\W]+$', v.strip())
                                         and len(v.strip()) < 5)}
                            if not clean:
                                continue

                            row_dict = {'excel_row': current_row}
                            row_dict.update(clean)
                            pymupdf_rows.append(row_dict)
                            current_row += 1
                            page_lines += 1

                    print(f"        [{page_label}] pytesseract extracted {page_lines} line(s).")
                doc.close()
            except Exception as e:
                import traceback
                print(f"    [ERROR] pytesseract Tier 3 failed: {e}")
                traceback.print_exc()

        # ============================================================
        # Save result (Tier 2 or Tier 3)
        # ============================================================
        df_fallback = pd.DataFrame(pymupdf_rows)
        if not df_fallback.empty:
            cols = [c for c in df_fallback.columns if c != 'excel_row']
            cols = sorted(cols)
            df_fallback = df_fallback[cols]
            df_fallback.to_excel(str(output_xlsx_path), index=False, header=False, engine='openpyxl')
            print(f"    Saved {tier_used} Fallback Excel: {output_xlsx_path.name} ({len(df_fallback)} rows, {tables_found} tables)")
        else:
            pd.DataFrame().to_excel(str(output_xlsx_path), index=False, header=False, engine='openpyxl')
            print(f"    Saved Empty Raw Excel: {output_xlsx_path.name}")


def _image_words(image_path: Path):
    """
    OCR a standalone image file with Tesseract and return (words, orig_img, inv):
      - words: (x0, y0, x1, y1, text) boxes in the ORIGINAL image's pixel
        space — if OCR ran on a downsized copy (per MAX_IMG_SIDE), boxes are
        scaled back up, mirroring the DPI scale-back in _pdf_page_words().
      - orig_img: the loaded, EXIF-rotated, RGB-converted PIL Image at full
        resolution (used by both the extractor and the "as is" writer so
        redaction boxes line up and the saved output isn't downscaled).
      - inv: the scale-back factor applied to the boxes (1.0 if no resize
        happened) — callers need this to scale pixel-distance tolerances
        (e.g. line-grouping y_tol) into original-image space too.
    """
    import pytesseract
    import shutil
    tess_cmd = shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    pytesseract.pytesseract.tesseract_cmd = tess_cmd

    orig_img = Image.open(str(image_path))
    orig_img = ImageOps.exif_transpose(orig_img)
    if orig_img.mode != "RGB":
        orig_img = orig_img.convert("RGB")

    ocr_img = orig_img
    scale = 1.0
    if max(orig_img.size) > MAX_IMG_SIDE:
        scale = MAX_IMG_SIDE / max(orig_img.size)
        new_size = (max(1, int(orig_img.width * scale)), max(1, int(orig_img.height * scale)))
        ocr_img = orig_img.resize(new_size, Image.LANCZOS)

    tsv = pytesseract.image_to_data(ocr_img, config='--psm 6', output_type=pytesseract.Output.DICT)
    inv = 1.0 / scale
    words = []
    for i in range(len(tsv['text'])):
        txt = (tsv['text'][i] or '').strip()
        try:
            conf = int(tsv['conf'][i])
        except (ValueError, TypeError):
            conf = -1
        if conf > 20 and txt:
            x0 = tsv['left'][i] * inv
            y0 = tsv['top'][i] * inv
            x1 = (tsv['left'][i] + tsv['width'][i]) * inv
            y1 = (tsv['top'][i] + tsv['height'][i]) * inv
            words.append((x0, y0, x1, y1, txt))
    return words, orig_img, inv


def _split_line_into_fields(line_words, gap_px: float):
    """
    Split one line's words into "fields" (a field = one label or one value,
    e.g. all of "Agent Ref Number" merged together) by looking at the gap
    between consecutive word boxes: a normal inter-word space stays merged
    into the current field, a wide gap starts a new field. line_words must
    already be sorted left-to-right (as returned by _group_words_into_lines()).

    This exists so column detection can cluster field positions instead of
    individual word positions — clustering raw per-word x-centres across a
    whole multi-column row lets a chain of moderately-spaced words collapse
    unrelated columns into one (e.g. "Tax Contact | Agent Ref Number |
    Contact Number" collapsing into a single garbled cell), because nothing
    stops the merge from "walking" across a column boundary one word at a
    time. Pre-merging within-field words first removes that chaining path.
    """
    fields = []
    cur = None
    for w in line_words:
        if cur is None:
            cur = {'x0': w[0], 'x1': w[2], 'text': w[4]}
        elif w[0] - cur['x1'] <= gap_px:
            cur['x1'] = max(cur['x1'], w[2])
            cur['text'] += ' ' + w[4]
        else:
            fields.append(cur)
            cur = {'x0': w[0], 'x1': w[2], 'text': w[4]}
    if cur is not None:
        fields.append(cur)
    return fields


def image_to_excel_ocr(image_path: Path, output_xlsx_path: Path) -> None:
    """
    Phase 1 extraction for standalone image files (PNG/JPG/JPEG). There's no
    layout engine like Docling for a bare image, so this uses a pytesseract
    word-box + two-pass column-detection approach: words are grouped into
    lines, each line is split into fields on wide gaps (_split_line_into_fields,
    which avoids the column-chaining bug a naive per-word clustering has —
    see its docstring), and only then are field START positions clustered
    into column anchors *across* lines (left-aligned columns share an x0,
    even though a label like "Agent Ref Number" and its value "24963161"
    have very different widths/centres). The result is a DataFrame shaped
    like every other Phase 1 output (dict-of-columns per row) that
    scan_text_for_entities()'s structural-label tier can use.
    """
    import unicodedata

    def _normalize(text):
        if not text: return ""
        return unicodedata.normalize('NFKC', str(text)).strip()

    print(f"    [Phase 1] OCR extracting: {image_path.name}")
    try:
        words, _, inv = _image_words(image_path)
    except Exception as e:
        import traceback
        print(f"    [ERROR] Image OCR failed: {e}")
        traceback.print_exc()
        words, inv = [], 1.0

    rows = []
    current_row = 0
    if words:
        lines = _group_words_into_lines(words, y_tol=12.0 * inv)
        lines.sort(key=lambda l: l['cy'])

        FIELD_GAP_PX = 40 * inv
        line_fields = [_split_line_into_fields(line['words'], FIELD_GAP_PX) for line in lines]

        all_x0 = sorted(f['x0'] for fields in line_fields for f in fields)
        COL_MERGE_PX = 60 * inv
        col_anchors = []
        for x0 in all_x0:
            if col_anchors and x0 - col_anchors[-1] <= COL_MERGE_PX:
                col_anchors[-1] = (col_anchors[-1] + x0) / 2
            else:
                col_anchors.append(x0)

        def nearest_col(x0):
            return min(range(len(col_anchors)), key=lambda i: abs(col_anchors[i] - x0))

        for fields in line_fields:
            row_cells = {}
            for f in fields:
                ci = nearest_col(f['x0'])
                row_cells[ci] = (row_cells[ci] + ' ' + f['text']) if ci in row_cells else f['text']

            clean = {ci: _normalize(v.strip()) for ci, v in row_cells.items()
                     if v.strip() and not (re.match(r'^[\d\W]+$', v.strip()) and len(v.strip()) < 5)}
            if not clean:
                continue
            row_dict = {'excel_row': current_row}
            row_dict.update(clean)
            rows.append(row_dict)
            current_row += 1

    df = pd.DataFrame(rows)
    if not df.empty:
        cols = sorted(c for c in df.columns if c != 'excel_row')
        df = df[cols]
        df.to_excel(str(output_xlsx_path), index=False, header=False, engine='openpyxl')
        print(f"    Saved OCR Raw Excel: {output_xlsx_path.name} ({len(df)} rows)")
    else:
        pd.DataFrame().to_excel(str(output_xlsx_path), index=False, header=False, engine='openpyxl')
        print(f"    Saved Empty Raw Excel (no OCR text found): {output_xlsx_path.name}")


_REDACTION_FONT_CACHE = {}


def _load_redaction_font(size: int):
    """Best-effort TrueType font for burning tokens into a redacted image;
    falls back to PIL's bundled default font (scalable since Pillow 10.1)
    if no system font is available."""
    if size in _REDACTION_FONT_CACHE:
        return _REDACTION_FONT_CACHE[size]
    try:
        font = ImageFont.truetype("arial.ttf", size)
    except Exception:
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:
            font = ImageFont.load_default()
    _REDACTION_FONT_CACHE[size] = font
    return font


def write_redacted_image_as_is(input_path: Path, output_path: Path, global_entities: dict,
                                secret_map: dict, candidate_counters: dict) -> list:
    """
    Visually redact a standalone image (PNG/JPG/JPEG): OCR word boxes, locate
    each PII value's exact position using the same _group_words_into_lines()/
    _find_spans_in_line() helpers as write_redacted_pdf_as_is(), then draw an
    opaque box with the replacement token burned on top via PIL.

    Unlike the PDF/DOCX/XLSX "as is" writers, this can't edit content in
    place — an image has no text layer, so this permanently overwrites the
    pixels under each match. That's why the original is separately preserved
    (see _store_original_encrypted()) for rehydration.
    """
    audit_records = []
    ts = time.strftime('%Y-%m-%dT%H:%M:%S')
    filename = input_path.name
    needles = sorted(global_entities.items(), key=lambda kv: -len(kv[0]))

    words, img, inv = _image_words(input_path)
    draw = ImageDraw.Draw(img)

    if words:
        lines = _group_words_into_lines(words, y_tol=12.0 * inv)
        matches = []
        for line in lines:
            for m in _find_spans_in_line(line['words'], needles):
                matches.append((m, line['words']))

        def _measure(t, fs):
            f = _load_redaction_font(max(1, int(round(fs))))
            try:
                return draw.textlength(t, font=f)
            except Exception:
                return fs * len(t) * 0.6

        for (bbox, matched_text, token), line_words in matches:
            x0, y0, x1, y1 = bbox
            box_h = y1 - y0
            base_fontsize = max(10, min(28, int(box_h * 0.8)))
            # The token label (e.g. "<*PERSON_1*>") can be longer than the
            # original matched text it replaces, so widen the box around its own
            # centre rather than clipping the label to the original span — but
            # cap that expansion to the gap to the nearest neighboring word on
            # the same line (shrinking the font first) so it never overwrites
            # part of an adjacent, unmatched word's pixels.
            max_half_w = _max_half_width_for_line(x0, x1, line_words, pad=2.0)
            fontsize, text_w, half_w = _shrink_to_fit(
                token, x1 - x0, base_fontsize, 6, max_half_w, gap=8, measure_fn=_measure, step=1,
            )
            font = _load_redaction_font(max(1, int(round(fontsize))))
            box_w = half_w * 2
            cx = (x0 + x1) / 2
            bx0, bx1 = cx - box_w / 2, cx + box_w / 2
            draw.rectangle([bx0, y0, bx1, y1], fill=(255, 255, 255))
            tx = bx0 + max(0, (box_w - text_w) / 2)
            ty = y0 + max(0, (box_h - fontsize) / 2)
            draw.text((tx, ty), token, fill=(0, 0, 0), font=font)
            audit_records.append(_make_audit_event(
                ts, filename, {'token': token, 'value': matched_text}, page_num=1))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    save_kwargs = {'quality': 92} if output_path.suffix.lower() in ('.jpg', '.jpeg') else {}
    img.save(str(output_path), **save_kwargs)
    return audit_records


def _store_original_encrypted(file_id: str, input_path: Path) -> Path:
    """Encrypt and store the original image bytes so /rehydrate can restore
    them exactly — image "as is" redaction burns black boxes directly into
    pixels, so (unlike PDF/DOCX/XLSX) there's no token text layer left to
    search-and-replace on rehydration."""
    enc_path = ORIGINALS_FOLDER / f"{file_id}{input_path.suffix.lower()}.enc"
    enc_path.write_bytes(crypto_utils.encrypt_bytes(input_path.read_bytes()))
    return enc_path


def unmask_image_as_is(file_row: dict, unmasked_path: Path) -> None:
    """Image "as is" rehydration: since pixel redaction leaves no searchable
    token text (unlike unmask_pdf_as_is/unmask_docx_as_is/etc.), this just
    decrypts and restores the original bytes saved by _store_original_encrypted()."""
    enc_path = file_row.get("original_encrypted_path")
    if not enc_path or not Path(enc_path).exists():
        raise RuntimeError(f"No stored original found for file_id={file_row['file_id']}")
    original_bytes = crypto_utils.decrypt_bytes(Path(enc_path).read_bytes())
    unmasked_path.parent.mkdir(parents=True, exist_ok=True)
    unmasked_path.write_bytes(original_bytes)


def convert_doc_to_docx(doc_path: Path, output_docx_path: Path) -> bool:
    """
    Converts a legacy binary .doc file to .docx via MS Word COM automation.
    python-docx (used by docx_to_excel) can only read the newer OOXML .docx
    format, not the old binary format, so .doc has to be upconverted first.
    Requires Microsoft Word to be installed on this machine.
    Returns True on success, False if Word/COM isn't available or the file
    can't be opened — caller should treat that as an extraction failure.
    """
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    word = None
    try:
        word = win32com.client.gencache.EnsureDispatch("Word.Application")
        word.Visible = False
        wdoc = word.Documents.Open(str(doc_path.resolve()), ReadOnly=True)
        try:
            wdoc.SaveAs2(str(output_docx_path.resolve()), FileFormat=12)  # wdFormatXMLDocument
        finally:
            wdoc.Close(False)
        return True
    except Exception as e:
        print(f"    [ERROR] Word COM conversion failed for {doc_path.name}: {e}")
        return False
    finally:
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def spreadsheet_to_excel(input_path: Path, output_xlsx_path: Path):
    """
    Phase 1 for already-tabular uploads (.xlsx/.xls).
    Normalises into the same header-less 'Raw Excel' shape Phase 2 expects, so
    spreadsheet uploads go through the same scan_text_for_entities()/apply_masking()
    path as PDFs and Word docs instead of being silently dropped.
    """
    print(f"    [Phase 1] Reading spreadsheet: {input_path.name}")
    engine = 'xlrd' if input_path.suffix.lower() == '.xls' else 'openpyxl'
    try:
        # sheet_name=None reads EVERY worksheet (dict of {name: DataFrame});
        # the pandas default (sheet_name=0) silently dropped every tab after the
        # first, so multi-tab uploads only got their first page redacted.
        sheets = pd.read_excel(str(input_path), header=None, sheet_name=None, engine=engine)
    except Exception as e:
        print(f"    [ERROR] Failed to read {input_path.name}: {e}")
        sheets = {}

    # Flatten all sheets into one header-less table (this mode's contract is a
    # single flat sheet). Prefix each sheet's block with a label row so the
    # origin of each tab stays visible and the scanner still sees plain text.
    blocks = []
    for sheet_name, sheet_df in sheets.items():
        if sheet_df.empty or not sheet_df.notna().any().any():
            print(f"    [WARNING] Sheet '{sheet_name}' in {input_path.name} has no usable content.")
            continue
        # Leading char must not be = + - @ or Excel/openpyxl treats the cell as
        # a formula and the label is lost on write-back.
        label = pd.DataFrame([[f"[Sheet: {sheet_name}]"]])
        blocks.append(label)
        blocks.append(sheet_df)

    if blocks:
        df = pd.concat(blocks, ignore_index=True)
    else:
        df = pd.DataFrame()
        print(f"    [WARNING] {input_path.name} has no usable content.")

    df.to_excel(str(output_xlsx_path), index=False, header=False, engine='openpyxl')
    print(f"    Saved Raw Excel: {output_xlsx_path.name} ({len(df)} rows)")


def _iter_docx_block_items(document: DocxDocument):
    """Yield paragraphs and tables from a docx Document body in document order."""
    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            yield DocxParagraph(child, document)
        elif isinstance(child, CT_Tbl):
            yield DocxTable(child, document)


def docx_to_excel(docx_path: Path, output_xlsx_path: Path):
    """
    Phase 1 for Word uploads (.docx).
    Flattens headers, footers, paragraphs, and tables (in document order) into the
    same row/column 'Raw Excel' shape Phase 2 already scans. Paragraphs become
    single-cell rows so the existing same-cell "Label: value" structural match
    (used for PDFs) applies unchanged; table rows keep their column positions so
    the left/right grid-context lookahead also works unchanged.
    """
    print(f"    [Phase 1] Reading Word document: {docx_path.name}")
    doc = DocxDocument(str(docx_path))
    rows = []

    for section in doc.sections:
        for part in (section.header, section.footer):
            for para in part.paragraphs:
                text = para.text.strip()
                if text:
                    rows.append({0: text})

    for block in _iter_docx_block_items(doc):
        if isinstance(block, DocxParagraph):
            text = block.text.strip()
            if text:
                rows.append({0: text})
        else:  # DocxTable
            for row in block.rows:
                row_dict = {}
                for col_idx, cell in enumerate(row.cells):
                    cell_text = cell.text.strip()
                    if cell_text:
                        row_dict[col_idx] = cell_text
                if row_dict:
                    rows.append(row_dict)

    df = pd.DataFrame(rows)
    if df.empty or not df.notna().any().any():
        print(f"    [WARNING] {docx_path.name} produced no usable content.")

    df.to_excel(str(output_xlsx_path), index=False, header=False, engine='openpyxl')
    print(f"    Saved Raw Excel: {output_xlsx_path.name} ({len(df)} rows)")


def pptx_to_excel(pptx_path: Path, output_xlsx_path: Path):
    """
    Phase 1 for PowerPoint uploads (.pptx). Mirrors docx_to_excel(): flattens
    slide text-frames, tables, and speaker notes (in slide order) into the same
    row/column 'Raw Excel' shape Phase 2 already scans. Each text-frame paragraph
    becomes a single-cell row (same-cell "Label: value" structural match applies
    unchanged); table rows keep their column positions.
    """
    from pptx import Presentation
    print(f"    [Phase 1] Reading PowerPoint document: {pptx_path.name}")
    prs = Presentation(str(pptx_path))
    rows = []

    for slide in prs.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                for para in shape.text_frame.paragraphs:
                    text = "".join(r.text for r in para.runs).strip()
                    if text:
                        rows.append({0: text})
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    row_dict = {}
                    for col_idx, cell in enumerate(row.cells):
                        cell_text = cell.text_frame.text.strip()
                        if cell_text:
                            row_dict[col_idx] = cell_text
                    if row_dict:
                        rows.append(row_dict)
        if slide.has_notes_slide:
            notes_text = slide.notes_slide.notes_text_frame.text.strip()
            if notes_text:
                rows.append({0: notes_text})

    df = pd.DataFrame(rows)
    if df.empty or not df.notna().any().any():
        print(f"    [WARNING] {pptx_path.name} produced no usable content.")

    df.to_excel(str(output_xlsx_path), index=False, header=False, engine='openpyxl')
    print(f"    Saved Raw Excel: {output_xlsx_path.name} ({len(df)} rows)")


def email_to_excel(email_path: Path, output_xlsx_path: Path):
    """
    Phase 1 for email uploads (.eml/.msg). Flattens the redactable text —
    Subject/From/To/Cc headers plus the text/plain and text/html body parts —
    into the same single-cell-per-line 'Raw Excel' shape Phase 2 scans. Each
    header and each body line becomes its own single-cell row so the existing
    same-cell "Label: value" structural match applies unchanged. Attachments
    are not extracted (see the Body+headers scope decision)."""
    print(f"    [Phase 1] Reading email: {email_path.name}")
    # openpyxl rejects ASCII control chars (except tab/newline/CR) in cells with
    # IllegalCharacterError; email text (esp. from .msg) often carries them, so
    # strip them before flattening to the scan sheet. This only affects the Raw
    # Excel used for detection — the redacted email output keeps its own bytes.
    illegal = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
    rows = []
    try:
        for chunk in email_utils.extract_email_text(email_path):
            for line in str(chunk).splitlines():
                line = illegal.sub('', line).strip()
                if line:
                    rows.append({0: line})
    except Exception as e:
        print(f"    [ERROR] Failed to read {email_path.name}: {e}")

    df = pd.DataFrame(rows)
    if df.empty or not df.notna().any().any():
        print(f"    [WARNING] {email_path.name} produced no usable content.")

    df.to_excel(str(output_xlsx_path), index=False, header=False, engine='openpyxl')
    print(f"    Saved Raw Excel: {output_xlsx_path.name} ({len(df)} rows)")


# ----------------------------------------------------------------------
# "As is" output writers — redact the ORIGINAL uploaded file in place,
# preserving its native format/structure, instead of the flattened Excel
# used by the "Excel" output option. See
# wiki/feature-updates/phase-1/plan-as-it-is-implementation.md.
#
# These reuse apply_masking()/global_entities from the existing flatten-and-
# scan pass (Phase 1 + scan_text_for_entities()) — masking is value-based,
# not position-based, so a fresh pass over the original document's own text
# can mask it directly without needing to map back to Raw Excel cell
# positions.
# ----------------------------------------------------------------------

def convert_docx_to_doc(docx_path: Path, output_doc_path: Path) -> bool:
    """
    Converts a .docx file back to legacy binary .doc via MS Word COM automation —
    the inverse of convert_doc_to_docx(). Used so ".doc As is" uploads round-trip
    back to ".doc" instead of leaking a ".docx" extension. Requires Microsoft Word
    to be installed. Returns True on success, False if Word/COM isn't available.
    """
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    word = None
    try:
        word = win32com.client.gencache.EnsureDispatch("Word.Application")
        word.Visible = False
        wdoc = word.Documents.Open(str(docx_path.resolve()), ReadOnly=True)
        try:
            wdoc.SaveAs2(str(output_doc_path.resolve()), FileFormat=0)  # wdFormatDocument (.doc)
        finally:
            wdoc.Close(False)
        return True
    except Exception as e:
        print(f"    [ERROR] Word COM conversion (docx->doc) failed for {docx_path.name}: {e}")
        return False
    finally:
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def _rewrite_paragraph_text(paragraph, transform_fn):
    """
    Rewrite a python-docx paragraph's visible text via transform_fn(text) -> (new_text, events).
    Single-run paragraphs keep their exact run formatting. Multi-run paragraphs whose
    text actually changes collapse into the first run's formatting (only on change —
    untouched paragraphs, the common case, are left byte-for-byte alone).
    Returns the events list from transform_fn (empty if nothing changed).
    """
    runs = paragraph.runs
    if not runs:
        return []
    original_text = "".join(r.text for r in runs)
    if not original_text.strip():
        return []
    new_text, events = transform_fn(original_text)
    if not events or new_text == original_text:
        return []
    runs[0].text = new_text
    for r in runs[1:]:
        r.text = ""
    return events


def _make_audit_event(ts: str, filename: str, ev: dict, page_num=1, row=None, col=None) -> dict:
    return {
        'timestamp': ts, 'filename': filename,
        'page_num': page_num, 'excel_row': row, 'excel_col': col,
        'token_type': ev['token'], 'original_text': ev['value'],
    }


def write_redacted_xlsx_as_is(input_path: Path, output_path: Path, global_entities: dict,
                               secret_map: dict, candidate_counters: dict) -> list:
    """Re-open the original .xlsx workbook and mask cell text in place, preserving
    sheets/formatting/merged cells (unlike spreadsheet_to_excel(), which flattens)."""
    import openpyxl
    wb = openpyxl.load_workbook(str(input_path))
    audit_records = []
    ts = time.strftime('%Y-%m-%dT%H:%M:%S')
    filename = input_path.name

    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.strip():
                    masked, events = apply_masking(cell.value, global_entities, secret_map, candidate_counters)
                    if events:
                        cell.value = masked
                        for ev in events:
                            audit_records.append(_make_audit_event(
                                ts, filename, ev, page_num=ws.title, row=cell.row, col=cell.column))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(output_path))
    return audit_records


def write_redacted_xls_as_is(input_path: Path, output_path: Path, global_entities: dict,
                              secret_map: dict, candidate_counters: dict) -> list:
    """Legacy .xls "as is" — best-effort cell-value round-trip via xlrd/xlwt.
    xlwt is unmaintained and doesn't preserve styling the way openpyxl does for
    .xlsx, so this is lower-fidelity than the .xlsx path. Raises (triggering the
    Excel-output fallback) if xlwt isn't installed."""
    import xlrd
    import xlwt

    rb = xlrd.open_workbook(str(input_path), formatting_info=False)
    wb = xlwt.Workbook()
    audit_records = []
    ts = time.strftime('%Y-%m-%dT%H:%M:%S')
    filename = input_path.name

    for sheet_idx in range(rb.nsheets):
        rs = rb.sheet_by_index(sheet_idx)
        ws = wb.add_sheet(rs.name or f"Sheet{sheet_idx + 1}")
        for r in range(rs.nrows):
            for c in range(rs.ncols):
                val = rs.cell_value(r, c)
                if isinstance(val, str) and val.strip():
                    masked, events = apply_masking(val, global_entities, secret_map, candidate_counters)
                    if events:
                        for ev in events:
                            audit_records.append(_make_audit_event(
                                ts, filename, ev, page_num=rs.name, row=r, col=c))
                        ws.write(r, c, masked)
                        continue
                ws.write(r, c, val)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(output_path))
    return audit_records


def write_redacted_docx_as_is(input_path: Path, output_path: Path, global_entities: dict,
                               secret_map: dict, candidate_counters: dict) -> list:
    """Re-open the original .docx and mask paragraph/table-cell text in place,
    preserving formatting, images, and layout (unlike docx_to_excel(), which
    flattens to plain text rows)."""
    doc = DocxDocument(str(input_path))
    audit_records = []
    ts = time.strftime('%Y-%m-%dT%H:%M:%S')
    filename = input_path.name

    def transform(text):
        return apply_masking(text, global_entities, secret_map, candidate_counters)

    for section in doc.sections:
        for part in (section.header, section.footer):
            for para in part.paragraphs:
                for ev in _rewrite_paragraph_text(para, transform):
                    audit_records.append(_make_audit_event(ts, filename, ev))

    row_idx = 0
    for block in _iter_docx_block_items(doc):
        if isinstance(block, DocxParagraph):
            for ev in _rewrite_paragraph_text(block, transform):
                audit_records.append(_make_audit_event(ts, filename, ev, row=row_idx))
            row_idx += 1
        else:  # DocxTable
            for row in block.rows:
                for col_idx, cell in enumerate(row.cells):
                    for para in cell.paragraphs:
                        for ev in _rewrite_paragraph_text(para, transform):
                            audit_records.append(_make_audit_event(ts, filename, ev, row=row_idx, col=col_idx))
                row_idx += 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return audit_records


def write_redacted_pptx_as_is(input_path: Path, output_path: Path, global_entities: dict,
                               secret_map: dict, candidate_counters: dict) -> list:
    """Re-open the original .pptx and mask slide text-frame/table-cell/notes text
    in place, preserving layout and images — mirrors write_redacted_docx_as_is().
    python-pptx paragraphs/runs expose the same .runs / run.text shape as
    python-docx, so _rewrite_paragraph_text() works unchanged here."""
    from pptx import Presentation
    prs = Presentation(str(input_path))
    audit_records = []
    ts = time.strftime('%Y-%m-%dT%H:%M:%S')
    filename = input_path.name

    def transform(text):
        return apply_masking(text, global_entities, secret_map, candidate_counters)

    for slide_idx, slide in enumerate(prs.slides):
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                for para in shape.text_frame.paragraphs:
                    for ev in _rewrite_paragraph_text(para, transform):
                        audit_records.append(_make_audit_event(ts, filename, ev, page_num=slide_idx + 1))
            if getattr(shape, "has_table", False):
                for row_idx, row in enumerate(shape.table.rows):
                    for col_idx, cell in enumerate(row.cells):
                        for para in cell.text_frame.paragraphs:
                            for ev in _rewrite_paragraph_text(para, transform):
                                audit_records.append(_make_audit_event(
                                    ts, filename, ev, page_num=slide_idx + 1, row=row_idx, col=col_idx))
        if slide.has_notes_slide:
            for para in slide.notes_slide.notes_text_frame.paragraphs:
                for ev in _rewrite_paragraph_text(para, transform):
                    audit_records.append(_make_audit_event(ts, filename, ev, page_num=slide_idx + 1))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(output_path))
    return audit_records


def write_redacted_email_as_is(input_path: Path, output_path: Path, global_entities: dict,
                               secret_map: dict, candidate_counters: dict) -> list:
    """Redact an .eml/.msg in place — Subject/From/To/Cc headers plus text body
    parts — preserving the native email format (unlike email_to_excel(), which
    flattens to plain text rows). .eml is rewritten with the stdlib email
    package; .msg goes through Outlook COM (see email_utils)."""
    ts = time.strftime('%Y-%m-%dT%H:%M:%S')
    filename = input_path.name
    audit_records = []

    def transform(text):
        masked, events = apply_masking(text, global_entities, secret_map, candidate_counters)
        for ev in events:
            audit_records.append(_make_audit_event(ts, filename, ev))
        return masked, events

    email_utils.rewrite_email(input_path, output_path, transform)
    return audit_records


def _pdf_page_words(page, use_ocr_fallback: bool = True):
    """
    Return (words, is_ocr) for one PDF page, where words is a list of
    (x0, y0, x1, y1, text) boxes in PDF point space.

    Uses the native text layer when present. Falls back to Tesseract OCR on a
    300 DPI rendered page image for scanned/image-only pages (same approach as
    Tier 3 in pdf_to_excel_docling()), converting pixel coordinates back to
    point space via the DPI scale factor.
    """
    native = [(w[0], w[1], w[2], w[3], w[4]) for w in page.get_text("words") if w[4].strip()]
    if native or not use_ocr_fallback:
        return native, False

    import pytesseract
    import shutil
    tess_cmd = shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    pytesseract.pytesseract.tesseract_cmd = tess_cmd

    render_dpi = 300
    pix = page.get_pixmap(dpi=render_dpi)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    tsv = pytesseract.image_to_data(img, config='--psm 6', output_type=pytesseract.Output.DICT)

    scale = 72.0 / render_dpi
    ocr_words = []
    for i in range(len(tsv['text'])):
        txt = (tsv['text'][i] or '').strip()
        try:
            conf = int(tsv['conf'][i])
        except (ValueError, TypeError):
            conf = -1
        if conf > 20 and txt:
            x0 = tsv['left'][i] * scale
            y0 = tsv['top'][i] * scale
            x1 = (tsv['left'][i] + tsv['width'][i]) * scale
            y1 = (tsv['top'][i] + tsv['height'][i]) * scale
            ocr_words.append((x0, y0, x1, y1, txt))
    return ocr_words, True


def _group_words_into_lines(words, y_tol: float = 4.0):
    """Group (x0,y0,x1,y1,text) word boxes into text lines by vertical proximity.
    Returns a list of {'cy': float, 'words': [...]} sorted left-to-right within a line."""
    lines = []
    for w in sorted(words, key=lambda w: (w[1], w[0])):
        cy = (w[1] + w[3]) / 2
        placed = False
        for line in lines:
            if abs(line['cy'] - cy) <= y_tol:
                line['words'].append(w)
                line['cy'] = sum((ww[1] + ww[3]) / 2 for ww in line['words']) / len(line['words'])
                placed = True
                break
        if not placed:
            lines.append({'cy': cy, 'words': [w]})
    for line in lines:
        line['words'].sort(key=lambda w: w[0])
    return lines


def _find_spans_in_line(line_words, needles: list):
    """
    needles: [(needle_lower, replacement), ...] sorted longest-first (mirrors
    apply_masking()'s longest-match-first substitution order, so overlapping
    values like "John" inside "John Smith" don't get double-matched).

    Returns [((x0,y0,x1,y1), matched_original_text, replacement), ...] — matched
    spans are the union bbox of every word the matched text touches.
    """
    texts = [w[4] for w in line_words]
    line_text = " ".join(texts)
    line_text_lower = line_text.lower()

    offsets = []
    pos = 0
    for t in texts:
        offsets.append(pos)
        pos += len(t) + 1

    covered = [False] * len(line_text)
    matches = []
    for needle_lower, replacement in needles:
        # Needles come from a different extraction pass (docling/OCR table cells)
        # than line_text (native PDF words / Tesseract here) and can carry stray
        # leading/trailing or doubled whitespace that would otherwise defeat an
        # exact substring match against the on-page text.
        needle_lower = re.sub(r'\s+', ' ', needle_lower).strip()
        if len(needle_lower) < 3:
            continue
        start = 0
        while True:
            idx = line_text_lower.find(needle_lower, start)
            if idx == -1:
                break
            end = idx + len(needle_lower)
            start = idx + 1
            if any(covered[idx:end]):
                continue

            first_wi = last_wi = None
            for wi, off in enumerate(offsets):
                w_end = off + len(texts[wi])
                if w_end > idx and off < end:
                    if first_wi is None:
                        first_wi = wi
                    last_wi = wi
            if first_wi is None:
                continue

            box_words = line_words[first_wi:last_wi + 1]
            x0 = min(w[0] for w in box_words)
            y0 = min(w[1] for w in box_words)
            x1 = max(w[2] for w in box_words)
            y1 = max(w[3] for w in box_words)
            matches.append(((x0, y0, x1, y1), line_text[idx:end], replacement))
            for k in range(idx, end):
                covered[k] = True
    return matches


def _max_half_width_for_line(x0: float, x1: float, line_words: list, pad: float) -> float:
    """How far a replacement-token box centered on the match spanning [x0,x1]
    may expand to each side before it reaches another word on the same line
    (kept `pad` clear of that neighbor's own edge). Used to keep a wide token
    like <*ORGANIZATION_3*> from overwriting/redacting part of an adjacent,
    unmatched word when it's drawn wider than the text it replaces. Never
    smaller than the match's own half-width, and unconstrained (inf) on a
    side with no neighboring word (start/end of line)."""
    left_limit = right_limit = None
    for w in line_words:
        wx0, wx1 = w[0], w[2]
        if wx1 <= x0 + 0.01:
            if left_limit is None or wx1 > left_limit:
                left_limit = wx1
        elif wx0 >= x1 - 0.01:
            if right_limit is None or wx0 < right_limit:
                right_limit = wx0
    cx = (x0 + x1) / 2
    left_room = (cx - left_limit - pad) if left_limit is not None else float('inf')
    right_room = (right_limit - pad - cx) if right_limit is not None else float('inf')
    return max(min(left_room, right_room), (x1 - x0) / 2)


def _shrink_to_fit(token: str, rect_width: float, base_fontsize: float, min_fontsize: float,
                    max_half_w: float, gap: float, measure_fn, step: float = 0.5):
    """Shrink `token`'s draw fontsize from base_fontsize down to min_fontsize
    (in `step` increments) until its box — max(rect_width, text_width + gap),
    split evenly either side of center — fits within max_half_w. Falls back
    to a hard clamp at min_fontsize if it still doesn't fit, so the caller's
    box can never exceed max_half_w (and therefore never reach a neighboring
    word) regardless of how long the token is."""
    fontsize = base_fontsize
    text_w = measure_fn(token, fontsize)
    half_w = max(rect_width, text_w + gap) / 2
    while half_w > max_half_w and fontsize > min_fontsize:
        fontsize = max(min_fontsize, fontsize - step)
        text_w = measure_fn(token, fontsize)
        half_w = max(rect_width, text_w + gap) / 2
    half_w = min(half_w, max_half_w)
    return fontsize, text_w, half_w


def write_redacted_pdf_as_is(input_path: Path, output_path: Path, global_entities: dict,
                              secret_map: dict, candidate_counters: dict) -> list:
    """
    Visually redact the original PDF: locate each PII value's exact position on
    the page (native text layer, or Tesseract OCR for scanned pages) and use
    PyMuPDF redaction annotations to draw over it and burn in the replacement
    token — unlike pdf_to_excel_docling(), which only extracts flattened text
    and discards page/bbox coordinates.
    """
    doc = fitz.open(str(input_path))
    audit_records = []
    ts = time.strftime('%Y-%m-%dT%H:%M:%S')
    filename = input_path.name
    needles = sorted(global_entities.items(), key=lambda kv: -len(kv[0]))

    for page_num in range(len(doc)):
        page = doc[page_num]
        words, is_ocr = _pdf_page_words(page, use_ocr_fallback=True)
        if not words:
            continue
        lines = _group_words_into_lines(words, y_tol=6.0 if is_ocr else 4.0)

        page_matches = []
        for line in lines:
            for m in _find_spans_in_line(line['words'], needles):
                page_matches.append((m, line['words']))
        if not page_matches:
            continue

        draws = []
        for (bbox, matched_text, token), line_words in page_matches:
            x0, y0, x1, y1 = bbox
            rect = fitz.Rect(x0, y0, x1, y1)
            base_fontsize = max(6, min(10, rect.height * 0.8))
            # MuPDF auto-shrinks annotation text to fit the rect using an internal
            # line-height reserve of ~2.3x fontsize, which a single-line word bbox
            # never has — so we white-out only, then draw the text ourselves at the
            # exact fontsize computed above (matches the working image-path approach).
            # The box is capped to the gap to the nearest neighboring word on the same
            # line (shrinking the font first) so a wide token never redact-annotates —
            # and thus deletes — part of an adjacent, unmatched word.
            max_half_w = _max_half_width_for_line(rect.x0, rect.x1, line_words, pad=1.5)
            fontsize, text_w, half_w = _shrink_to_fit(
                token, rect.width, base_fontsize, 4.0, max_half_w, gap=4,
                measure_fn=lambda t, fs: fitz.get_text_length(t, fontname="helv", fontsize=fs),
            )
            cx = (rect.x0 + rect.x1) / 2
            wide_rect = fitz.Rect(cx - half_w, rect.y0, cx + half_w, rect.y1)
            page.add_redact_annot(wide_rect, fill=(1, 1, 1))
            baseline_y = y0 + (rect.height + fontsize * 0.7) / 2
            draws.append((cx - text_w / 2, baseline_y, token, fontsize))
            audit_records.append(_make_audit_event(
                ts, filename, {'token': token, 'value': matched_text}, page_num=page_num + 1))
        page.apply_redactions()
        for origin_x, baseline_y, token, fontsize in draws:
            page.insert_text((origin_x, baseline_y), token, fontname="helv", fontsize=fontsize, color=(0, 0, 0))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path), garbage=4, deflate=True)
    doc.close()
    return audit_records


def write_redacted_as_is(input_path: Path, output_dir: Path, global_entities: dict,
                          secret_map: dict, candidate_counters: dict):
    """
    Dispatch to the right per-format "as is" writer, keyed off the ORIGINAL
    input file's extension. Returns (output_path, audit_records) on success.
    Raises on failure — the caller in main() falls back to Excel output
    (plan Open Question #5).
    """
    ext = input_path.suffix.lower()
    stem = input_path.stem

    if ext == '.xlsx':
        out_path = output_dir / f"{stem}_redacted.xlsx"
        return out_path, write_redacted_xlsx_as_is(input_path, out_path, global_entities, secret_map, candidate_counters)

    if ext == '.xls':
        out_path = output_dir / f"{stem}_redacted.xls"
        return out_path, write_redacted_xls_as_is(input_path, out_path, global_entities, secret_map, candidate_counters)

    if ext == '.docx':
        out_path = output_dir / f"{stem}_redacted.docx"
        return out_path, write_redacted_docx_as_is(input_path, out_path, global_entities, secret_map, candidate_counters)

    if ext == '.doc':
        tmp_in = OUTPUT_RAW_FOLDER / f"{stem}_as_is_source.docx"
        if not convert_doc_to_docx(input_path, tmp_in):
            raise RuntimeError("Word COM conversion (doc->docx) failed")
        tmp_out = OUTPUT_RAW_FOLDER / f"{stem}_as_is_masked.docx"
        records = write_redacted_docx_as_is(tmp_in, tmp_out, global_entities, secret_map, candidate_counters)
        out_path = output_dir / f"{stem}_redacted.doc"
        if not convert_docx_to_doc(tmp_out, out_path):
            raise RuntimeError("Word COM conversion (docx->doc) failed")
        for tmp in (tmp_in, tmp_out):
            try: tmp.unlink(missing_ok=True)
            except Exception: pass
        return out_path, records

    if ext == '.pptx':
        out_path = output_dir / f"{stem}_redacted.pptx"
        return out_path, write_redacted_pptx_as_is(input_path, out_path, global_entities, secret_map, candidate_counters)

    if ext in ('.eml', '.msg'):
        # A .msg redacts to .eml (pure-Python path) unless Outlook mode is on.
        out_path = output_dir / f"{stem}_redacted{email_utils.redacted_output_ext(ext)}"
        return out_path, write_redacted_email_as_is(input_path, out_path, global_entities, secret_map, candidate_counters)

    if ext == '.pdf':
        out_path = output_dir / f"{stem}_redacted.pdf"
        return out_path, write_redacted_pdf_as_is(input_path, out_path, global_entities, secret_map, candidate_counters)

    if ext in IMAGE_EXTENSIONS:
        out_path = output_dir / f"{stem}_redacted{ext}"
        return out_path, write_redacted_image_as_is(input_path, out_path, global_entities, secret_map, candidate_counters)

    raise ValueError(f"No as-is writer for extension: {ext}")


# ----------------------------------------------------------------------
# "As is" rehydration — mirror-image of the writers above, searching the
# redacted document for token text and swapping in the original value.
# ----------------------------------------------------------------------

def unmask_xlsx_as_is(redacted_path: Path, unmasked_path: Path, mapping: dict):
    import openpyxl
    wb = openpyxl.load_workbook(str(redacted_path))
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value:
                    new_val = cell.value
                    for cand, orig in mapping.items():
                        if cand in new_val:
                            new_val = new_val.replace(cand, orig)
                    if new_val != cell.value:
                        cell.value = new_val
    unmasked_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(unmasked_path))


def unmask_xls_as_is(redacted_path: Path, unmasked_path: Path, mapping: dict):
    import xlrd
    import xlwt
    rb = xlrd.open_workbook(str(redacted_path), formatting_info=False)
    wb = xlwt.Workbook()
    for sheet_idx in range(rb.nsheets):
        rs = rb.sheet_by_index(sheet_idx)
        ws = wb.add_sheet(rs.name or f"Sheet{sheet_idx + 1}")
        for r in range(rs.nrows):
            for c in range(rs.ncols):
                val = rs.cell_value(r, c)
                if isinstance(val, str) and val:
                    for cand, orig in mapping.items():
                        if cand in val:
                            val = val.replace(cand, orig)
                ws.write(r, c, val)
    unmasked_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(unmasked_path))


def unmask_docx_as_is(redacted_path: Path, unmasked_path: Path, mapping: dict):
    doc = DocxDocument(str(redacted_path))

    def transform(text):
        new_text = text
        events = []
        for cand, orig in mapping.items():
            if cand in new_text:
                new_text = new_text.replace(cand, orig)
                events.append({'token': cand, 'value': orig})
        return new_text, events

    for section in doc.sections:
        for part in (section.header, section.footer):
            for para in part.paragraphs:
                _rewrite_paragraph_text(para, transform)

    for block in _iter_docx_block_items(doc):
        if isinstance(block, DocxParagraph):
            _rewrite_paragraph_text(block, transform)
        else:
            for row in block.rows:
                for cell in row.cells:
                    for para in cell.paragraphs:
                        _rewrite_paragraph_text(para, transform)

    unmasked_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(unmasked_path))


def unmask_pptx_as_is(redacted_path: Path, unmasked_path: Path, mapping: dict):
    from pptx import Presentation
    prs = Presentation(str(redacted_path))

    def transform(text):
        new_text = text
        events = []
        for cand, orig in mapping.items():
            if cand in new_text:
                new_text = new_text.replace(cand, orig)
                events.append({'token': cand, 'value': orig})
        return new_text, events

    for slide in prs.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                for para in shape.text_frame.paragraphs:
                    _rewrite_paragraph_text(para, transform)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    for cell in row.cells:
                        for para in cell.text_frame.paragraphs:
                            _rewrite_paragraph_text(para, transform)
        if slide.has_notes_slide:
            for para in slide.notes_slide.notes_text_frame.paragraphs:
                _rewrite_paragraph_text(para, transform)

    unmasked_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(unmasked_path))


def unmask_pdf_as_is(redacted_path: Path, unmasked_path: Path, mapping: dict):
    """Find each token's burned-in text (added as real, searchable text by
    write_redacted_pdf_as_is()'s redact-annotation) and redact-and-replace it
    with the original value."""
    doc = fitz.open(str(redacted_path))
    needles = sorted(((tok.lower(), orig) for tok, orig in mapping.items()), key=lambda kv: -len(kv[0]))

    for page_num in range(len(doc)):
        page = doc[page_num]
        words, _ = _pdf_page_words(page, use_ocr_fallback=False)
        if not words:
            continue
        lines = _group_words_into_lines(words, y_tol=4.0)

        page_matches = []
        for line in lines:
            for m in _find_spans_in_line(line['words'], needles):
                page_matches.append((m, line['words']))
        if not page_matches:
            continue

        draws = []
        for (bbox, matched_text, orig_value), line_words in page_matches:
            x0, y0, x1, y1 = bbox
            rect = fitz.Rect(x0, y0, x1, y1)
            base_fontsize = max(6, min(10, rect.height * 0.8))
            # Same neighbor-gap cap as write_redacted_pdf_as_is — the restored
            # original value can be wider than the token it replaces, so it must
            # not redact-annotate into an adjacent, unmatched word either.
            max_half_w = _max_half_width_for_line(rect.x0, rect.x1, line_words, pad=1.5)
            fontsize, text_w, half_w = _shrink_to_fit(
                orig_value, rect.width, base_fontsize, 4.0, max_half_w, gap=4,
                measure_fn=lambda t, fs: fitz.get_text_length(t, fontname="helv", fontsize=fs),
            )
            cx = (rect.x0 + rect.x1) / 2
            wide_rect = fitz.Rect(cx - half_w, rect.y0, cx + half_w, rect.y1)
            page.add_redact_annot(wide_rect, fill=(1, 1, 1))
            baseline_y = y0 + (rect.height + fontsize * 0.7) / 2
            draws.append((cx - text_w / 2, baseline_y, orig_value, fontsize))
        page.apply_redactions()
        for origin_x, baseline_y, orig_value, fontsize in draws:
            page.insert_text((origin_x, baseline_y), orig_value, fontname="helv", fontsize=fontsize, color=(0, 0, 0))

    unmasked_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(unmasked_path), garbage=4, deflate=True)
    doc.close()


def unmask_email_as_is(redacted_path: Path, unmasked_path: Path, mapping: dict):
    """Restore original values in a redacted .eml/.msg by replacing each token
    with its source text across Subject/From/To/Cc headers and text body
    parts."""
    def transform(text):
        new_text = text
        events = []
        for cand, orig in mapping.items():
            if cand in new_text:
                new_text = new_text.replace(cand, orig)
                events.append({'token': cand, 'value': orig})
        return new_text, events

    email_utils.rewrite_email(redacted_path, unmasked_path, transform)


def unmask_as_is(redacted_path: Path, unmasked_path: Path, mapping: dict):
    """Dispatch rehydration to the right per-format reader, keyed off the
    redacted file's own extension."""
    ext = redacted_path.suffix.lower()
    if ext == '.xlsx':
        unmask_xlsx_as_is(redacted_path, unmasked_path, mapping)
    elif ext == '.xls':
        unmask_xls_as_is(redacted_path, unmasked_path, mapping)
    elif ext == '.docx':
        unmask_docx_as_is(redacted_path, unmasked_path, mapping)
    elif ext == '.doc':
        tmp_in = redacted_path.with_name(redacted_path.stem + "_as_is_source.docx")
        if not convert_doc_to_docx(redacted_path, tmp_in):
            raise RuntimeError("Word COM conversion (doc->docx) failed")
        tmp_out = redacted_path.with_name(redacted_path.stem + "_as_is_unmasked.docx")
        unmask_docx_as_is(tmp_in, tmp_out, mapping)
        if not convert_docx_to_doc(tmp_out, unmasked_path):
            raise RuntimeError("Word COM conversion (docx->doc) failed")
        for tmp in (tmp_in, tmp_out):
            try: tmp.unlink(missing_ok=True)
            except Exception: pass
    elif ext == '.pptx':
        unmask_pptx_as_is(redacted_path, unmasked_path, mapping)
    elif ext in ('.eml', '.msg'):
        unmask_email_as_is(redacted_path, unmasked_path, mapping)
    elif ext == '.pdf':
        unmask_pdf_as_is(redacted_path, unmasked_path, mapping)
    else:
        raise ValueError(f"No as-is unmasker for extension: {ext}")


def _load_manifest(manifest_path: Path) -> dict:
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    required = {"client_id", "job_id", "process_id", "files"}
    if not required.issubset(manifest):
        raise ValueError(f"Manifest {manifest_path} missing required keys: {required - set(manifest)}")
    return manifest


# ----------------------------------------------------------------------
# Pass 4: Convergence Accuracy Engine + Critique Agent
# Ported from PII_Code_19 Aug's pipeline/accuracy_engine.py,
# pipeline/llm_ground_truth_extractor.py, and pipeline/critique_agent.py.
#
# Reuses this file's own checksum validators/patterns (see "4. Checksum
# Validators" above) and Ollama config (SLM_OLLAMA_URL/SLM_OLLAMA_MODEL)
# rather than introducing a second, separate set. Feedback-memory
# persistence (auto_apply_feedback / feedback_memory.json) is deliberately
# NOT ported — out of scope for this round, see
# wiki/plan-compare-wiki.md "Explicitly out of scope for this round".
# ----------------------------------------------------------------------

# This project's token syntax is <*TYPE_N*> (see get_candidate_token above),
# not NEW's [TYPE_N] — adapted here accordingly.
_TOKEN_PAT = re.compile(r'<\*([A-Z_]+)_(\d+)\*>')

_GROUND_TRUTH_BLOCKLIST = {
    'you', 'your', 'yours', 'we', 'our', 'ours', 'us', 'my', 'me', 'mine',
    'i', 'he', 'him', 'his', 'she', 'her', 'hers', 'they', 'them', 'their',
    'theirs', 'it', 'its', 'who', 'whom', 'whose',
    'taxpayer', 'agent', 'tax agent', 'client', 'borrower', 'applicant',
    'person', 'individual', 'services', 'declaration', 'lodgment', 'return',
    'notice', 'form', 'page', 'year', 'section', 'info', 'information',
    'number', 'details', 'summary', 'period', 'date', 'total', 'amount',
    'balance', 'due', 'payment', 'invoice', 'receipt', 'statement',
    'account', 'description', 'quantity', 'unit', 'price', 'rate',
    'australia', 'australian', 'ato', 'asx', 'asic',
    'nsw', 'vic', 'qld', 'act', 'sa', 'wa', 'tas', 'nt',
    'pty', 'ltd', 'inc', 'corp', 'pty ltd',
}

_GROUND_TRUTH_SYSTEM_PROMPT = """[SYSTEM ROLE]
You are a Principal Data Privacy Officer and Lead Forensic Compliance Auditor evaluating documents under global privacy standards (APP, GDPR, HIPAA, CCPA).

[CORE OBJECTIVE]
Your mission is to perform a Zero-Trust Unbiased Information Extraction on the provided raw document text. Identify and extract ALL Personally Identifiable Information (PII) and Private Entity Identifiers that can uniquely identify, contact, locate, or link a natural person, private citizen, employee, or private financial account.

[EXTRACTION CRITERIA — SEMANTIC CLASSES]
1. DIRECT INDIVIDUAL IDENTIFIERS:
   - Full names, individual titles + names, family names, signature line names.
2. CONTACT & GEOGRAPHIC COORDINATES:
   - Email addresses, telephone numbers, mobile numbers.
   - Physical street addresses, residential locations, post office boxes, specific suburb+postcode blocks.
3. STATUTORY & GOVERNMENT REGISTRATION NUMBERS:
   - Tax File Numbers (TFN), ABN, ACN, Social Security Numbers (SSN), National Insurance Numbers (NINO), Medicare numbers, Passport numbers, Driver License numbers, CRNs, Aadhaar, PAN.
4. PRIVATE FINANCIAL & ACCOUNT LINKAGES:
   - Bank Account Numbers, BSB codes, IBANs, Credit/Debit Card numbers, Policy numbers, Superannuation/Pension IDs, Investment reference numbers.
5. INSTITUTIONAL & EMPLOYMENT IDENTIFIERS:
   - Employee IDs, Staff numbers, Payroll IDs, BMS IDs, Client Reference numbers, Customer IDs, Unique Invoice/Receipt/Transaction tracking numbers tied to an individual.
6. TEMPORAL PRIVATE ATTRIBUTES:
   - Specific individual Dates of Birth (DOB).

[NEGATIVE CONSTRAINTS — WHAT IS NOT PII]
Do NOT extract:
- Static form titles, form section headers, or field labels (e.g. "Taxpayer Name:", "Date of Birth:", "Description", "Amount", "Balance", "Total", "Notice").
- Public corporate entities, statutory government departments (e.g. "ATO", "Commonwealth of Australia", "ASIC", "ANZ Bank", "Pty Ltd").
- Financial currency values or monetary line items (e.g. "$81.87", "$2,055.00").
- Generic calendar years or tax financial period headers (e.g. "2024", "2025", "Financial Year").
- Standard English grammar words, pronouns, or legal boilerplate text.

[OUTPUT FORMAT]
Return ONLY a valid, raw JSON array of objects with zero markdown formatting or code fences:
[
  {
    "value": "exact raw string from document",
    "type": "PERSON" | "ADDRESS" | "PHONE" | "EMAIL" | "GOVERNMENT_ID" | "BUSINESS_ID" | "BANK_DETAILS" | "SENSITIVE_INFO"
  }
]"""


def generate_ground_truth(text: str, use_ollama: bool = True) -> List[Dict[str, str]]:
    """Independent PII baseline for the convergence loop below — mathematical
    checksum-validated identifiers (this file's own validators/patterns, so
    nothing here is circular against the detectors that produced the
    redaction) plus one independent LLM pass."""
    ground_truth = []
    seen = set()

    def _add(val: str, ptype: str):
        val_clean = val.strip()
        val_lower = val_clean.lower()
        if not val_clean or len(val_clean) < 2 or val_lower in seen:
            return
        if val_lower in _GROUND_TRUTH_BLOCKLIST:
            return
        # NOTE: requires a literal '$' — a bare digit-only pattern here (as in
        # PII_Code_19 Aug's original) would also match unformatted checksum
        # IDs like a 16-digit credit card number or a spaceless SSN/TFN,
        # silently dropping them from the ground truth baseline.
        if re.match(r'^\$\d+(?:,\d{3})*(?:\.\d{2})?$', val_clean) or re.match(r'^\s*(?:19|20)\d{2}\s*$', val_clean):
            return
        seen.add(val_lower)
        ground_truth.append({"value": val_clean, "type": ptype})

    for m in EMAIL_PAT.finditer(text):
        _add(m.group(), "EMAIL")
    for m in CREDIT_CARD_PAT.finditer(text):
        if valid_luhn(m.group()):
            _add(m.group(), "BANK_DETAILS")
    for m in US_SSN_PAT.finditer(text):
        if valid_us_ssn(m.group()):
            _add(m.group(), "GOVERNMENT_ID")
    for m in IBAN_PAT.finditer(text):
        if valid_iban(m.group()):
            _add(m.group(), "BANK_DETAILS")
    for m in TFN_PAT.finditer(text):
        if valid_tfn(m.group()):
            _add(m.group(), "GOVERNMENT_ID")
    for m in MEDICARE_PAT.finditer(text):
        if valid_medicare(m.group()):
            _add(m.group(), "GOVERNMENT_ID")
    for m in ABN_PAT.finditer(text):
        if valid_abn(m.group()):
            _add(m.group(), "BUSINESS_ID")
    for m in ACN_PAT.finditer(text):
        if valid_acn(m.group()):
            _add(m.group(), "BUSINESS_ID")
    for m in AADHAAR_PAT.finditer(text):
        if valid_verhoeff(m.group()):
            _add(m.group(), "GOVERNMENT_ID")

    if use_ollama and text.strip():
        try:
            doc_snippet = text[:6000]
            resp = requests.post(
                SLM_OLLAMA_URL,
                json={
                    "model": SLM_OLLAMA_MODEL,
                    "messages": [
                        {"role": "system", "content": _GROUND_TRUTH_SYSTEM_PROMPT},
                        {"role": "user", "content": f"<raw_document_text>\n{doc_snippet}\n</raw_document_text>\n\nOutput JSON Array:"},
                    ],
                    "format": "json",
                    "options": {"temperature": 0.0, "num_ctx": SLM_NUM_CTX},
                    "stream": False,
                },
                timeout=SLM_CHUNK_TIMEOUT,
            )
            if resp.status_code == 200:
                content = resp.json()["message"]["content"].strip()
                cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.MULTILINE).strip()
                llm_data = json.loads(cleaned)
                if isinstance(llm_data, list):
                    for item in llm_data:
                        if isinstance(item, dict):
                            val = item.get("value", "").strip()
                            ptype = item.get("type", "SENSITIVE_INFO").strip().upper()
                            if val and val.lower() in text.lower():
                                _add(val, ptype)
        except Exception as e:
            print(f"    [Ground Truth Extractor] LLM note: {e}")

    return ground_truth


def compute_accuracy(original_text: str, masked_text: str, ground_truth: list, global_entities: dict) -> dict:
    """Precision/recall of one masking pass against generate_ground_truth()'s
    independent baseline. Recall: how many ground-truth PII items are no
    longer visible in masked_text. Precision: how many minted tokens map to
    a real (non-blocklisted) PII value, using global_entities (this file's
    value_lower -> token map) as the reverse lookup."""
    masked_lower = masked_text.lower()
    result = {"precision": 1.0, "recall": 1.0, "missed": [], "false_positives": []}

    if not ground_truth:
        return result

    total_gt = len(ground_truth)
    masked_count = 0
    for gt_item in ground_truth:
        gt_val = gt_item["value"].strip().lower()
        if len(gt_val) < 2:
            masked_count += 1
            continue
        if gt_val in masked_lower:
            result["missed"].append(gt_item)
        else:
            masked_count += 1
    result["recall"] = masked_count / total_gt if total_gt > 0 else 1.0

    all_tokens = _TOKEN_PAT.findall(masked_text)
    if global_entities and all_tokens:
        token_to_original = {tok: orig for orig, tok in global_entities.items()}
        total_tokens = len(all_tokens)
        valid_tokens = 0
        for cat_name, num in all_tokens:
            token_str = f"<*{cat_name}_{num}*>"
            orig_val = token_to_original.get(token_str, "")
            if orig_val:
                if orig_val.strip().lower() in _GROUND_TRUTH_BLOCKLIST:
                    result["false_positives"].append({"value": orig_val, "token": token_str})
                else:
                    valid_tokens += 1
            else:
                valid_tokens += 1  # can't verify (propagated/manual token), assume valid
        result["precision"] = valid_tokens / total_tokens if total_tokens > 0 else 1.0

    return result


_CRITIQUE_SYSTEM_PROMPT = """[SYSTEM ROLE]
You are a Senior Data Privacy QA Auditor specializing in zero-leakage enterprise compliance (APP/GDPR).

[TASK]
Perform a differential audit between `<original_text>` and `<masked_text>`. Identify any unmasked PII elements present in `<original_text>` that were leaked (left visible without a placeholder) inside `<masked_text>`.

[CRITICAL NON-PII EXCLUSION RULES - NEVER REPORT THESE AS LEAKS]
1. TRANSACTION DATES & FISCAL PERIODS: Dates like "09 September 2025", "2 Sep 2024", "27 Aug 2024", "1 Nov 2023", "2024 to 2025", or tax years are standard accounting metadata. ONLY personal Date of Birth (DOB) is PII. NEVER flag transaction or statement dates.
2. FINANCIAL AMOUNTS & BALANCES: Currency values like "$6,041.80", "$8,748.04", "$2,434.71", "$127.20", "$0.00", interest rates like "3.20%", "4.70%", and percentages are NOT PII. NEVER flag financial numbers.
3. STATIC LABELS & HEADINGS: Words like "Balance", "Description", "Debit", "Credit", "General interest charge", "EFT refund", "Australian Taxation Office", "Taxpayer", "Transactions", "Overdue", "Not yet due" are static form text. NEVER flag them.

[WHAT CONSTITUTES REAL PII LEAKS]
- Human citizen names (e.g. "William Thomas Dunbar")
- Tax File Numbers (TFN e.g. "123 456 782"), ABNs, ACNs
- Bank Account Numbers, BSBs, Credit Card numbers
- Personal residential street addresses (e.g. "42 Wallaby Way, Sydney NSW")
- Personal phone numbers and personal email addresses
- Personal Date of Birth (e.g. "DOB: 14/05/1988")

[FEW-SHOT AUDIT GUIDELINES]

Example 1 (Legitimate Leak -> FAIL):
<original_text>Account Holder: John Smith, TFN: 987 654 321, Processed: 09 Sep 2025, Balance: $5,400.00</original_text>
<masked_text>Account Holder: John Smith, TFN: 987 654 321, Processed: 09 Sep 2025, Balance: $5,400.00</masked_text>
Output: {"passed": false, "missed_entities": [{"value": "John Smith", "type": "PERSON"}, {"value": "987 654 321", "type": "GOVERNMENT_ID"}]}

Example 2 (Accounting Dates & Balances Visible -> PASS):
<original_text>Processed date 09 Sep 2025, General interest charge $127.20, Balance $0.00</original_text>
<masked_text>Processed date 09 Sep 2025, General interest charge $127.20, Balance $0.00</masked_text>
Output: {"passed": true, "missed_entities": []}

[OUTPUT FORMAT]
Return ONLY a raw valid JSON object:
{
  "passed": true|false,
  "missed_entities": [
    {"value": "exact raw string", "type": "PERSON"|"PHONE"|"EMAIL"|"ADDRESS"|"GOVERNMENT_ID"|"BUSINESS_ID"|"BANK_DETAILS"}
  ]
}
Do NOT include markdown formatting, code block fences (```json), or extra text."""


class CritiqueAgent:
    """Pass 4 differential audit — compares original vs masked text via this
    project's existing Ollama SLM to catch PII the earlier detection passes
    missed. See module docstring above for what was intentionally left out
    of this port (feedback-memory persistence)."""

    def __init__(self, model: str = None):
        self._model = model or SLM_OLLAMA_MODEL
        self._available = None

    def _check_availability(self) -> bool:
        try:
            base_url = SLM_OLLAMA_URL.rsplit("/api/chat", 1)[0]
            resp = requests.get(f"{base_url}/api/tags", timeout=3)
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                model_names = [m.get("name") for m in models if m.get("name")]
                if model_names:
                    if self._model in model_names:
                        return True
                    for name in model_names:
                        if "gemma" in name or "llama" in name or "qwen" in name:
                            self._model = name
                            return True
                    self._model = model_names[0]
                    return True
        except Exception:
            pass
        return False

    def _extract_json_safely(self, result_text: str) -> dict:
        """Resilient fuzzy JSON parser — recovers missed entities from
        malformed, truncated, or chatter-filled SLM output."""
        if not result_text or not result_text.strip():
            return {"passed": True, "missed_entities": []}

        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", result_text.strip(), flags=re.MULTILINE).strip()
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                return {"passed": len(parsed) == 0, "missed_entities": parsed}
            if isinstance(parsed, dict):
                return {
                    "passed": parsed.get("passed", len(parsed.get("missed_entities", [])) == 0),
                    "missed_entities": parsed.get("missed_entities", []),
                }
        except Exception:
            pass

        missed = []
        p1 = re.finditer(r'\{\s*"value"\s*:\s*"([^"]+)"\s*,\s*"type"\s*:\s*"([^"]+)"\s*\}', cleaned, re.IGNORECASE)
        for m in p1:
            missed.append({"value": m.group(1).strip(), "type": m.group(2).strip()})
        if not missed:
            p2 = re.finditer(r'\{\s*"type"\s*:\s*"([^"]+)"\s*,\s*"value"\s*:\s*"([^"]+)"\s*\}', cleaned, re.IGNORECASE)
            for m in p2:
                missed.append({"value": m.group(2).strip(), "type": m.group(1).strip()})
        if not missed:
            val_matches = re.findall(r'"value"\s*:\s*"([^"]+)"', cleaned, re.IGNORECASE)
            type_matches = re.findall(r'"type"\s*:\s*"([^"]+)"', cleaned, re.IGNORECASE)
            for i in range(min(len(val_matches), len(type_matches))):
                missed.append({"value": val_matches[i].strip(), "type": type_matches[i].strip()})

        passed = True
        if missed:
            passed = False
        elif re.search(r'"passed"\s*:\s*false', cleaned, re.IGNORECASE):
            passed = False
        return {"passed": passed, "missed_entities": missed}

    def _chunk_differential_text(self, original_text: str, masked_text: str, max_chars: int = 2500):
        """Filters purely-numeric/separator lines and caps size so the prompt
        fits cleanly in the SLM context window."""
        orig_lines = [l.strip() for l in original_text.splitlines() if l.strip()]
        mask_lines = [l.strip() for l in masked_text.splitlines() if l.strip()]
        filtered_orig, filtered_mask = [], []
        for ol, ml in zip(orig_lines, mask_lines):
            if re.match(r'^[\s\d\$,\.\-%/=]+$', ol) or ol in ('---', '===', '___'):
                continue
            filtered_orig.append(ol)
            filtered_mask.append(ml)
        sample_orig = "\n".join(filtered_orig)[:max_chars]
        sample_mask = "\n".join(filtered_mask)[:max_chars]
        if not sample_orig:
            sample_orig = original_text[:max_chars]
            sample_mask = masked_text[:max_chars]
        return sample_orig, sample_mask

    def audit(self, original_text: str, masked_text: str) -> dict:
        if self._available is None:
            self._available = self._check_availability()
        if not self._available:
            print("    [Critique Agent] Ollama not available — skipping audit (pass).")
            return {"passed": True, "missed_entities": []}

        orig_text, masked_text_send = self._chunk_differential_text(original_text, masked_text, max_chars=2500)
        prompt_user = (
            f"<original_text>{orig_text}</original_text>\n"
            f"<masked_text>{masked_text_send}</masked_text>\n\n"
            f"Output JSON:"
        )
        for attempt in range(2):
            try:
                resp = requests.post(
                    SLM_OLLAMA_URL,
                    json={
                        "model": self._model,
                        "messages": [
                            {"role": "system", "content": _CRITIQUE_SYSTEM_PROMPT},
                            {"role": "user", "content": prompt_user},
                        ],
                        "format": "json",
                        "options": {"temperature": 0.0, "num_ctx": 2048},
                        "stream": False,
                    },
                    # PII_Code_19 Aug's original used a 15s timeout ("keep the
                    # loop instantaneous"). Measured against a real local
                    # gemma3:4b on CPU-bound inference hardware, a single
                    # critique call (full system prompt + differential text)
                    # took ~60s even warm — a 15s or even 60s timeout would
                    # silently fall through to "pass, nothing missed" (see
                    # audit()'s no-response fallback below) on exactly the
                    # hardware this project already assumes elsewhere.
                    # SLM_CHUNK_TIMEOUT is this file's own existing timeout for
                    # the primary Ollama SLM pass — reused here instead of a
                    # second, separately-tuned constant.
                    timeout=SLM_CHUNK_TIMEOUT,
                )
                data = resp.json()
                if "error" in data:
                    print(f"    [Critique Agent] Ollama note: {data['error']}")
                    continue
                result_text = data.get("message", {}).get("content", "").strip()
                audit_data = self._extract_json_safely(result_text)
                filtered_missed = []
                for ent in audit_data.get("missed_entities", []):
                    val = ent.get("value", "").strip()
                    if val and val.lower() in masked_text.lower():
                        filtered_missed.append(ent)
                audit_data["missed_entities"] = filtered_missed
                audit_data["passed"] = len(filtered_missed) == 0
                return audit_data
            except Exception as e:
                if attempt == 0:
                    continue
                print(f"    [Critique Agent] Audit skipped after retry: {e}")
                return {"passed": True, "missed_entities": []}
        return {"passed": True, "missed_entities": []}


def _reconstruct_text_from_raw_df(df: pd.DataFrame) -> str:
    """Flatten every non-null cell in a Raw Excel DataFrame into one string,
    row by row — the same shape scan_text_for_entities() reconstructs
    internally, but without skipping any cells, so it's a faithful full-text
    view for the convergence loop below to compare against."""
    row_strings = []
    for _, row_data in df.iterrows():
        cells = [str(v) for v in row_data if pd.notna(v)]
        if cells:
            row_strings.append(" | ".join(cells))
    return "\n".join(row_strings)


def run_convergence_loop(df_raw: pd.DataFrame, global_entities: dict,
                          secret_map: dict, candidate_counters: dict, max_loops: int = 3) -> dict:
    """Pass 4: runs generate_ground_truth() once, then loops compute_accuracy()
    + CritiqueAgent.audit() up to max_loops times, minting new tokens for
    anything either one flags as missed so the *next* iteration (or the final
    write, if this was the last loop) catches it. Mutates and returns
    global_entities. Always-on per wiki/plan-compare-wiki.md Q1 — a
    clean file exits after loop 1 (one ground-truth call + one audit call),
    so the extra cost only applies to files that actually needed fixing."""
    reconstructed_text = _reconstruct_text_from_raw_df(df_raw)
    if not reconstructed_text.strip():
        return global_entities

    ground_truth = generate_ground_truth(reconstructed_text, use_ollama=SLM_USE_OLLAMA)
    critique_agent = CritiqueAgent()

    for loop_idx in range(max_loops):
        masked_text, _ = apply_masking(reconstructed_text, global_entities, secret_map, candidate_counters)
        acc = compute_accuracy(reconstructed_text, masked_text, ground_truth, global_entities)

        audit_res = {"passed": True, "missed_entities": []}
        if SLM_USE_OLLAMA:
            audit_res = critique_agent.audit(reconstructed_text, masked_text)

        passed_llm = audit_res.get("passed", True)
        print(f"    [Convergence Loop {loop_idx + 1}/{max_loops}] "
              f"Precision={acc['precision']:.0%} Recall={acc['recall']:.0%} "
              f"LLM={'PASS' if passed_llm else 'FAIL'}")

        if acc["precision"] >= 1.0 and acc["recall"] >= 1.0 and passed_llm:
            print(f"    [Convergence] Target met at loop {loop_idx + 1}")
            break

        all_missed = acc.get("missed", []) + audit_res.get("missed_entities", [])
        added_any = False
        for m in all_missed:
            val = str(m.get("value", "")).strip()
            val_lower = val.lower()
            if not val or val_lower in global_entities:
                continue
            ptype = str(m.get("type", "SENSITIVE_INFO")).strip().upper()
            token_base = f"[{ptype}]"
            cand_token = get_candidate_token(token_base, val, secret_map, candidate_counters)
            global_entities[val_lower] = cand_token
            added_any = True
        if not added_any:
            break  # nothing new surfaced this round — stop instead of spinning

    return global_entities


def main():
    parser = argparse.ArgumentParser(description="PII Redaction & Unmasking Pipeline")
    parser.add_argument("--unmask", action="store_true", help="Run the unmasking flow instead of redaction (legacy, folder-scan)")
    parser.add_argument("--unmask-file", metavar="FILE_ID", help="Rehydrate a single file by its DB file_id (e.g. FIL-ABC-100001)")
    parser.add_argument("--manifest", metavar="PATH", help="JSON manifest naming the exact files/client/job for this run")
    parser.add_argument("--output-format", choices=["excel", "as_is"], default="excel",
                         help="'excel' (default): flattened .xlsx, same for every input format. "
                              "'as_is': redact the original file in its own format (falls back to "
                              "'excel' per-file on failure). Ignored when --manifest is given — the "
                              "manifest's output_format field takes precedence.")
    args = parser.parse_args()

    if args.unmask_file:
        unmask_file(args.unmask_file)
        return

    if args.unmask:
        unmask_documents()
        return

    print("=" * 60)
    print("PII Redaction Pipeline with Docling Layout Heron (Two-Phase)")
    print("=" * 60)

    # Initialize SLM / spaCy
    init_models()

    if SLM_USE_OLLAMA:
        try:
            slm_base = SLM_OLLAMA_URL.rsplit("/api/chat", 1)[0]
            r = requests.get(f"{slm_base}/api/tags", timeout=5)
            if r.status_code == 200:
                models = [m["name"] for m in r.json().get("models", [])]
                print(f"Ollama server detected. Models: {models}")
            else:
                print("Warning: Ollama server not responding")
        except Exception:
            print("Warning: Could not connect to Ollama — SLM pass will be skipped.")

    SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".eml", ".msg"} | IMAGE_EXTENSIONS

    manifest = None
    file_id_by_name = {}
    default_output_format = args.output_format
    if args.manifest:
        manifest_path = Path(args.manifest)
        if not manifest_path.exists():
            print(f"\nManifest not found: {manifest_path}")
            return
        manifest = _load_manifest(manifest_path)
        default_output_format = manifest.get("output_format", "excel")
        file_id_by_name = {f["filename"]: f["file_id"] for f in manifest["files"]}
        unsupported = [name for name in file_id_by_name if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS]
        if unsupported:
            print(f"Warning: manifest lists unsupported file(s) not processed by this pipeline: {unsupported}")
        input_files = sorted(
            INPUT_FOLDER / name for name in file_id_by_name
            if Path(name).suffix.lower() in SUPPORTED_EXTENSIONS
        )
        input_files = [p for p in input_files if p.exists()]
    else:
        input_files = sorted(
            p for ext in SUPPORTED_EXTENSIONS for p in INPUT_FOLDER.glob(f"*{ext}")
        )

    if not input_files:
        print(f"\nNo supported files found in {INPUT_FOLDER}")
        return

    print(f"Found {len(input_files)} file(s) to process. Output format: {default_output_format}")
    # file_id -> {"redacted_filename": str, "output_format": "excel"|"as_is"} — written to a
    # result sidecar for manifest-driven (web) runs so app.py can patch MySQL with the
    # *actual* output produced (may differ from the upload-time placeholder if "as_is"
    # fell back to Excel for a given file — see plan Open Question #5).
    result_files = {}

    # Job-scoped token allocation: every file in a manifest-driven run shares
    # one secret_map/candidate_counters (keyed by client_id+job_id, not
    # file_id), so the same real value always mints the same token and two
    # distinct values never land on the same token number by coincidence —
    # see wiki/feature-updates/phase-1/plan-rehyrdration-implementation.md
    # Section 3.0.1. Seeded from prior pii_redaction_map rows for this
    # client+job so token allocation stays stable across separate runs, not
    # just within this one. Ad-hoc CLI runs (no manifest, no job) keep the
    # old per-file reset inside the loop below.
    if manifest is not None:
        secret_map, candidate_counters = db.get_secret_map_for_job(manifest["client_id"], manifest["job_id"])
    else:
        secret_map, candidate_counters = {}, {}

    for input_path in input_files:
        if manifest is not None and db.is_batch_cancelled(manifest["process_id"]):
            print(f"\n[CANCELLED] Batch cancelled — stopping before processing {input_path.name}")
            break

        print(f"\n>>> Processing: {input_path.name}")

        raw_xlsx    = OUTPUT_RAW_FOLDER / f"{input_path.stem}_raw.xlsx"
        output_xlsx = OUTPUT_REDACTED_FOLDER / f"{input_path.stem}_redacted.xlsx"
        audit_xlsx  = AUDIT_FOLDER  / f"pii_audit_log_{input_path.stem}.xlsx"
        # Resume-safe: any prior redacted output for this stem (regardless of which
        # format produced it) means this file was already processed.
        existing_redacted = list(OUTPUT_REDACTED_FOLDER.glob(f"{input_path.stem}_redacted.*"))

        if manifest is None:
            secret_map = {}
            candidate_counters = {}

        # -------------------------------------------------------------
        # PHASE 1: SOURCE DOCUMENT to RAW EXCEL
        # -------------------------------------------------------------
        if not raw_xlsx.exists():
            ext = input_path.suffix.lower()
            if ext == ".pdf":
                pdf_to_excel_docling(input_path, raw_xlsx)
            elif ext == ".docx":
                docx_to_excel(input_path, raw_xlsx)
            elif ext == ".doc":
                converted_docx = OUTPUT_RAW_FOLDER / f"{input_path.stem}_converted.docx"
                if convert_doc_to_docx(input_path, converted_docx):
                    docx_to_excel(converted_docx, raw_xlsx)
                else:
                    pd.DataFrame().to_excel(str(raw_xlsx), index=False, header=False, engine='openpyxl')
                    print(f"    [Phase 1] Saved Empty Raw Excel (Word conversion failed): {raw_xlsx.name}")
            elif ext in (".xlsx", ".xls"):
                spreadsheet_to_excel(input_path, raw_xlsx)
            elif ext == ".pptx":
                pptx_to_excel(input_path, raw_xlsx)
            elif ext in (".eml", ".msg"):
                email_to_excel(input_path, raw_xlsx)
            elif ext in IMAGE_EXTENSIONS:
                image_to_excel_ocr(input_path, raw_xlsx)
            else:
                print(f"    [Phase 1] Unsupported file type: {ext} — skipping")
                continue
        else:
            print(f"    [Phase 1] Skipping (already exists): {raw_xlsx.name}")
            print(f"    NOTE: Delete this file to regenerate.")

        # -------------------------------------------------------------
        # PHASE 2: RAW EXCEL to REDACTED OUTPUT (Excel, or "as is" per Open Question #1)
        # -------------------------------------------------------------
        if raw_xlsx.exists() and not existing_redacted:
            print(f"    [Phase 2] Scanning Raw Excel and Applying Masking...")
            df_raw = pd.read_excel(str(raw_xlsx), header=None, engine='openpyxl')
            df_raw = _fix_raw_excel_alignment(df_raw)

            print(f"    [Phase 2] Running SLM inference...")
            scan_text_for_entities.last_stats = []
            global_entities = scan_text_for_entities(df_raw, secret_map, candidate_counters)

            # Print ops summary
            if hasattr(scan_text_for_entities, "last_stats") and scan_text_for_entities.last_stats:
                doc_stats = {"total": 0, "skip_allowlist": 0, "skip_llm": 0, "redact": 0}
                for s in scan_text_for_entities.last_stats:
                    for k in doc_stats:
                        doc_stats[k] += s.get(k, 0)
                print("    [Audit Summary] Operations Report:")
                print(f"      - Total Candidates Evaluated: {doc_stats['total']}")
                print(f"      - SKIP (Allowlist):           {doc_stats['skip_allowlist']}")
                print(f"      - SKIP (LLM Decision):        {doc_stats['skip_llm']}")
                print(f"      - REDACT (LLM Decision):      {doc_stats['redact']}")
                print(f"      - Detailed forensic log saved to SECRETS_FOLDER / pii_forensic_log.jsonl")

            print(f"    [Phase 2.5] Running Convergence Accuracy Engine + Critique Agent...")
            global_entities = run_convergence_loop(df_raw, global_entities, secret_map, candidate_counters)

            audit_records = []
            actual_output_path = None
            actual_output_format = default_output_format

            if default_output_format == "as_is":
                try:
                    actual_output_path, audit_records = write_redacted_as_is(
                        input_path, OUTPUT_REDACTED_FOLDER, global_entities, secret_map, candidate_counters
                    )
                    print(f"    Saved Redacted {input_path.suffix.upper()} (as is): {actual_output_path.name}")
                except Exception as e:
                    import traceback
                    print(f"    [WARNING] As-is redaction failed for {input_path.name}: {e} — falling back to Excel output")
                    traceback.print_exc()
                    actual_output_path = None
                    actual_output_format = "excel"

            if actual_output_path is None and input_path.suffix.lower() in (".xlsx", ".xls"):
                # Excel mode on a native spreadsheet: write through the sheet-preserving
                # writer so a multi-tab workbook comes out with its tabs intact, instead
                # of flattening every sheet into one page. Detection (global_entities)
                # already covers all sheets because Phase 1 flattened them for scanning.
                try:
                    actual_output_path, audit_records = write_redacted_as_is(
                        input_path, OUTPUT_REDACTED_FOLDER, global_entities, secret_map, candidate_counters
                    )
                    actual_output_format = "excel"
                    print(f"    Saved Redacted {input_path.suffix.upper()} (sheets preserved): {actual_output_path.name}")
                except Exception as e:
                    import traceback
                    print(f"    [WARNING] Sheet-preserving Excel write failed for {input_path.name}: {e} — falling back to flattened Excel")
                    traceback.print_exc()
                    actual_output_path = None
                    audit_records = []

            if actual_output_path is None:
                # Excel path — either requested directly, or "as_is" failed and fell back here.
                ts = time.strftime('%Y-%m-%dT%H:%M:%S')
                redacted_rows = []
                for row_idx, row_data in df_raw.iterrows():
                    row_dict = {'excel_row': row_idx}
                    for col_idx, cell_value in enumerate(row_data):
                        if pd.isna(cell_value):
                            continue
                        cell_text = str(cell_value)

                        masked, events = apply_masking(cell_text, global_entities, secret_map, candidate_counters)
                        row_dict[col_idx] = masked

                        for ev in events:
                            audit_records.append({
                                'timestamp': ts, 'filename': input_path.name,
                                'page_num': 1, 'excel_row': row_idx, 'excel_col': col_idx,
                                'token_type': ev['token'], 'original_text': ev['value']
                            })
                    redacted_rows.append(row_dict)

                df_redacted = pd.DataFrame(redacted_rows)
                if not df_redacted.empty:
                    cols = [c for c in df_redacted.columns if c != 'excel_row']
                    cols = sorted(cols)
                    df_redacted = df_redacted[cols]
                    df_redacted.to_excel(str(output_xlsx), index=False, header=False, engine='openpyxl')
                    print(f"    Saved Redacted Excel: {output_xlsx.name} ({len(df_redacted)} rows)")
                    actual_output_path = output_xlsx
                actual_output_format = "excel"

            if audit_records:
                df_audit = pd.DataFrame(audit_records)
                df_audit.to_excel(str(audit_xlsx), index=False, engine='openpyxl')
                print(f"    Saved audit log:      {audit_xlsx.name} ({len(df_audit)} entries)")

            # global_entities is Phase 2's DETECTION result (what should be redacted,
            # from the docling/OCR-extracted Raw Excel) — it does NOT confirm the as-is
            # writer actually located and replaced that text in the real output file.
            # audit_records is the writer's own record of every match it actually made,
            # so it's the source of truth for what was really redacted. A value can be
            # in global_entities/secret_map (token minted) but missing from
            # audit_records if the writer's literal text search failed to find it in
            # the output (OCR/extraction wording mismatch, split across lines, etc).
            # Gate the redaction map on confirmed matches so the reference table never
            # claims a value is redacted when the source document still shows it raw.
            confirmed_tokens = {rec['token_type'] for rec in audit_records}
            unmatched = [vl for vl in global_entities.keys() if global_entities[vl] not in confirmed_tokens]
            if unmatched:
                print(f"    [WARNING] {len(unmatched)} detected value(s) had no confirmed match in the output — excluded from redaction map:")
                for val_lower in unmatched:
                    cand = global_entities[val_lower]
                    orig_val = secret_map.get(val_lower, {}).get('original_value', val_lower)
                    print(f"      - {cand}: '{orig_val[:60]}'")
                    log_forensic_decision(
                        {"id": f"unmatched_{input_path.stem}_{cand}", "type": cand.strip('<*>'),
                         "value": orig_val, "context": "", "pre_decision": "REDACT", "source": "AS_IS_WRITER"},
                        "SKIP", "NO_MATCH_FOUND_IN_OUTPUT"
                    )

            # Save the token<->value map. Manifest-driven (web upload) runs know the
            # client/job/process/file attribution, so it's written to pii_redaction_map
            # for DB-backed rehydration. Ad-hoc CLI runs (no manifest) fall back to the
            # legacy per-file secrets/*.xlsx used by `--unmask`.
            if manifest is not None:
                file_id = file_id_by_name.get(input_path.name)
                if file_id:
                    # Restricted to values confirmed redacted in THIS file (see
                    # confirmed_tokens above) — not the whole job-shared secret_map,
                    # which now also carries values from other files/earlier runs in
                    # the same client+job.
                    pairs = [(secret_map[val_lower]['original_value'], secret_map[val_lower]['candidate'])
                             for val_lower in global_entities.keys()
                             if val_lower in secret_map and global_entities[val_lower] in confirmed_tokens]
                    try:
                        db.insert_redaction_rows(
                            manifest["client_id"], manifest["job_id"], manifest["process_id"], file_id, pairs
                        )
                        print(f"    Saved redaction map:  {len(pairs)} entries -> DB (file_id={file_id})")
                    except Exception as e:
                        print(f"    [ERROR] Failed to save redaction map to DB for {file_id}: {e}")
                    if actual_output_format == "as_is" and input_path.suffix.lower() in IMAGE_EXTENSIONS:
                        try:
                            enc_path = _store_original_encrypted(file_id, input_path)
                            db.set_original_encrypted_path(file_id, str(enc_path))
                            print(f"    Saved encrypted original: {enc_path.name} (for rehydration)")
                        except Exception as e:
                            print(f"    [ERROR] Failed to store encrypted original for {file_id}: {e}")
                    if actual_output_path is not None:
                        result_files[file_id] = {
                            "redacted_filename": actual_output_path.name,
                            "output_format": actual_output_format,
                        }
                else:
                    print(f"    [WARN] {input_path.name} not found in manifest — redaction map not saved")
            else:
                secret_xlsx = SECRETS_FOLDER / f"pii_secret_log_{input_path.stem}.xlsx"
                secret_records = [{'Candidate': v['candidate'], 'Original Value': v['original_value']}
                                  for k, v in secret_map.items() if v['candidate'] in confirmed_tokens]
                df_secret = pd.DataFrame(secret_records)
                df_secret.to_excel(str(secret_xlsx), index=False, engine='openpyxl')
                print(f"    Saved secret log:     {secret_xlsx.name} ({len(df_secret)} entries)")
        elif existing_redacted:
            print(f"    [Phase 2] Skipping (already exists): {existing_redacted[0].name}")

    if manifest is not None:
        result_path = INPUT_FOLDER / f"manifest_{manifest['process_id']}_result.json"
        result_path.write_text(json.dumps({"files": result_files}), encoding="utf-8")

    print("\nDone.")

if __name__ == "__main__":
    main()
