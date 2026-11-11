# Plan: Propagate `uid` Through Every Internal Link

Status: **SUPERSEDED BY A SIMPLER FIX, IMPLEMENTED 2026-08-25** — after
discussion, Section 3.1/3.2's per-link URL-rewriting approach was dropped
in favor of a much smaller change: Flask's `session` is already a cookie
that's sent automatically on every request to every internal path (`/`,
`/dashboard`, `/rehydrate`, `/dms`) — nothing needed to carry `uid` in the
URL for that to keep working. The actual gap was durability: no session
lifetime was configured anywhere, so Flask's default (non-permanent,
cleared when the browser fully closes) applied. Fixed by making the
existing cookie permanent with a 30-day lifetime instead of rewriting 9
links. See Section 9 for what was actually built and verified.
Owner: TBC
Created: 2026-08-25
Updated: 2026-08-25 — implemented the simplified session-lifetime fix;
Sections 3.1/3.2 (link rewriting) and their Open Questions are no longer
being pursued.

---

## 1. Goal

Make the signed-in `uid` reliably follow the user through every internal
page of the app — Dashboard, Rehydrate, DMS, and the "back to Redaction
Pipeline" links on each of those — instead of only being picked up once at
`/` and then relying purely on the browser's session cookie to carry it
silently. Today the URL visibly loses `?uid=...` the moment you click
almost any internal link (confirmed directly — see Section 2), and if the
session cookie ever doesn't persist, that's not just cosmetic: you lose
admin/API access with no way to recover except going back to your
original signed-in link.

This plan does **not** change how `uid` is issued in the first place
(still an external system generating `?uid=<encrypted>` links), how
`crypto_utils.encrypt`/`decrypt` work, or the admin allowlist. It only
changes whether internal navigation keeps that identity visible/usable.

---

## 2. Current State (verified directly, not assumed)

### 2.1 Only 3 routes ever decrypt `?uid=` from the URL

