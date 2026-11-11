# Configuration

All runtime configuration lives at the top of `dual_redaction_pipeline.py` and in `pii_rules.yaml`.

---

## Core Pipeline Settings

| Parameter | Default | Description |
|-----------|---------|-------------|
| `INPUT_FOLDER` | `./files/` | Drop input PDFs here |
| `OUTPUT_FOLDER` | `./files/output/` | Parent output folder |
| `OUTPUT_RAW_FOLDER` | `./files/output/raw/` | Raw extracted Excel files |
| `OUTPUT_REDACTED_FOLDER` | `./files/output/redacted/` | Final redacted Excel files |
| `AUDIT_FOLDER` | `./audit/` | Compliance audit logs |
| `SECRETS_FOLDER` | `./secrets/` | PII token-to-value maps |
| `UNMASKED_FOLDER` | `./unmasked/` | Restored documents |
| `DPI` | `150` | OCR render resolution |
| `OCR_LANG` | `'en'` | Tesseract language |

---

## GLiNER Settings

| Parameter | Default | Description |
|-----------|---------|-------------|
| `GLINER_MODEL` | `urchade/gliner_multi_pii-v1` | HuggingFace model ID |
| `GLINER_CONFIDENCE_THRESHOLD` | `0.85` | Minimum score to consider an entity a candidate |
| `MAX_IMG_SIDE` | `2500` | Max pixel dimension for image resizing before OCR |

GLiNER processes text in 200-word chunks with 50-word overlap to preserve context across boundaries. Labels it detects: `PERSON`, `ORGANIZATION`, `FINANCIAL`, `COMPANY`.

---

## Ollama / SLM Settings

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SLM_USE_OLLAMA` | `True` | Enable/disable LLM tier |
| `SLM_OLLAMA_BASE` | `http://localhost:11434` | Ollama server base URL |
| `SLM_OLLAMA_MODEL` | `gemma3:4b` | Model to use for decisions |
| `SLM_NUM_CTX` | `4096` | Context window (tokens) |
| `SLM_CHUNK_CHAR_LIMIT` | `500` | Max characters per LLM chunk |
| `SLM_CHUNK_TIMEOUT` | `600` | Seconds before a single chunk request times out |
| `SLM_NUM_THREAD` | `14` | CPU threads for Ollama inference |
| `SLM_CONFIDENCE_THRESHOLD` | `0.7` | Minimum confidence for LLM MASK decisions |

To switch model (e.g., to a larger or different model):

```python
SLM_OLLAMA_MODEL = "llama3.2:3b"   # or any model pulled via `ollama pull`
```

---

## PII Token Types

These are the replacement tokens inserted into redacted output:

| Token | Covers |
|-------|--------|
| `[PERSON]` | Full names, individuals |
| `[SIGNATURE]` | Signature images or text |
| `[IMAGE]` | Embedded images |
| `[EMAIL]` | Email addresses |
| `[PHONE]` | Phone numbers |
| `[ADDRESS]` | Street addresses |
| `[DOB]` | Date of birth |
| `[PLACE_OF_BIRTH]` | Birth location |
| `[GENDER]` | Gender information |
| `[MARITAL_STATUS]` | Marital status |
| `[GOVERNMENT_ID]` | TFN, Medicare, passport |
| `[BUSINESS_ID]` | ABN, ACN |
| `[BANK_DETAILS]` | BSB + account numbers |
| `[INVESTMENT_ID]` | Fund/investment reference numbers |
| `[EMPLOYMENT_ID]` | Employer identifiers |
| `[PROPERTY_DETAILS]` | Property addresses or titles |
| `[FAMILY_INFO]` | Dependent/family details |
| `[SENSITIVE_INFO]` | Health, religion, ethnicity |
| `[FINANCIAL_STATUS]` | Financial position details |

Each token is suffixed with a counter, e.g. `[PERSON:001]`, `[PERSON:002]`, so the same entity always maps to the same token within a document.

---

## pii_rules.yaml

The YAML file controls what is and isn't redacted without touching Python code. It is hot-reloaded — changes take effect on the next pipeline run without restarting the server.

### Sections

**GLOBAL_RULES**
Things that are never redacted regardless of tier:
- Government bodies: ATO, ASIC, Medicare (as organisations)
- Form field labels
- Generic dates (financial year dates, statement periods)
- Dollar amounts of $0

**PERSON_RULES**
- Redact: Full names, spouses, dependents, next-of-kin
- Skip: Generic single given names without a surname

**ORGANIZATION_RULES**
- Skip: ASX-listed public companies (unless combined with a personal account number)
- Redact: Private companies, trusts, or partnerships linked to an individual

**FINANCIAL_RULES**
- Do NOT redact financial amounts (dollar figures are not PII under APP)

**ALLOWLIST**
23 hard-coded phrases that are always skipped:
```
Commonwealth Bank, ANZ, Westpac, NAB, Macquarie Bank,
Australian Taxation Office, Medicare Australia, ASIC,
Australian Super, REST Super, Hostplus, AustralianSuper,
Tax File Number, ABN, ACN, BSB, TFN,
given name, family name, date of birth, address, phone number
```

**STRUCTURAL_LABELS**
Maps field label text to PII token:
```yaml
"given name":     PERSON
"family name":    PERSON
"date of birth":  DOB
"dob":            DOB
"email":          EMAIL
"mobile":         PHONE
"tfn":            GOVERNMENT_ID
"tax file number": GOVERNMENT_ID
"bsb":            BANK_DETAILS
"account number": BANK_DETAILS
```

**TIER_1_REGEX**
Defines pattern groups used by Tier 0 (regex + checksum):
- `TFN_PAT` — 9-digit TFN with Luhn-style checksum
- `MEDICARE_PAT` — 10-digit Medicare with IRN validation
- `ABN_PAT` — 11-digit ABN with weighted checksum
- `ACN_PAT` — 9-digit ACN with weighted checksum
- `PHONE_PAT` — AU phone: +61, 02–09, 04xx, 13/18 numbers
- `EMAIL_PAT` — Standard email regex
- `BSB_ACCOUNT_PAT` — `NNN-NNN` BSB + 6–10 digit account

---

## Web Server Settings (`app.py`)

| Setting | Value | Description |
|---------|-------|-------------|
| Port | `5009` | HTTP port |
| Host | `0.0.0.0` | Listens on all interfaces |
| Max upload | `200 MB` | Per-file size limit |
| Allowed extensions | `.pdf .xlsx .xls .docx .doc` | Rejected files return 400 |

To change the port, edit the last line of `app.py`:

```python
app.run(host="0.0.0.0", port=5009, debug=False, threaded=True)
```

And in `serve.py`:

```python
port = 5009
```

---

## Linked Apps (environment variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `AGATE_MERGER_URL` | `http://localhost:5015` | Agate Merger. The review page's **Next** button (enabled once the review box is ticked) sends the process's redacted files to `<AGATE_MERGER_URL>/api/intake`, then opens `<AGATE_MERGER_URL>/?uid=<encrypted uid>&intake=<token>` in a new tab with the files already in Agate's upload list. Production: `http://172.16.29.31:5015`. |
| `AGATE_INTAKE_API_KEY` | *(empty — Next shows a "not configured" error)* | Shared secret sent as `X-Agate-Intake-Key`; must equal Agate Merger's `AGATE_INTAKE_API_KEY`. |
