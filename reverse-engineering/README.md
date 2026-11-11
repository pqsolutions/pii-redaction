# Reverse Engineering — PII Redaction Pipeline

This folder is a from-the-code reconstruction of how `script_pii` actually works
today — architecture, workflows, data model, and security posture — written for a
management audience that needs both the conceptual picture and the technical
detail behind it.

Unlike [`wiki/`](../wiki/home.md), which documents individual feature changes over
time in the order they shipped, this folder documents the system **as a whole, as
it exists right now**, derived directly from reading `app.py`,
`dual_redaction_pipeline.py`, `redaction_formats.py`, `db.py`, `crypto_utils.py`,
the SQL migrations, and `pii_rules.yaml` — not copied from prior documentation.

## Contents

| # | Document | Read this for |
|---|---|---|
| 00 | [Executive Summary](00-executive-summary.md) | What the system does and why, in plain language — start here for a management audience |
| 01 | [System Architecture](01-system-architecture.md) | Components, technology stack, deployment topology |
| 02 | [Workflows](02-workflows.md) | Step-by-step sequence diagrams for every major process (upload, correction, rehydration, bulk API) |
| 03 | [PII Detection Pipeline](03-pii-detection-pipeline.md) | How the 4-tier detection engine decides what's PII, and the audit trail it leaves |
| 04 | [Data Model](04-data-model.md) | Every table, what it's for, and the identifier scheme tying it together |
| 05 | [Module Reference](05-module-reference.md) | Per-file technical breakdown — routes, key functions — for engineers |
| 06 | [Security and Authentication](06-security-and-auth.md) | Auth mechanisms, known gaps, and where plaintext PII lives |
| 07 | [Glossary](07-glossary.md) | Plain-English definitions of every term used above |

## Suggested reading order

- **Management / non-technical**: 00 → 02 → 06 (skip the deep technical sections)
- **Technical / engineering**: 00 → 01 → 03 → 04 → 05 → 06
- **Security review**: 06 first, then 04 (data model) and 01 (deployment topology) for supporting detail

## Diagrams

All diagrams in this folder are [Mermaid](https://mermaid.js.org/) — they render
inline in GitHub, Azure DevOps, and VS Code (with the Mermaid preview extension).