- [app.py:2639](../../../app.py#L2639) — `index()` (`/`)
- [app.py:2722](../../../app.py#L2722) — `api_link_context()` (`/api/link-context`)
- [dashboard.py:61](../../../dashboard.py#L61) — `_dashboard_authorize()`,
  called by both `/dashboard` and `/dashboard/download`

**`/dms` and `/rehydrate` do not accept `?uid=` at all today.** Their
routes never call `_decrypt_uid_param()` — they only read whatever's
already sitting in `session["uid"]`. This matters: even after this plan's
fix appends `?uid=...` to links pointing at those two pages, the pages
themselves wouldn't do anything with it without also being updated (see
Section 3.2).

### 2.2 Only one internal link actually propagates uid today

[app.py:94-106](../../../app.py#L94) `build_dms_href(uid)` re-encrypts
the session uid and appends it to the **external** DMS link (`{{
dms_href }}` at [app.py:812](../../../app.py#L812)). It's the only place
in the codebase that does this. Every other internal link is a bare path:

| Link | Location | Carries uid? |
|---|---|---|
| "Rehydrate a file →" (header nav) | [app.py:813](../../../app.py#L813) | No |
| "Dashboard" (header nav, admin only) | [app.py:814](../../../app.py#L814) | No |
| "Back to Redaction Pipeline" (`/dms` page) | [app.py:2710](../../../app.py#L2710) | No |
| "Dashboard" back-link (`/rehydrate` page, admin only) | [app.py:2278](../../../app.py#L2278) | No |
| "Redaction Pipeline" back-link (`/rehydrate` page) | [app.py:2279](../../../app.py#L2279) | No |
| "Redaction Pipeline" back-link (`/dashboard` page) | [dashboard.py:427](../../../dashboard.py#L427) | No |
| Date-range filter form (`/dashboard`) | [dashboard.py:435](../../../dashboard.py#L435) | No — resubmits only `tab`/`start`/`end` |
| "Download Report (.xlsx)" (`/dashboard`) | [dashboard.py:448](../../../dashboard.py#L448) | No |
| "Back to Redaction Pipeline" (Access Denied page) | [dashboard.py:690](../../../dashboard.py#L690) | No |

### 2.3 Why this can silently break access, not just look wrong

No `session.permanent = True` and no `PERMANENT_SESSION_LIFETIME` is set
anywhere in `app.py`, `dashboard.py`, `serve.py`, or `windows_service.py`
— confirmed by direct search. Flask's default applies: the session cookie
has no `Expires`/`Max-Age`, so browsers treat it as a "session cookie" —
it survives navigation and tabs within the same browser session, but is
cleared the moment the browser fully closes. If that cookie is ever
missing (browser closed and reopened, cookie blocked, cleared, or lost
for any other reason) and the URL no longer carries `?uid=` (because
every internal link dropped it), the user hits:

- [app.py:3447-3450](../../../app.py#L3443) `_require_uid()` → `403
  {"error": "Not authorized — open this app via your normal signed-in
  link first"}` on API endpoints, or
- [dashboard.py:64-66](../../../dashboard.py#L45) `_dashboard_authorize()`
  → the **"Access Denied"** page on `/dashboard`
  ([dashboard.py:656](../../../dashboard.py#L656)), or `403 {"error":
  "Not authorized"}` on `/dashboard/download`.

Whether this is what you actually hit, or the cookie *did* survive and
this is purely a "the URL just looks wrong" observation, is Open Question
#3 — the fix is the same either way, but it changes how urgent this is.

### 2.4 A separate, minor finding while auditing this

[dashboard.py:47-48](../../../dashboard.py#L45) — `_dashboard_authorize()`'s
docstring says it "Returns a Flask response to short-circuit with if
unauthorized, else None," which is backwards from what the code actually
does (returns `None` when unauthorized, the uid int when authorized).
Callers check `is None` correctly either way, so this isn't a functional
bug — just a stale comment worth fixing while this function is being
touched anyway (Open Question #6).

---

## 3. Proposed Design

### 3.1 A shared "carry uid forward" helper

Generalize `build_dms_href()`'s pattern into something every internal
template can use, e.g. in `app.py`:

```python
def _uid_link(path: str, uid) -> str:
    """Append a re-encrypted ?uid=... to an internal path if uid is
    known, so internal navigation keeps carrying the signed-in identity
    forward instead of relying solely on the session cookie (see
    wiki/feature-updates/phase-1/plan-uid-link-propagation-implementation.md)."""
    if uid is None:
        return path
    try:
        encrypted = crypto_utils.encrypt(str(int(uid)))
    except (TypeError, ValueError):
        return path
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}uid={quote(encrypted, safe='')}"
```

Each route that renders a page with internal links passes the relevant
`_uid_link(...)` results into its template, replacing the hardcoded
`href="/"` etc. with `{{ redaction_pipeline_href }}` / `{{
dashboard_href }}` / `{{ rehydrate_href }}` as appropriate.

For the `/dashboard` filter form and download link, simplest fix is a
hidden `<input type="hidden" name="uid" value="{{ uid_enc }}">` (GET form
resubmission) and appending `&uid={{ uid_enc }}` to the download href —
both already have `uid` available server-side from
`_dashboard_authorize()`.

### 3.2 `/dms` and `/rehydrate` need to accept `?uid=` too

Right now only `/`, `/api/link-context`, and `/dashboard` decrypt
`?uid=`. For Section 3.1's links to actually do anything on `/rehydrate`
and `/dms`, those two routes need the same
`_decrypt_uid_param()`-into-session handling `index()` already has
([app.py:2633-2645](../../../app.py#L2633)) — otherwise a correctly
uid-carrying link would land on a page that ignores the very param it was
given.

### 3.3 (Optional, separate lever) Make the session itself longer-lived

Independent of link propagation: setting `app.permanent_session_lifetime`
and calling `session.permanent = True` at the point `uid` is first
decrypted would make the cookie survive a closed-and-reopened browser for
N days, not just until the browser closes. This doesn't replace Section
3.1 (a shared link, or someone without cookies enabled, still needs the
URL to carry uid), but it reduces how often anyone hits the "cookie's
gone" case in the first place. See Open Question #4.

---

## 4. Process diagram (proposed)

```
User clicks a signed-in link:  http://host:5009/?uid=<encrypted>
    │
    ▼
index() decrypts -> session["uid"] = 1580          (already works today)
    │
    │  clicks "Dashboard"
    ▼
GET /dashboard?uid=<re-encrypted>   ◄── NEW: link now carries uid (3.1)
    │
    │  _dashboard_authorize() re-syncs session["uid"] from the URL
    │  (belt-and-suspenders: works even if the cookie didn't survive)
    ▼
    │  clicks "Redaction Pipeline" back-link
    ▼
GET /?uid=<re-encrypted>            ◄── NEW: back-link now carries uid (3.1)
    │
    ▼
index() re-decrypts -> session["uid"] still 1580 either way
```

---

## 5. Open Questions (please answer inline below each)

1. **Scope — every internal link, or just the ones you actually hit?**
   Section 2.2 lists 9 links/forms with no uid propagation. The Dashboard
   → Redaction Pipeline path you hit is 2 of those 9. Fixing all 9 is the
   same pattern repeated, low incremental risk, and closes the gap
   consistently (including `/dms`, which you may not have tried yet but
   has the identical problem). Fixing only the 2 you noticed is a smaller
   diff but leaves the same bug reachable from Rehydrate/DMS.
answer:

2. **Confirm: update `/rehydrate` and `/dms` to accept `?uid=`.**
   Section 3.2 — without this, appending `?uid=` to links pointing at
   those two pages does nothing, since neither route reads it today. Any
   reason not to add the same `index()`-style decrypt to both?
answer: 

3. **Did you actually lose access (saw an error/Access Denied), or did
   you just notice the URL bar change?** Both point at the same fix, but
   this tells us whether the session cookie is genuinely not surviving in
   your environment (worth investigating separately — e.g. an IIS/ARR
   reverse-proxy cookie-forwarding issue, browser cookie settings) or
   whether this is purely a "the address bar looks wrong" report.
   answer:

4. **Also make the session cookie longer-lived (Section 3.3)?** This is
   an independent, complementary fix — `session.permanent = True` +
   `PERMANENT_SESSION_LIFETIME` (pick a duration, e.g. 7 or 30 days) means
   identity survives a closed-and-reopened browser too, not just
   in-session navigation. Want this added alongside the link fix, or is
   link propagation alone sufficient for how this app is actually used?
   If yes, what lifetime?
   answer:

5. **Access Denied page's back-link — carry uid forward too?**
   ([dashboard.py:690](../../../dashboard.py#L690)) A genuinely
   non-admin uid doesn't need re-attaching, but if the denial was caused
   by a lost session (not actual non-admin status), carrying it forward
   would let `/` re-establish the session correctly. Include it for
   consistency, or leave that one link plain since "denied" already means
   something went wrong?
   answer:

6. **Fix the stale `_dashboard_authorize()` docstring (Section 2.4) while
   touching this function?** Pure documentation correction, no behavior
   change — just flagging it as a drive-by since Section 3.2 already
   means editing this function.
   answer:

---

## 6. Database changes needed

**None.** This only changes which query params internal links carry and
which routes read them — no schema, no new tables, no migration.

---

## 7. Task List — ORIGINAL (Sections 3.1/3.2), not pursued

Superseded — see Section 9. Left here for the record of what was
considered and why it wasn't necessary:

- [ ] ~~Add `_uid_link()` helper (Section 3.1) to `app.py`~~
- [ ] ~~Update every internal link/form to carry uid forward (Section
      3.1)~~
- [ ] ~~Add `_decrypt_uid_param()` handling to `/dms` and `/rehydrate` GET
      routes (Section 3.2)~~

## 7b. Task List — ACTUAL (Section 9)

- [x] Set `app.permanent_session_lifetime = timedelta(days=30)` in `app.py`
- [x] Set `session.permanent = True` at all 3 `_decrypt_uid_param()` call
      sites (`index()`, `api_link_context()` in `app.py`;
      `_dashboard_authorize()` in `dashboard.py`)
- [x] Fixed `_dashboard_authorize()`'s stale docstring (Section 2.4 /
      original Q6) while touching this function
- [x] Verified live: the `Set-Cookie` response header now carries an
      `Expires=` date ~30 days out at all 3 call sites (previously no
      `Expires`/`Max-Age` at all — a pure browser-session cookie)
- [x] Verified: a follow-up request to `/dashboard` using only the cookie
      set by an earlier `/?uid=...` visit (no `?uid=` on the `/dashboard`
      request itself) stays authorized — confirms the cookie was already
      doing this, durability was the only real gap
- [x] Full existing regression suite re-run: TL/Vertical/Cluster tabs,
      `.xlsx` download (all 8 sheets), non-admin 403, `/`, `/rehydrate` —
      all still pass
- [x] `python -m py_compile app.py dashboard.py db.py` — clean
- [ ] Update [wiki/architecture.md](../../architecture.md)'s changelog

---

## 8. Out of Scope (for this plan)

- Changing how `uid` links are originally issued (the external system
  generating `?uid=<encrypted>` links) — unchanged.
- Any change to `crypto_utils.encrypt`/`decrypt` or the admin allowlist.
- Server-side session storage (Redis, DB-backed sessions) — Flask's
  default client-side signed cookie is unaffected by this plan either way.
- Rewriting internal links to carry `?uid=` (original Section 3.1) and
  making `/dms`/`/rehydrate` accept `?uid=` (original Section 3.2) — not
  needed once the cookie itself is durable; see Section 9.

---

## 9. What was actually built (2026-08-25)

### 9.1 Why the simpler fix works

Flask's `session` object is a signed, client-side **cookie** — not
something that needs the URL's help to reach other routes. Once
`session["uid"]` is set anywhere (today: `/`, `/api/link-context`, or
`/dashboard`), the browser sends that cookie automatically on every
subsequent request to any path on the same host, `/dashboard` and
`/rehydrate` and `/dms` included — confirmed directly (Section 9.3): a
`/dashboard` request carrying no `?uid=` at all, using only the cookie
from an earlier `/?uid=...` visit in the same test client, is correctly
authorized. Section 2.2's table of 9 uid-less links was real, but it
turns out not to matter for *access* — only for whether the address bar
happens to still show `?uid=...`, which was never actually required for
the session to keep working.

The one genuine gap (Section 2.3): with no `session.permanent` /
`PERMANENT_SESSION_LIFETIME` configured anywhere, Flask's default
non-permanent cookie has no `Expires`/`Max-Age` — browsers drop it the
moment they fully close. That's the only scenario where losing `?uid=`
from the URL would actually matter (no cookie left, and no `?uid=` in the
URL to re-establish one). Fixing durability closes that gap directly,
without touching any of the 9 links.

### 9.2 The change

[app.py](../../../app.py), near the `Flask(__name__)` setup:
```python
app.permanent_session_lifetime = timedelta(days=30)
```
And at each of the 3 places `uid` gets decrypted into the session
(`index()` and `api_link_context()` in `app.py`, `_dashboard_authorize()`
in `dashboard.py`), immediately after `session["uid"] = ...`:
```python
session.permanent = True
```
30 days was picked as a reasonable default, not something you specified a
number for — easy to change if a different lifetime is wanted later.

### 9.3 Verified

- Live `Set-Cookie` header after hitting `/?uid=<encrypted>`:
  `session=...; Expires=Thu, 24 Sep 2026 ...; HttpOnly; Path=/` — was
  previously a bare `session=...` with no expiry at all.
- Confirmed at all 3 call sites (`/`, `/api/link-context`, `/dashboard`).
- A same-client follow-up request to `/dashboard` with **no** `?uid=` in
  its own URL, relying purely on the cookie set by an earlier `/` visit,
  returned 200 (authorized) — directly demonstrates the cookie already
  carries identity across every internal path with zero link changes.
- Full existing test suite (TL tab, Vertical tab, Cluster tab, `.xlsx`
  download's 8 sheets, non-admin 403, `/`, `/rehydrate`) re-run with no
  regressions.
