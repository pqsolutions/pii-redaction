# Plan: API Key Authentication for Programmatic/API Access

Status: **IMPLEMENTED 2026-07-21** — all Open Questions in Section 5 answered
(Option A / static `.env` key, `X-API-Key` header, design (b) merged into
`_require_uid()`, additive/non-breaking for the browser flow). See Section 7
for what shipped and the small implementation decisions made to fill in the
gaps between the answers (401-vs-403 split, and extending the same
API-key-optional pattern to `POST /upload` for attribution per Q5/Q7).
Owner: TBC
Created: 2026-07-21
Updated: 2026-07-21 — implemented.

---

## 1. Goal

Today this app has exactly one authentication mechanism, and it's browser-shaped:
a host system generates a link containing an AES-encrypted `uid`
(`crypto_utils.decrypt()`), the user's browser opens `GET /?uid=...`, `index()`
decrypts it and stashes it in the signed Flask session
([app.py:1956-1964](../../../app.py#L1956-L1964)), and every subsequent request
from that browser rides the session cookie.

That model doesn't fit a caller that isn't a browser — a script, a server-to-server
integration, Postman, etc. — because there's no browser to receive/hold the
encrypted link or the session cookie. We want a second, independent way in:

1. The caller sends an **API key** on the request.
2. The server checks it against the key(s) it knows about.
3. **Match → request proceeds** (as if authenticated). **No key / wrong key →
   request is rejected**, before any route logic runs.

This plan covers *only* that key-issue-and-check mechanism and which routes it
gates — it does not change the existing browser/session flow, which keeps
working exactly as it does today unless an Open Question below says otherwise.

---

## 2. Current State (what exists today)

### 2.1 The only auth that exists today is the session, and it's optional in most places

- `session["uid"]` is set once, in `index()`
  ([app.py:1957-1964](../../../app.py#L1957-L1964)), by decrypting the `uid` query
  param with `crypto_utils.decrypt()` — a fixed, hardcoded AES key
  ([crypto_utils.py:20-27](../../../crypto_utils.py#L20-L27)) shared with a legacy
  .NET helper. There is no login form, no password, no per-request credential —
  just "did this browser session ever see a validly-encrypted `uid`".
- `_require_uid()` ([app.py:2395-2398](../../../app.py#L2395-L2398)) is the only
  gate in the codebase, and it only checks "is *some* uid present in the
  session" — it does **not** check that the uid is allowed to see the specific
  `client_id`/`job_id`/`process_id` being requested (flagged as an open caveat in
  [plan-rehyrdration-implementation.md, Section 5 Q8](plan-rehyrdration-implementation.md#L706-L710)).
- `_require_uid()` gates: `/api/processes`, `/rehydrate` (POST), `/rehydrate/upload`,
  `/api/redaction-map/<process_id>`, `/api/preview/<file_id>`,
  `/api/preview/<file_id>/ocr-region`, `/api/redaction-map/add`,
  `/api/redaction-map/<row_id>` (PUT/DELETE), `/api/categories` (GET/POST).
- **`/upload` has no auth gate at all** — `session.get("uid")` is only checked to
  decide whether to call `db.log_upload()` for attribution; if it's `None` the
  upload still runs, it just isn't logged
  ([app.py:2042-2044](../../../app.py#L2042-L2044), documented in
  [wiki/architecture.md:178-180](../../architecture.md#L178-L180)). Same for
  `/api/clients`, `/api/jobs`, `/download/<filename>`, `/stream/<job_id>`, `/` — no
  check at all.
- Net effect: **anyone who can reach the app over the network can already upload
  and redact files with zero credential today** — the encrypted-`uid` link only
  adds *attribution* for the browser flow, not a hard barrier. Whether that's
  acceptable for the network this app sits on (LAN/VPN-only vs. more broadly
  reachable) is Open Question #6.

### 2.2 No API-key concept exists anywhere in this codebase today

- No `api_key`/`Authorization`/`X-API-Key` handling anywhere in `app.py`, `db.py`,
  or `crypto_utils.py` (checked directly — nothing found).
- `.env` / `.env.example` currently hold exactly one app-wide secret,
  `SECRET_KEY` (Flask session-cookie signing —
  [.env.example:3-6](../../../.env.example#L3-L6)), plus DB credentials. This is
  the closest existing precedent for "one shared secret, one env var" — Section
  3 below reuses that same shape as the default option for the new key.
- No database table for storing credentials/keys exists. `migrations/0001`–`0006`
  cover the redaction/rehydration data model only ([migrations/](../../../migrations)) —
  nothing about auth.

### 2.3 Deployment path the key would travel over

- In production this app sits behind IIS with ARR reverse-proxying to Waitress
  on `127.0.0.1:5009` ([web.config](../../../web.config),
  [setup_iis.ps1](../../../setup_iis.ps1)) — Flask/Waitress itself never sees
  TLS directly; whatever protects the wire is IIS's own binding. Whether that
  external IIS binding is HTTPS-only matters a lot for an API key (a key sent
  over plain HTTP is trivially sniffable) — see Open Question #5.

---

## 3. Proposed Design

### 3.1 Where the key lives (needs your answer — Open Question #1)

**Option A — one static key in `.env` (recommended default, matches the
`SECRET_KEY` precedent exactly).**
Add `API_KEY=` to `.env`/`.env.example`, generated the same way the `.env.example`
comment already tells you to generate `SECRET_KEY`
(`python -c "import secrets; print(secrets.token_hex(32))"` —
[.env.example:4-5](../../../.env.example#L4-L5)). One value, loaded once at
startup, compared against whatever the caller sends. Simplest possible version
of "check the two keys match" — no schema, no new table, no key-management UI.
Rotating it means editing `.env` and restarting the app (Waitress/the Windows
service) — every caller must be updated at the same time, no overlap window.

**Option B — a DB table of keys (`pii_api_keys`), for more than one caller or
zero-downtime rotation.**
New table: `id`, `key_hash` (SHA-256 of the key — **never store the plaintext
key itself**, same principle as a password hash), `label` (e.g. which external
system/integration this key belongs to), `created_at`, `revoked_at` (nullable —
set instead of deleting, so revocation is auditable). Lets you: issue a
different key per caller (so one can be revoked without affecting others),
have two valid keys during a rotation window (issue the new one, wait for the
caller to switch, revoke the old one — no simultaneous-downtime cutover), and
know *which* integration made a given request (useful for the audit trail in
Open Question #3). Costs one migration + a couple of `db.py` helpers
(`get_active_api_key_hashes()` or similar) + some way to actually mint a new row
(a one-off script is enough — a self-service "create API key" endpoint is
almost certainly overkill for how few callers this app will realistically have,
but flag if you disagree).

Recommendation: start with **Option A** unless you already know there will be
more than one external caller, or you need to rotate/revoke a key without a
coordinated restart. It's a strict subset of Option B's behavior (Option B with
exactly one always-valid row), so moving from A to B later isn't wasted work.

### 3.2 How the caller sends the key

An HTTP request header, e.g. `X-API-Key: <key>` — not a query string
(`?api_key=...` gets written into IIS/ARR access logs, and into browser history
if anyone ever opens the URL manually) and not a JSON body field (doesn't work
for GET requests, and mixes transport-auth with the actual request payload).
Header name itself is a naming choice, not a technical constraint — flagged as
Open Question #2 in case you have an existing convention to match (e.g. if the
external caller is a system that already has a standard header it sends).

### 3.3 The check itself

A new helper, mirroring `_require_uid()`'s existing shape:

```python
import hmac

def _require_api_key():
    provided = request.headers.get("X-API-Key", "")
    if not hmac.compare_digest(provided, API_KEY):   # Option A
        return jsonify({"error": "Invalid or missing API key"}), 401
    return None
```

Two details that matter and aren't optional/stylistic:

- **`hmac.compare_digest`, never `==`.** A plain `==` string comparison
  short-circuits on the first mismatched character, so response time leaks how
  many leading characters were correct — a timing attack can recover the key
  byte-by-byte. `compare_digest` runs in constant time regardless of where the
  mismatch is.
- **401, not 403.** The rest of the app uses 403 for "you're identified (have a
  session) but not allowed" (`_require_uid()`'s failure). An API key check is
  "you didn't authenticate at all, or your credential is wrong" — 401 is the
  correct code for that distinction, and matches standard API-key convention
  (this also matters if any HTTP client library treats 401 vs 403 differently,
  e.g. retry-with-different-credential logic).
- The error message is deliberately generic (not "key too short" vs "key
  doesn't match") — doesn't help an attacker narrow down what's wrong with a
  guessed key.

(Option B's version of the same helper hashes `provided` and checks it against
the active, non-revoked rows in `pii_api_keys` instead of one env var — same
shape, same 401/`compare_digest` behavior either way.)

### 3.4 Which routes require it (needs your answer — Open Question #3)

This is the biggest open design fork, because it changes how much code moves.
Three shapes, from smallest to largest:

- **(a) New gate, applied only to routes an external system would plausibly
  call.** The browser-only routes (`/`, `/rehydrate` GET page, `/stream/<job_id>`
  SSE — none of which make sense for a non-browser caller anyway) stay exactly
  as they are; every other route (`/upload`, all `/api/*`, `/rehydrate` POST,
  `/rehydrate/upload`, `/download/<filename>`) gains `_require_api_key()`
  **in addition to** whatever gate it already has (none, or `_require_uid()`).
  This closes the "no auth at all on `/upload`" gap from Section 2.1 as a side
  effect.
- **(b) API key as an *alternative* to the session, on the routes that already
  check `_require_uid()`.** i.e. `_require_uid()` becomes "session uid **OR**
  valid API key" — an API caller gets in the same way a browser session does,
  just via a different credential. Routes that have no gate today
  (`/upload`, `/api/clients`, etc.) are unaffected unless (a) is also applied to
  them.
- **(c) A parallel API surface.** New routes/blueprint dedicated to
  programmatic callers (e.g. everything under `/api/v1/...`), gated by the API
  key only, left entirely separate from the existing browser-facing routes.
  Most work, but cleanest separation if the API's request/response shapes ever
  need to diverge from what the browser SPA expects (e.g. different pagination,
  different field names) — see Open Question #4.

My default recommendation is **(a)**: it's the smallest change, it doesn't
touch the existing session-based flow at all, and it directly fixes the
"`/upload` has no gate" gap noted in Section 2.1 as a natural side effect
rather than a separate task.

### 3.5 Attribution for API-originated requests (needs your answer — Open Question #... see below, folded into #3's answer)

`db.log_upload(uid, input_file)` requires a non-null `uid`
([db.py:78-90](../../../db.py#L78-L90), `PII_redation_upload_log.uid INT NOT
NULL`). A bare API key has no `uid` attached to it. Two ways to fill that gap
if API-originated uploads should still be logged:

- Every API key (Option B) also stores/maps to a fixed internal `uid` to log
  under (e.g. a "service account" row), so `POST /upload` via API key still
  produces an upload-log row, just attributed to that service account rather
  than a real employee.
- Or: API-originated uploads simply skip logging, same as today's "no uid in
  session → skip logging" behavior ([app.py:2042-2044](../../../app.py#L2042-L2044))
  — i.e. the API key only proves "allowed to call this endpoint", it doesn't
  need to prove "on behalf of whom".

This only matters if Option B (per-caller keys) is chosen and/or logging API
uploads is wanted — flag your preference when answering Open Question #1/#3.

### 3.6 Swagger tie-in (small, since Swagger was just added)

flasgger/OpenAPI supports declaring an API-key `securityDefinitions` scheme —
once the header name (Open Question #2) is settled, `/apidocs/` can show a
padlock + "Authorize" button so the key can be set once in the UI and reused
across every "Try it out" call, instead of hand-editing headers per request.
Small addition on top of the Swagger work already done, not a separate effort.

---

## 4. Data flow diagram (as shipped: Option A + design (b))

```
Session-gated route (e.g. GET /api/categories, POST /rehydrate/upload, ...)
    │
    ▼
_require_uid()
    ├─ session["uid"] set?           → yes: proceed (browser flow, unchanged)
    ├─ no session, X-API-Key present → hmac.compare_digest(provided, API_KEY)
    │       MATCH    → proceed
    │       NO MATCH → 401 {"error": "Invalid API key"}
    └─ no session, no header         → 403 {"error": "Not authorized..."} (unchanged)

POST /upload  (never hard-gated — Section 2.1, unchanged)
    │
    ▼
X-API-Key header present?
    ├─ no  → proceeds exactly as today (uid from session if any, else unlogged)
    └─ yes → must be valid (401 if not, checked before any file is saved)
             valid → uid read from the `uid` form field, upload logged under it
```

The existing browser flow (`GET /?uid=...` → session → cookie on every
subsequent request) is completely unaffected — this is a second, independent
door into the same routes, not a replacement for the first one.

---

## 5. Open Questions (need your answers)

1. **Key storage — Option A (one static key in `.env`) or Option B (DB table,
   supports multiple keys/rotation/revocation)?** Section 3.1. Depends on
   whether more than one external system will call this API, and whether you
   need to rotate a key without a coordinated restart.
   answer: one static key in `.env`
   **Implemented**: `API_KEY` added to `.env`/`.env.example`, loaded once at
   startup ([app.py](../../../app.py)). No migration, no new table.

2. **Header name** the caller sends the key in — `X-API-Key` (common
   convention) or something else to match an existing convention on the
   caller's side?
   answer: Yes, X-API-Key header
   **Implemented**: `_api_key_valid()` reads `request.headers.get("X-API-Key")`.

3. **Which routes require the key** — (a) added on top of every route an API
   caller would plausibly hit (my default recommendation), (b) accepted as an
   alternative to the session on the routes that already check
   `_require_uid()`, or (c) a wholly new, separate set of routes just for API
   callers? Section 3.4.
   answer:Option (b)
   **Implemented**: `_require_uid()` ([app.py](../../../app.py)) now passes if
   `session["uid"]` is set **or** `X-API-Key` is present and valid — covers
   every route that already called it (`/api/processes`,
   `/rehydrate/upload`, `/api/redaction-map/*`, `/api/preview/*`,
   `/api/categories`), plus `POST /rehydrate`, which had its own duplicate
   inline `session.get("uid") is None` check — refactored to call
   `_require_uid()` too instead of keeping two copies of the same gate, so it
   gets the API-key alternative automatically. `POST /upload` is a separate
   case — see the note on Q5/Q7 below, since it was never gated by
   `_require_uid()` in the first place (Section 2.1) and (b)'s scope
   ("routes that already check `_require_uid()`") doesn't literally cover it.

4. **If (c) (separate API surface)** — does the response shape need to differ
   at all from what the existing `/api/*` JSON endpoints already return, or
   would it return identical payloads and only exist to be reachable without a
   session? (If identical, (a)/(b) are simpler with no downside.)
   answer: N/A — (c) wasn't chosen (Q3: option (b)).

5. **Attribution** — for `/upload` calls made via API key, do they need to be
   logged to `PII_redation_upload_log` (which needs a `uid`, Section 3.5), or
   is "the key was valid" sufficient with no per-request identity needed?
   answer: yes, take uid from the request body and write it to PII_redation_upload_log.
   **Implemented**: since this answer requires `POST /upload` to recognize the
   API key too (it's the only caller of `db.log_upload()`), the same
   optional-API-key pattern from Q3 was extended to it — *not* a new hard
   gate (Q8 still holds: no key at all → today's exact behavior, unlogged,
   zero credential). If `X-API-Key` is present it must be valid (401 if not,
   checked before any file is saved), and in that case `uid` is read from a
   `uid` form field (there's no session to pull it from) and passed to
   `db.log_upload()` exactly like a browser-session upload today.

6. **Transport** — is the production IIS binding for this app already
   HTTPS-only? An API key sent over plain HTTP can be read by anything on the
   network path between the caller and IIS — if it's HTTP-only today, that
   should be fixed as a prerequisite, not treated as optional.
   answer: now its http ,so http is fine because we use it inteneral so http is fine
   **Noted, no action taken** — internal-only network accepted as-is; no IIS
   binding change made as part of this plan.

7. **Who is "the user" calling through the API** — a person running scripts
   (e.g. via Postman/curl) or another server/system calling this app
   programmatically (server-to-server)? Mostly affects whether you'll ever want
   more than one key (Question 1) and whether a request needs to be attributed
   to a specific caller (Question 5).
   answer:server to server

8. **Should `/upload` (and any other currently wide-open route, Section 2.1)
   require the API key even for the *existing* browser/session flow**, or
   should the browser flow keep working with zero credential exactly as it
   does today, and the API key only ever applies to genuinely new,
   non-browser callers? This decides whether Section 3.4's answer changes any
   *existing* caller's behavior or is purely additive.
   answer: Purely additive. The existing browser/session flow should keep working exactly as it does today with zero change — no new credential required for browser users. The API key applies only to genuinely new, non-browser callers (Postman/curl/server-to-server). This change is additive and does not alter any existing caller's behavior.
   **Implemented**: confirmed — a request with no `X-API-Key` header and no
   session hits exactly the same code path as before on every route (403 on
   the `_require_uid()`-gated ones, unlogged-but-successful on `/upload`).
   The only new failure mode is a request that *does* send an `X-API-Key`
   header and gets it wrong — that's a new caller making a new kind of
   mistake, not an existing caller's behavior changing.

---

## 6. Database changes needed

**None**, if Option A (Section 3.1) is chosen — just a new `.env` value.

**One new migration**, if Option B is chosen:

- `migrations/0007_pii_api_keys.sql` (number depends on what's landed by the
  time this is built) —
  ```sql
  CREATE TABLE IF NOT EXISTS pii_api_keys (
      id         INT AUTO_INCREMENT PRIMARY KEY,
      key_hash   CHAR(64) NOT NULL UNIQUE,   -- SHA-256 hex digest, never the plaintext key
      label      VARCHAR(100) NOT NULL,      -- which integration/caller this key belongs to
      created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
      revoked_at TIMESTAMP NULL
  );
  ```

---

## 7. Task List

- [x] Get answers to Open Questions in Section 5
- [x] Storage: `API_KEY` added to `.env` and `.env.example` (Option A) — no
      migration needed
- [x] Implement `_api_key_valid()` in [app.py](../../../app.py), using
      `hmac.compare_digest`
- [x] Wire the `X-API-Key` header (Q2) into the check
- [x] `_require_uid()` updated to accept session-uid OR valid API key (401 on
      a present-but-wrong key, 403 unchanged when neither is present);
      `POST /rehydrate` refactored off its duplicate inline check onto the
      same helper (Q3)
- [x] `POST /upload`: optional `X-API-Key` (401 if present-but-invalid) +
      `uid` form field for attribution, with zero change to the no-credential
      path (Q5/Q7/Q8)
- [x] Noted Q6 (HTTP-internal accepted as-is) — no infra change made
- [x] Added `ApiKeyAuth` `securityDefinitions` scheme + applied `security:` to
      every route gated by `_require_uid()`, and an optional `security: [{},
      {ApiKeyAuth: []}]` + `uid` field on `/upload`'s spec (Section 3.6)
- [x] Updated [wiki/architecture.md](../../architecture.md) with a new "API
      key (server-to-server callers)" subsection under Upload Logging
- [x] Test: valid key on a gated route succeeds (`/api/categories` → 200)
- [x] Test: missing key on a gated route returns 403 (unchanged)
- [x] Test: wrong key on a gated route returns 401
- [x] Test: wrong key on `/upload` returns 401 before any file is processed
- [x] Test: Swagger spec (`/apispec_1.json`) carries the security scheme and
      per-route `security` blocks correctly
- [ ] Test: existing browser/session flow, end-to-end through a real
      `?uid=...` link — verified via test client only in this session, not a
      real browser session against the live DB/IIS deployment
- [ ] Share the generated `API_KEY` value with whichever system will call in
      server-to-server (not done here — value lives only in local `.env`)

---

## 8. Out of Scope (for this plan)

- Per-`client_id`/`job_id` authorization scoping (i.e. "this API key may only
  see client X's data") — today's session gate doesn't do this either
  (flagged, unresolved, in
  [plan-rehyrdration-implementation.md Section 5 Q8](plan-rehyrdration-implementation.md#L706-L710));
  this plan brings API callers to the same level the browser flow is already
  at, not further.
- Rate limiting / brute-force lockout on repeated failed key attempts — only
  in scope if Open Question 6/7's answers indicate the key's own entropy
  (a `secrets.token_hex(32)`-style random value) isn't considered sufficient
  protection on its own.
- A self-service "create/revoke your own API key" UI — if Option B is chosen,
  minting a new key is assumed to be an ad-hoc script/DB insert run by whoever
  administers this app, not a feature end users interact with, unless you say
  otherwise.
- Changing anything about the existing encrypted-`uid` browser/session flow —
  this plan only adds a second, independent way in.
