# PII Redaction Reference

## Purpose

This document lists the categories of personally identifiable information (PII) that must be removed from client files before they are processed using AI and parallel systems.

Use this as a checklist. If any of the items below are visible in a document, redact them before the file leaves the firm. The categories align with the Australian Privacy Principles under the Privacy Act 1988 (Cth) and the Tax File Number Rule 2015.

## 1. Names and personal details

- Full name (given, middle, family or surname)
- Preferred or shortened name (for example, "Kavi" for "Kavindu")
- Previous legal name (maiden name, pre-marriage name, deed-poll change)
- Initials where they could identify (for example, "K P Walker" on a bank record)
- Date of birth in any format (02/07/1985, 2 July 1985, 2-Jul-85)
- Place of birth
- Gender or sex marker
- Marital status, particularly "separated", "divorced", or "widowed"
- Signatures (handwritten image or typed)
- Photograph or scanned identification image

## 2. Contact details

- Residential address - every line, including street, suburb, state, postcode
- Postal address or PO Box
- Mobile phone number
- Home or landline phone
- Work phone, including international format with +61
- Personal email
- Work email
- Emergency contact name, phone, and address

## 3. Australian government identifiers

These are the most sensitive category. Under the Privacy Act and the TFN Rule, treat them as critical and never share them externally without explicit authorisation.

- Tax File Number (TFN) — 9 digits, often labelled "TFN"
- Medicare card number — 10 digits, plus the Individual Reference Number (IRN) and expiry
- Driver licence number — state-specific format
- Australian passport number — 1 to 2 letters followed by 7 digits
- Centrelink Customer Reference Number (CRN) — 9 digits and 1 letter
- Department of Veterans Affairs (DVA) file number
- Unique Student Identifier (USI) — 10 alphanumeric characters
- AHPRA registration number — for health practitioners
- Visa Grant Number or Transaction Reference Number (TRN)

## 4. Business identifiers (when they identify an individual)

- ABN of a sole trader — effectively a personal identifier
- ACN where it ties to a controlled private entity
- Trading name of a sole trader
- Trust name where the individual is settlor, trustee, or beneficiary

## 5. Bank and financial account details

- BSB and account number — always redact as a pair
- Credit or debit card number — full PAN
- Card CVV and expiry date
- SWIFT or BIC code, used for international transfers
- IBAN for foreign accounts
- PayID — which may itself be a phone number, email, or ABN
- Customer reference number or NetBank ID

## 6. Investment and superannuation identifiers

- CHESS Holder Identification Number (HIN) for broker-sponsored holdings
- Issuer-Sponsored Subregister Reference Number (SRN)
- Broker account number
- Superannuation fund member number
- Insurance policy number — Life, TPD, Trauma, Income Protection, or General
- Managed fund or wrap account number

## 7. Employment information

- Employee ID or staff number
- Payroll reference
- Employer name combined with job title or workplace address — the combination identifies even when each alone does not

## 8. Property and lending details

- Rental property address
- Loan or mortgage account number
- Margin loan account
- Tenant name on a lease
- Real estate agent name combined with property reference

## 9. Family and beneficiary information

- Spouse or partner's name, date of birth, and TFN
- Children's names and dates of birth
- Shared-care percentages and custody arrangements
- Beneficiary names — superannuation, life insurance, or wills
- Power of attorney details
- Next of kin

## 10. Sensitive information (APP-defined — extra care)

- Health information, medical conditions, medications, treatment history
- Private health fund name combined with member number
- Disability status
- Racial or ethnic origin
- Religious or philosophical beliefs
- Political affiliations or memberships
- Sexual orientation
- Trade union or professional association membership
- Criminal record
- Biometric or genetic data

## 11. Financial-status information that narrows identity

These are not identifiers on their own, but combined with other fields they can make a person identifiable. Treat as PII when they appear alongside any of the items above.

- HELP or HECS debt balance
- Centrelink payment amounts — Parenting Payment, JobSeeker, Family Tax Benefit
- Specific salary figures next to a job title
- Bankruptcy or insolvency references
- Foreign country of source for income, particularly when combined with passport details

## Rules of thumb

- If in doubt, redact. Over-redacting wastes a minute. Under-redacting is a notifiable data breach.
- Same value, every occurrence. If a TFN appears five times in a document, all five must go — including any version embedded inside a longer reference (for example, CCS-418922367B).
- Pairs travel together. A BSB and account number must both be removed. Either alone, combined with the holder's name, is enough to identify the account.
- Watch the metadata. Document author, last-modified-by, comments, and tracked changes can carry names. Clear these before saving the redacted version.
- Watch the narrative. Identifiers buried inside prose sentences are easy to miss. Read every line, not just the labelled fields.
- Watch the headers and footers. Letterheads, page footers, and watermarks can carry contact details that get overlooked.
- Children's information needs the same care as adults'. Minors' PII is not less sensitive — often more so.

## What to leave alone

These look identifier-like but are not personal information and should not be redacted.

- ATO form-item references (Item 1, Item M2/30, Item IT 8)
- Legal citations (Section 8-1 of ITAA 1997)
- Public company names on a dividend statement — the holding amount is sensitive, the company name is not
- Bank names when not paired with account details
- Generic dates (financial year, statement period)
- Standard form headings and disclaimers