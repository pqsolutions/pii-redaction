# Glossary

Plain-English definitions of every term used in this document set, for readers
without a software background.

| Term | Meaning |
|---|---|
| **PII** | Personally Identifiable Information — any data that could identify a specific person (name, TFN, address, date of birth, etc.) |
| **Redaction / masking** | Replacing a sensitive value with a placeholder token so the original is no longer visible |
| **Token** | The placeholder text that replaces a redacted value, e.g. `<*PERSON_1*>`. The same real value always gets the same token within a file |
| **Rehydration** | The reverse of redaction — putting the original real value back in place of its token, for someone authorised to see it |
| **Tier / Layer** | One of the four detection methods the system runs, in order of cheapest/most-certain to most expensive/most judgement-based (regex → structural labels → NER → LLM) |
| **OCR (Optical Character Recognition)** | Software that reads text out of an image (e.g. a scanned document) rather than a native digital text layer |
| **NER (Named Entity Recognition)** | A type of AI model that identifies things like people's names or organisations from surrounding context, not just fixed patterns |
| **LLM (Large Language Model)** | An AI model (here, running locally via Ollama) used to make a final judgement call on ambiguous cases |
| **Checksum** | A mathematical validation built into identifiers like Tax File Numbers — lets the system confirm a 9-digit string is a *real* TFN, not just any 9 digits |
| **`process_id`** | The identifier for one upload *batch* — e.g. all 4 documents uploaded together for one client's tax return share one `process_id` |
| **`file_id`** | The identifier for one individual file within a batch |
| **Client / Job** | Existing categories from the firm's practice-management system (a `project` and an `activity` record) that every upload is tagged with, so redacted data can be traced back to the right engagement |
| **Manifest** | A small file the web server writes describing exactly which files belong to one upload batch, which the redaction pipeline reads to know what to process |
| **Raw Excel** | The unredacted, structured extraction of a document — an intermediate working file, never meant to be shared |
| **Redacted output** | The final, safe-to-share deliverable with PII replaced by tokens |
| **Audit log** | A compliance record of every redaction made, for review after the fact |
| **Forensic log** | An append-only record of *every* value the system evaluated, redacted or not, with the reason for each decision |
| **Secret log / redaction map** | The record that maps each token back to its original value — required to rehydrate a document later |
| **"As is" output** | A redacted document that keeps the original file's own format and layout (PDF stays PDF with the same visual layout, Word stays Word), as opposed to the flattened, generic "Excel" output |
| **Session** | A browser's temporary, signed-cookie-based "logged in" state, set once a valid pre-authenticated link is opened |
| **API key** | A shared secret string a non-browser system (another server, a script) sends instead of a browser session, to prove it's allowed to use the API |
| **SSE (Server-Sent Events)** | A live, one-way streaming connection the browser keeps open to receive real-time progress updates (used for the live log console and progress bar) |
| **Subprocess** | A separate running program the web server launches and manages — used here so the heavy AI-driven detection work is isolated from the always-running web server |
| **Docling** | The AI/software component that reads a PDF's layout and turns it into structured, table-like data |
| **GLiNER** | The specific AI model used for contextual name/entity recognition |
| **Ollama** | The local server that runs the small language model used for final redaction judgement calls |
| **Tesseract** | The OCR engine used as a last-resort fallback for scanned/image-only PDFs |
| **Conflict (rehydration)** | When a token could mean two different real values, because it came from two different files whose token numbering happened to coincide — the system never guesses in this case, it flags it |
| **Reference table** | The on-screen table showing every original-value → token pair for a file, so a reviewer can check the system's work and correct it if needed |
| **`.env`** | A configuration file holding secrets and connection details (database credentials, encryption keys) that isn't meant to be committed to source control |
| **Migration** | A script that creates or updates database tables in a safe, repeatable way |
| **APP (Australian Privacy Principles)** | The set of privacy rules under the Privacy Act 1988 that this system's redaction rules are designed to comply with |
| **TFN (Tax File Number)** | A unique 9-digit Australian tax identifier — one of the most sensitive categories this system detects |
