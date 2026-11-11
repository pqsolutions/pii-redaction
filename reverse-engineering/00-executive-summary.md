# Executive Summary — PII Redaction Pipeline

*Reverse-engineered from the current codebase for a management audience. No prior
design documents were assumed — this reflects what the system actually does today.*

## What this system is

A web application that automatically finds and blacks out (redacts) personally
identifiable information — names, tax file numbers, bank details, dates of birth,
addresses, etc. — inside client documents (PDFs, Word files, Excel spreadsheets)
before those documents are shared with outside parties, AI tools, or offshore teams.

It also does the reverse: given a redacted document, it can restore the original
values for someone who is authorised to see them ("rehydration").

## Why it exists

The firm handles tax and financial documents (tax returns, super statements, bank
statements) that are full of information protected under the **Privacy Act 1988**
and the **Australian Privacy Principles (APP)**. Before these documents can be
processed by external systems (including AI models) or looked at by anyone outside
a strict need-to-know group, the PII in them has to be removed — reliably, and in a
way that can be proven after the fact (an audit trail of what was redacted and why).

Doing this by hand for every document is slow and error-prone. This system
automates the detection and masking, while keeping every decision it made logged for
compliance review, and keeping a secure map so authorised staff can put the original
information back when it's genuinely needed (e.g. preparing a final tax return that
must show the real numbers).

## How it works, in plain terms

1. **A staff member uploads one or more documents** through a browser page, tagging
   the upload with which client and which job it belongs to (pulled from the firm's
   existing practice-management records).
2. **The system reads the document** — including scanned/image PDFs, using OCR —
   and turns it into structured data it can scan.
3. **A four-layer detection engine** looks for PII:
   - Layer 1: pattern-matching for things with a checksum (tax file numbers, ABNs,
     Medicare numbers, phone numbers, emails) — 100% automatic, no judgement calls.
   - Layer 2: recognising labelled fields on forms ("Date of Birth: ...").
   - Layer 3: a machine-learning model trained to recognise names, organisations
     and other entities from context, not just patterns.
   - Layer 4: a local AI model makes a final call on borderline cases (mask or
     keep), following rules aligned to the Privacy Act.
4. **Every found value is replaced with a token** (e.g. `John Smith` becomes
   `<*PERSON_1*>`), and the same real value always gets the same token throughout a
   document.
5. **The redacted document is handed back for download**, in either a simplified
   Excel format or — for higher-fidelity needs — the original file format
   (PDF/Word/Excel) with the layout intact and only the sensitive content masked.
6. **Every masking decision is logged** (what was redacted, what wasn't, and why) —
   this is the compliance record.
7. **Authorised staff can restore the original values later** ("rehydrate"),
   including restoring values into a brand-new document that was built elsewhere
   from several redacted source files (e.g. a tax return drafted from four
   redacted source documents).
8. **A reviewer can also correct the system's output by hand** — add a value the
   automatic detection missed, or undo one it shouldn't have caught — without
   re-running the whole (slow) detection pass.

## Who uses it, and how

- **Browser users** (staff): open a pre-authenticated link, upload documents tagged
  to a client/job, watch progress live, download the result, and later rehydrate a
  derived document if needed.
- **Other internal systems** (server-to-server): can call the same functionality
  over an API using a shared secret key instead of a browser login — for example, to
  submit a batch of already-known PII values for a specific set of files to be
  masked in bulk, without going through the automatic detection engine at all.

## What makes this more than "find and replace"

- **It's tiered, not a blunt keyword list** — cheap, certain checks run first;
  expensive AI judgement is reserved for ambiguous cases, which keeps it both fast
  and defensible.
- **It's reversible by design** — every redaction can be traced back to its
  original value by someone with the right access, which is what makes the output
  usable for downstream work (like preparing a final tax return) rather than a
  one-way destructive process.
- **It's auditable** — a permanent, append-only log records every decision the
  system made, including PII it chose *not* to redact and why, which is what
  supports a compliance review after the fact.
- **It degrades safely** — if a supporting AI service isn't running, the system
  doesn't fail; it falls back to the more conservative, deterministic layers only.

## Current scope and honest limitations

- The AI models (document layout analysis, entity recognition, and the local
  decision-making model) run on the same machine as the application — there's no
  cloud AI dependency for the redaction itself.
- Visual, position-accurate redaction (blacking out the exact spot on a page) is
  fully built for PDFs; Word and Excel preserve their original formatting too.
  Scanned/image-only documents rely on OCR quality.
- The system is intended for internal/VPN-style network use today — see
  [06-security-and-auth.md](06-security-and-auth.md) for the specific caveats a
  security review would want to know about.

## Where to go next in this document set

| Document | For |
|---|---|
| [01-system-architecture.md](01-system-architecture.md) | The technical shape of the system: components, tech stack, deployment |
| [02-workflows.md](02-workflows.md) | Step-by-step diagrams of every user-facing process |
| [03-pii-detection-pipeline.md](03-pii-detection-pipeline.md) | How the 4-tier detection engine actually decides what's PII |
| [04-data-model.md](04-data-model.md) | What's stored, where, and how records relate to each other |
| [05-module-reference.md](05-module-reference.md) | Per-file technical breakdown, for engineers |
| [06-security-and-auth.md](06-security-and-auth.md) | Authentication, where plaintext PII lives, and known gaps |
| [07-glossary.md](07-glossary.md) | Plain-English definitions of every term used above |
