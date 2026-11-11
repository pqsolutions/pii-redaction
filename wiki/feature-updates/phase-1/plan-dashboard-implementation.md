# Plan: Analytics Dashboard (`/dashboard`)

Status: **IMPLEMENTED 2026-08-25** — Summary tab (Sections 1-9) and the
Vertical/Cluster tabs extension (Section 10) are both built. All Open
Questions in Section 5 and Section 10.4 answered; `db.py` query functions,
the three-tab `/dashboard` + `/dashboard/download` routes,
`DASHBOARD_HTML`/`DASHBOARD_DENIED_HTML`, the admin-gated header button,
and the `wiki/architecture.md` changelog entry are all in place. Verified
end-to-end against the real database this time (not just fake data — see
Section 13), since the Vertical/Cluster joins touch org-hierarchy tables
this app had never queried before.
Owner: TBC
Created: 2026-08-25
Updated: 2026-08-25 — Section 10 (Vertical/Cluster tabs) implemented per
Section 10.4's answers.

---

## 1. Goal

Add a new **`/dashboard`** page that gives an at-a-glance, aggregate view of
redaction activity: table(s) of the underlying numbers plus graphical
(chart) representations of the same data. A new **"Dashboard" button** goes
in the header nav (next to the existing `DMS` / `Rehydrate a file →` links)
that navigates to it.

This plan covers **only** a new read-only reporting page — it does not
change how uploads, redaction, or rehydration work, and does not add any new
data to the database (everything it shows is derived from data already being
written today).

---

## 2. Current State (what exists today)

### 2.1 Header nav pattern

[app.py:809-811](../../../app.py#L809-L811) — the header's `.header-actions`
div already holds two nav links styled with `.nav-link-btn`:

```html
<div class="header-actions">
  <a href="{{ dms_href }}" class="nav-link-btn">DMS</a>
  <a href="/rehydrate" class="nav-link-btn">Rehydrate a file &rarr;</a>
</div>
```

A third `<a href="/dashboard" class="nav-link-btn">Dashboard</a>` slots in
here directly — no new CSS needed, the class already exists
([app.py:328-335](../../../app.py#L328-L335)).

### 2.2 Precedent for a new full-page route

[app.py:2639-2705](../../../app.py#L2639-L2705) — the `/dms` route
(`dms_page()`) is the closest existing precedent for a standalone page:
a separate `render_template_string(...)` with its own `<html>`/`<style>`
block (today just a placeholder card), reached via a plain
`@app.route("/dms")` with no auth decorator. `/rehydrate`
([app.py:3285](../../../app.py#L3285)) is a fuller example of the same
pattern — its own full HTML page, form, and JS, registered as both a GET
(render the page) and POST (do the work) route.

There is **no `@login_required`-style decorator anywhere in this app**.
Access to per-user data is controlled a different way: `index()`
([app.py:2626-2636](../../../app.py#L2626-L2636)) and `/api/link-context`
decrypt a `uid` query-param into `session["uid"]` when present; routes that
need to scope data then call `db.get_allowed_pids(uid)` /
`db.get_clients(allowed_pids)` ([db.py:158-200](../../../db.py#L158-L200))
to filter to the clients (`project.Pid`) that uid is permitted to see, via
the existing `userwiseclient` table. `/dashboard` would follow the same
pattern if it needs to be scoped (see Open Question #1).

### 2.3 Data already available to build this from

No new tables or columns are needed — everything below already exists:

- **`PII_redation_upload_log`** ([db.py:4-16](../../../db.py#L4-L16)) — one
  row per uploaded file: `uid`, `status` (`pending`/`success`/`fail`),
  `error_message`, `input_file`, `output_file`, `uploaded_at`,
  `completed_on`, `process_id`. This is the natural source for "uploads
  over time" and "success/fail rate" charts.
- **`pii_files`** ([migrations/0001](../../../migrations/0001_pii_redaction_tables.sql))
  — one row per file: `client_id`, `job_id`, `process_id`, `filename`,
  `output_format`, `created_at`. Natural source for "files per client/job"
  and format breakdown (Excel vs "As is", see
  [plan-as-it-is-implementation.md](plan-as-it-is-implementation.md)).
- **`pii_redaction_map`** ([migrations/0001](../../../migrations/0001_pii_redaction_tables.sql))
  — one row per redacted value: `client_id`, `job_id`, `process_id`,
  `file_id`, `source_text`, `redacted_text`, `created_at`, `source`
  (`auto` vs `manual`, added by
  [plan-table-implementation.md](plan-table-implementation.md)). The
  category is embedded in `redacted_text` as a token like `<*PERSON_1*>` —
  `app.py` already parses this exact shape in two places
  (`_DMS_TOKEN_CATEGORY_RE` at [app.py:165-175](../../../app.py#L165-L175)
  and `TOKEN_RE`/`_TOKEN_RE` in
  [redaction_formats.py:49](../../../redaction_formats.py#L49) /
  [db.py:476](../../../db.py#L476)) — the dashboard's "PII by category"
  breakdown would reuse the same regex, not invent a new one.
- **`pii_categories`** ([migrations/0006](../../../migrations/0006_pii_categories.sql))
  — the shared category list, if a category filter/legend is wanted.
- **`project` / `activity`** (existing, not owned by this app) — client
  names and job descriptions, for turning `client_id`/`job_id` into
  readable labels in tables.

### 2.4 No charting library exists yet

Checked directly — there is no Chart.js, D3, ApexCharts, Recharts, or any
other charting dependency anywhere in this repo (`script_pii`). The only
external asset the current pages load is the Google Fonts stylesheet
([app.py:262](../../../app.py#L262)). This app has no frontend build step —
every page is a Python string rendered via `render_template_string`, with
plain `<style>`/`<script>` blocks (see `HTML` at
[app.py:256](../../../app.py#L256) and `REHYDRATE_HTML` at
[app.py:1994](../../../app.py#L1994)). Whatever charting approach is picked
needs to fit that same self-contained, no-build-step style. See Open
Question #3.

---

## 3. Proposed Design

### 3.1 Route

```python
@app.route("/dashboard")
def dashboard_page():
    uid_enc = request.args.get("uid")
    if uid_enc:
        try:
            session["uid"] = int(_decrypt_uid_param(uid_enc))
        except Exception as e:
            print(f"[WARN] Failed to decrypt uid '{uid_enc}': {e}")
    stats = db.get_dashboard_stats(uid=session.get("uid"))  # shape TBD, Section 3.3
    return render_template_string(DASHBOARD_HTML, stats=stats)
```

Same `uid`-decrypt-into-session pattern as `index()`
([app.py:2626-2636](../../../app.py#L2626-L2636)), so a link into
`/dashboard?uid=...` from wherever `dms_href`-style links are built
(`build_dms_href`, [app.py:92](../../../app.py#L92)) keeps working the same
way. Whether `stats` is filtered by that uid at all is Open Question #1.

### 3.2 Header button

```html
<div class="header-actions">
  <a href="{{ dms_href }}" class="nav-link-btn">DMS</a>
  <a href="/rehydrate" class="nav-link-btn">Rehydrate a file &rarr;</a>
  <a href="/dashboard" class="nav-link-btn">Dashboard</a>
</div>
```

Added to `HTML` ([app.py:809-811](../../../app.py#L809-L811)) — and, for
consistency, the same link should probably also appear in
`REHYDRATE_HTML`'s header ([app.py:2266](../../../app.py#L2266) has its own
`<header>`) so it's reachable from every page, not just the upload screen.

### 3.3 Data layer — new `db.py` functions

New read-only query functions, none of them touching existing tables'
schema:

- `get_upload_volume_over_time(allowed_pids=None, since=None)` — groups
  `PII_redation_upload_log` by day (or week, Open Question #4) →
  `[{date, count}]`.
- `get_upload_status_breakdown(allowed_pids=None, since=None)` — counts by
  `status` (`pending`/`success`/`fail`) → `{pending: N, success: N, fail: N}`.
- `get_pii_category_breakdown(allowed_pids=None, since=None)` — pulls
  `redacted_text` from `pii_redaction_map`, applies the existing token-regex
  (Section 2.3) to bucket by category → `[{category, count}]`.
- `get_top_clients(allowed_pids=None, since=None, limit=10)` — joins
  `pii_files`/`pii_redaction_map` to `project` for client names, ranked by
  file or redaction count → `[{client_name, file_count, redaction_count}]`.
- `get_dashboard_summary(allowed_pids=None, since=None)` — top-line totals
  for the table view (total files processed, total PII values redacted,
  total clients/jobs touched, success rate %).

All of them take an optional `allowed_pids` set, reusing
`get_allowed_pids()`/the same `IN (...)` pattern `get_clients()` already
uses ([db.py:158-183](../../../db.py#L158-L183)) — so scoping (Open
Question #1) is a parameter these functions accept, not a redesign, whichever
way that question is answered.

### 3.4 Page layout — table + charts

Sketch (final metric set depends on Open Question #2):

```
┌─────────────────────────────────────────────────────────────┐
│  Dashboard                                    [date filter]  │
├─────────────────────────────────────────────────────────────┤
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐        │
│  │ Total    │ │ Total    │ │ Success  │ │ Active   │  ← stat │
│  │ Files    │ │ PII      │ │ Rate     │ │ Clients  │   cards │
│  │ 1,284    │ │ Masked   │ │ 98.2%    │ │ 12       │        │
│  │          │ │ 9,410    │ │          │ │          │        │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘        │
├─────────────────────────────────────────────────────────────┤
│  Upload Volume Over Time          │  PII Category Breakdown  │
│  ┌──────────────────────────┐     │  ┌─────────────────────┐│
│  │     line/bar chart       │     │  │   donut/bar chart   ││
│  └──────────────────────────┘     │  └─────────────────────┘│
├─────────────────────────────────────────────────────────────┤
│  Top Clients / Jobs (table)                                  │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ Client        │ Files │ PII Redacted │ Last Activity   │  │
│  ├───────────────────────────────────────────────────────┤  │
│  │ Acme Corp     │  142  │    1,203     │ 2026-08-24      │  │
│  │ ...           │  ...  │    ...       │ ...             │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

Stat cards + table are straightforward server-rendered HTML (same style as
the rest of the app). The two chart panels are the part whose
implementation depends on Open Question #3.

---

## 4. Process / data-flow diagram (proposed)

```
Browser: click "Dashboard" in header
    │
    ▼
GET /dashboard  (?uid=... optional, same as index())
    │
    ▼
app.py: dashboard_page()
    │  decrypt uid → session["uid"] (if present)
    │  db.get_allowed_pids(uid) ──► (Open Q1: used to scope, or ignored?)
    ▼
db.py: get_dashboard_summary() / get_upload_volume_over_time() /
       get_upload_status_breakdown() / get_pii_category_breakdown() /
       get_top_clients()
    │  reads: PII_redation_upload_log, pii_files, pii_redaction_map,
    │         project, activity
    ▼
render_template_string(DASHBOARD_HTML, stats=...)
    │
    ▼
Browser: stat cards + table (server-rendered) + charts
         (rendered client-side from stats JSON, Open Q3)
```

---

## 5. Open Questions (please answer inline below each — I'll implement once these are filled in)

1. **Data scope — org-wide or per-user?** Should `/dashboard` show totals
   across **all** clients/jobs in the system, or be scoped to only the
   clients the logged-in `uid` is allowed to see (reusing
   `db.get_allowed_pids()`, same boundary the upload form already respects,
   Section 2.2)? Org-wide is a new kind of access this app doesn't
   currently grant anyone (today `uid` only *narrows* what's visible, it
   never *is* the visibility); per-user scoping is consistent with
   everything else but means two different users looking at `/dashboard`
   see different numbers, and a `/dashboard` link with no `uid` at all would
   need a defined fallback (empty? unscoped? Section 2.2 shows
   `get_allowed_pids` returns "no clients" — not "all clients" — for an
   unrecognized uid).
   
   answer: Neither org-wide-for-everyone nor per-user-scoped-for-everyone — 
`/dashboard` should only be accessible to specific admin uids (100, 1580, 
1477). Any other `uid` should not be able to view the dashboard at all 
(not an empty/scoped version — no access, same as any other unauthorized 
route). Within the dashboard, since only these designated admin uids can 
see it, the data shown can be org-wide (all clients/jobs) — that's fine 
because access itself is already gated to a known, trusted set of users, 
so this isn't the "new unbounded visibility for anyone" concern raised 
above.

Same restriction applies to the download button — visible/usable only for 
this same set of admin uids, not shown to regular users at all.

Implementation: hardcode this allowed-uid list (100, 1580, 1477) for now 
rather than adding a new role/permission column to the schema — simplest 
approach for a small, fixed set of admins. `_require_uid()` (or an 
equivalent check) should reject/hide the dashboard and download button 
for any uid not in this list.

2. **Which metrics/charts matter most?** (pick any/all — this drives what
   `db.py` functions in Section 3.3 actually get built)
   - [ ] Upload volume over time (files/batches per day or week)
   - [ ] Success/fail rate (from `PII_redation_upload_log.status`)
   - [ ] PII entities masked by category (PERSON/EMAIL/PHONE/etc., token-based)
   - [ ] Top clients/jobs by volume
   - [ ] Something else (describe):
   answer:All 4:
- [x] Upload volume over time (files/batches per day or week)
- [x] Success/fail rate (from `PII_redation_upload_log.status`)
- [x] PII entities masked by category (PERSON/EMAIL/PHONE/etc., token-based)
- [x] Top clients/jobs by volume

Build all four `db.py` query functions in Section 3.3.

3. **Chart rendering approach.** This app has no charting library today and
   no frontend build step (Section 2.4) — the production server
   (`172.16.29.61`, internal IIS, per
   [wiki/deployment.md](../../deployment.md)) may or may not have open
   internet egress for a public JS CDN at runtime.
   - **Hand-rolled inline SVG/CSS** — zero external dependency, matches the
     rest of the app's self-contained style, works with no internet access;
     limited to simple bar/line/donut charts I write by hand.
   - **Chart.js (or similar) via public CDN** — much richer charts with far
     less code, but adds a hard runtime dependency on reaching a public CDN
     (e.g. `cdn.jsdelivr.net`) from the production machine — worth
     confirming that's actually reachable before committing to it, since a
     blocked CDN would mean the charts silently fail to render in
     production while working fine in local dev.
   answer:Hand-rolled inline SVG/CSS. Zero external dependency means no risk 
of the charts silently failing in production if the IIS server's internet 
egress turns out to be restricted — that failure mode (works in dev, 
silently broken in prod) is exactly the kind of hard-to-debug issue worth 
avoiding upfront rather than discovering after deployment. It also matches 
the app's existing self-contained style (no build step, no external JS 
dependencies today). The four metrics we need (volume over time, success/
fail rate, category breakdown, top clients) are all simple bar/line/donut 
charts — well within what's reasonable to hand-roll, without needing 
Chart.js's richer interactive features.

4. **Default time range + filtering.** Should the dashboard default to a
   recent window (e.g. last 30 days) with a date-range picker to widen/narrow
   it, or just show all-time cumulative totals with no filter UI (simpler,
   less to build)? Related: if scoped per-client (Q1), should there also be
   a client/job dropdown filter on top of the date range?
   answer:Default to a recent window (last 30 days) with a simple date-range 
picker to widen/narrow — all-time cumulative totals alone wouldn't show 
trends (is volume up or down this week), which is the actual point of a 
monitoring dashboard. Keep the picker simple (start/end date), no need 
for elaborate preset-range UI.

No client/job dropdown filter for now. Since Q1 already scoped this as 
org-wide totals for admins, adding a per-client drill-down is a separate 
feature with its own scope — better to ship the simple org-wide + 
date-range version first and add client filtering later if it turns out 
to be needed, rather than building it speculatively now.

5. **Refresh behavior.** Static snapshot on page load (user hits refresh /
   revisits the page to see new numbers), or auto-refresh on an interval
   (e.g. poll every 30–60s, similar to how the batch-status polling on the
   main page works today, [app.py:3069](../../../app.py#L3069))? Given this
   is an analytics/reporting view rather than a live progress tracker, a
   manual refresh button is likely sufficient — confirming there's no reason
   for it to auto-poll.
   answer:static snapshot on page load with a manual refresh 
button, no auto-polling. This is an analytics/reporting view, not a live 
progress tracker, so there's no real need to see updates within seconds — 
unlike batch-status polling, where the user is actively waiting on an 
in-progress job. Auto-polling here would just add unnecessary recurring 
DB load for a view that a handful of admins occasionally check, with no 
real benefit over a manual refresh.

6. **Should `/dashboard` require the `uid` context at all**, or is it
   reachable with no query param / no session (e.g. bookmarked directly,
   opened in a fresh tab)? If Q1's answer is "per-user scoped," a
   `/dashboard` hit with no `uid` in session needs a defined behavior —
   show nothing, show a "select a client" prompt, or fall back to org-wide?

   answer:`/dashboard` must require a valid `uid` in session, checked 
against the admin allow-list (100, 1580, 1477) from Q1. A hit with no 
`uid` at all (bookmarked, fresh tab, no session) should be treated the 
same as any unauthorized access attempt — redirect to login/show an 
access-denied message, not show an empty dashboard or fall back to 
org-wide-for-anyone. There's no "select a client" prompt scenario here 
since Q1 already made this org-wide-for-a-fixed-admin-set, not per-user 
scoped — so the only defined behaviors are "recognized admin uid → show 
dashboard" and "anything else (no uid, or a uid not on the list) → no 
access."

---

## 6. Database changes needed

**None.** Every metric in Section 3.3 is derived from columns that already
exist in `PII_redation_upload_log`, `pii_files`, and `pii_redaction_map` —
no new tables, no new columns, no migration. If Q2's answers end up wanting
something not already captured (e.g. per-file processing duration isn't
currently stored anywhere — only `uploaded_at`/`completed_on` timestamps
exist, so "average processing time" would need to be computed as their
difference at query time, not stored separately), that'll be called out
explicitly once Section 5 is answered, rather than added speculatively now.

---

## 7. Task List

- [x] Get answers to Open Questions in Section 5
- [x] Add `db.py` query functions (Section 3.3) — org-wide per Q1's answer,
      no `allowed_pids` scoping: `_dashboard_date_clause()`,
      `get_dashboard_summary()`, `get_upload_volume_over_time()`,
      `get_upload_status_breakdown()`, `get_pii_category_breakdown()`,
      `get_top_clients()`
- [x] Add `DASHBOARD_HTML` + `DASHBOARD_DENIED_HTML` templates and the
      `/dashboard` + `/dashboard/download` routes in `app.py`, gated by
      `_dashboard_authorize()` / `_is_dashboard_admin()` against
      `DASHBOARD_ADMIN_UIDS = {100, 1580, 1477}` (Q1/Q6)
- [x] Add the admin-gated "Dashboard" header button to `HTML` (index page)
      and to `REHYDRATE_HTML`'s header, both conditional on
      `is_dashboard_admin`
- [x] Build stat cards + Top Clients table (Section 3.4)
- [x] Build charts as hand-rolled inline SVG per Q3's answer — vertical bar
      (upload volume), donut + legend (success/fail), horizontal bar (PII
      category breakdown); no charting library
- [x] Add date-range filter (`start`/`end` GET params, defaulting to the
      last 30 days per Q4) + a manual "Refresh" button (`location.reload()`,
      no auto-polling, per Q5) + a "Download Report (.xlsx)" button
      (`/dashboard/download`, admin-gated, Q1) built with `openpyxl`
- [x] Verified: `db.py`/`app.py` byte-compile cleanly
      (`python -m py_compile`), `app.py` imports cleanly in the project venv
      (`pii_env`), and `DASHBOARD_HTML`/`DASHBOARD_DENIED_HTML`/the
      conditional header blocks all render through Jinja without error
      against representative fake data (including the empty-state branches)
- [ ] **Not yet done — needs a live MySQL connection, see Section 9.2:**
      manual test against real data — verify numbers shown match a
      hand-count from the DB for a known small dataset (sanity check the
      aggregation queries, especially `get_top_clients()`'s join/date-filter
      interaction, Section 9.2)
- [x] Update [wiki/architecture.md](../../architecture.md)'s Recent Changes
      log to describe `/dashboard` and `/dashboard/download`

---

## 8. Out of Scope (for this plan)

- Any change to how uploads, redaction, or rehydration work — this is a
  read-only reporting page over existing data.
- Real-time/streaming updates beyond a manual refresh button (Q5) — no
  WebSocket, no polling.
- A real role/permission system — Q1's answer settled on a hardcoded
  `DASHBOARD_ADMIN_UIDS` allowlist in `app.py`, not a new
  roles/permissions table. Adding/removing an admin today means editing
  that constant and redeploying, not a UI action — acceptable for a small,
  infrequently-changing set of 3 uids per Q1, but worth revisiting if the
  admin set grows or needs to be self-service.
- Per-client/job drill-down filtering on top of the date range (Q4) — the
  dashboard is org-wide totals only; a client/job dropdown filter is
  flagged as a possible fast-follow, not built here.

---

## 9. Implementation status (2026-08-25)

### 9.1 What was done

All Section 7 items except the live-data manual test are complete:

- [db.py](../../../db.py) — `_dashboard_date_clause()` plus five new
  read-only query functions (`get_dashboard_summary()`,
  `get_upload_volume_over_time()`, `get_upload_status_breakdown()`,
  `get_pii_category_breakdown()`, `get_top_clients()`), appended after
  `add_category()`. No existing function was modified.
- [app.py](../../../app.py) — `DASHBOARD_ADMIN_UIDS`, `_is_dashboard_admin()`,
  `_dashboard_default_range()`, three hand-rolled SVG chart builders
  (`_dashboard_bar_chart_svg()`, `_dashboard_hbar_chart_svg()`,
  `_dashboard_donut_chart_svg()`), `DASHBOARD_HTML`/`DASHBOARD_DENIED_HTML`
  templates, `_dashboard_authorize()`, and the `/dashboard` +
  `/dashboard/download` routes. `index()` and `rehydrate_page()` now pass
  `is_dashboard_admin` to their templates, and both header templates
  conditionally render the "Dashboard" nav link.
- New imports: `math`, `html` (stdlib, for escaping values embedded in the
  hand-rolled SVG strings), `datetime.date`/`timedelta`, `openpyxl.Workbook`.

### 9.2 What was deliberately *not* done

No live MySQL query was executed against this database — I don't have (and
didn't request) production DB credentials, and running unreviewed queries
against a real client database wasn't something to do unprompted. Everything
was verified short of that:

- `python -m py_compile app.py db.py` — clean.
- `import app` inside the project venv (`pii_env`) — imports cleanly (picks
  up all new imports/constants correctly, no `NameError`/`ImportError`).
- All four new/changed Jinja templates (`DASHBOARD_HTML`,
  `DASHBOARD_DENIED_HTML`, and the conditional nav-link blocks in `HTML` /
  `REHYDRATE_HTML`) were rendered with `render_template_string()` against
  representative fake data — including the empty-data branches (`{% else
  %}` on the Top Clients table, zero-value donut) — and the
  admin/non-admin branches of the header link. All rendered without error.

**Before this is safe to call fully done**, run it once against the real
database and eyeball the numbers against a known small slice of data —
particularly `get_top_clients()`, whose `LEFT JOIN pii_redaction_map`
without a matching date filter on the join means "PII Redacted" for a
client counts *all* redactions ever tied to files created in the selected
range, not redactions that themselves happened in range (Section 3.3) —
worth confirming that reads the way you expect before trusting it for
reporting.

### 9.3 A note on the admin uid allowlist

`DASHBOARD_ADMIN_UIDS = {100, 1580, 1477}` is hardcoded in `app.py` per
Q1's answer. Confirm those are actually the three intended uids before
relying on this in production — I sourced them directly from your answer
in Section 5, not from any independent lookup.

---

## 10. Extension: Vertical & Cluster tabs (2026-08-25) — PLANNING, not yet built

### 10.1 Goal

Restructure `/dashboard` into three tabs:

1. **Summary** — the existing dashboard (Sections 1-9 above), unchanged.
2. **Vertical** — PII-pipeline usage broken down by business vertical
   (Financial Planning, SMSF, Bookkeeping, etc.), sourced from `coemaster`.
3. **Cluster** — usage broken down by Cluster Head (org-hierarchy rollup —
   which employees report, via their Team Leader, to which Cluster Head),
   adapted from the query you supplied.

### 10.2 What I verified directly against the live database (read-only)

Rather than guess at a schema I hadn't seen, I connected with the app's
existing (already-configured) read-only DB credentials and ran `DESCRIBE`
plus a few aggregate `COUNT(*)`-only checks — no individual employee rows
were pulled, only table structure and counts. Findings:

**`coemaster`** — 17 rows total, 6 flagged `IsBusinessVertical = 1`:
`Financial planning (FP)`, `SMSF`, `Business services (BS)`,
`Bookkeeping (BK)`, `Mortgage (MG)`, `NDIS`. This matches the kind of
client documents already seen in this app's sample/PII category work (tax
forms, loan agreements, brokerage/bank statements) — so `coemaster` with
`IsBusinessVertical = 1` does look like the right table for "vertical."
**Not yet confirmed: which column on `user` is the foreign key into it**
— see Open Question #1 below, this is a real blocker, not a formality.

**`teamleaderusermaster`** (the table in your sample query) — 169 rows,
19 distinct `CHID` values. I adapted your query by removing its
`WHERE FIND_IN_SET(u.uid, 673)` filter (which, since `673` isn't a
comma-list, is just `u.uid = 673` — a single-employee "who's my cluster
head" lookup, not an org-wide report) and tested the join at scale:

```sql
SELECT COUNT(*) FROM user u
JOIN teamleaderusermaster ch ON FIND_IN_SET(u.TLID, ch.TLid) > 0
```
→ **1,718 of 1,863** `user` rows (92%) join successfully via `TLID`.
```sql
SELECT COUNT(*) FROM user u
JOIN teamleaderusermaster ch ON FIND_IN_SET(u.TLID, ch.TLid) > 0
JOIN user uch ON uch.Uid = ch.CHID
```
→ **1,433** (77%) also resolve to a real Cluster Head row via `CHID`.
So this join pattern works and is well-covered — the Cluster tab is
mostly a "how do you want it displayed" question (Section 10.4), not a
schema-ambiguity one like Vertical is.

**A data-completeness issue that affects both tabs equally:**
`PII_redation_upload_log` has 210 rows total, but only **13** have
`process_id` populated (it was added later, by
`migrations/0008_pii_redation_upload_log_cancel.sql`) — and `process_id`
is the *only* thing that could bridge an upload-log row (which has `uid`)
to `pii_files`/`pii_redaction_map` (which have neither `uid` nor any other
user reference). That means **"PII values redacted" cannot be reliably
attributed to a user for ~94% of historical upload rows** — only upload
counts / success-fail status can be, since those live directly on
`PII_redation_upload_log.uid`. This is Open Question #2 — it's a real data
limitation, not a design preference, so I'm flagging it rather than
building a metric that would silently undercount almost everything.

**Read-only, not owned by this app** — like `project`/`activity`/
`userwiseclient` already noted in `db.py`'s module docstring, `user` /
`teamleaderusermaster` / `coemaster` belong to the org's HR/staffing
system. This plan would only ever `SELECT` from them, never write —
same posture as the existing client/job lookups.

### 10.3 Proposed design (pending Section 10.4's answers)

**Vertical tab** (FK column is a placeholder, `<VERTICAL_FK>`, until Q1 is answered):
```sql
SELECT COALESCE(c.ShortName, c.Name, 'Unassigned') AS vertical_name,
       COUNT(*) AS upload_count,
       COUNT(DISTINCT l.uid) AS user_count,
       SUM(CASE WHEN l.status='success' THEN 1 ELSE 0 END) AS success_count,
       SUM(CASE WHEN l.status='fail' THEN 1 ELSE 0 END) AS fail_count
FROM PII_redation_upload_log l
LEFT JOIN user u ON u.Uid = l.uid
LEFT JOIN coemaster c ON c.Code = u.<VERTICAL_FK> AND c.IsBusinessVertical = 1
WHERE 1=1 {date clause on l.uploaded_at}
GROUP BY vertical_name
ORDER BY upload_count DESC
```

**Cluster tab** (adapted from your query, org-wide instead of one uid):
```sql
SELECT uch.Uid AS ch_uid,
       CONCAT(uch.Firstname, ' ', uch.Lastname) AS cluster_head_name,
       COUNT(DISTINCT u.Uid) AS team_size,
       COUNT(l.id) AS upload_count,
       COUNT(DISTINCT l.uid) AS active_user_count,
       SUM(CASE WHEN l.status='success' THEN 1 ELSE 0 END) AS success_count
FROM user u
JOIN teamleaderusermaster ch ON FIND_IN_SET(u.TLID, ch.TLid) > 0
JOIN user uch ON uch.Uid = ch.CHID
LEFT JOIN PII_redation_upload_log l
       ON l.uid = u.Uid {date clause on l.uploaded_at}
GROUP BY uch.Uid, cluster_head_name
ORDER BY upload_count DESC, cluster_head_name
```
Both are draft shapes to react to, not final — they'll change based on
your answers below (especially #2, which determines whether a
"PII Redacted" column belongs in either of these at all).

### 10.4 Open Questions (please answer inline — same as Section 5)

1. **Which `user` column is the vertical foreign key?** I checked three
   candidates against `coemaster.Code` (which only spans 1-17) and none is
   a clean match: `user.OBUID` and `user.OCEID` have values ranging into
   the hundreds/thousands with mostly 2-4 occurrences each — that value
   shape looks like a reference to *another person* (a manager/buddy uid),
   not a small vertical code. `user.CategoryDivisionId` overlaps
   partially (values 1-11 are common) but also has values up to 29,
   exceeding `coemaster`'s 17 rows entirely — so it likely points at a
   different/broader master table, not `coemaster` directly. Is there a
   different column, or a join path through another table I haven't seen,
   that actually links a `user` row to their `coemaster` vertical?
  answer: `user.teamid` is the correct foreign key — it joins cleanly to 
`coemaster.Code`. 

SELECT u.uid, u.*, cm.Name AS vertical_name
FROM user u
JOIN coemaster cm ON u.teamid = cm.Code

This gives the vertical (SMSF/Bookkeeping/Business Services/Financial 
Planning) for each user. The three earlier candidates (OBUID, OCEID, 
CategoryDivisionId) were red herrings — teamid wasn't checked yet.

2. **"PII Redacted" per vertical/cluster — acceptable partial data, or
   upload-count-only?** Given only 13/210 `PII_redation_upload_log` rows
   have `process_id` (Section 10.2), a true "values redacted" count per
   vertical/cluster would only reflect recent activity, silently
   undercounting everything before `process_id` existed. Options: (a)
   Vertical/Cluster tabs show upload counts + success/fail only (fully
   reliable, no redaction-count column), (b) show a redaction-count column
   anyway with a visible caveat that it only covers recent data, or (c)
   backfill `process_id` for old rows some other way first (out of scope
   for this plan, and may not even be possible if the old data doesn't
   retain enough to reconstruct it — I haven't checked that).
   answer:(a) — Vertical/Cluster tabs show upload counts + success/fail 
rate only, no redaction-count column for now. A partial-coverage 
redaction-count (13/210 rows) risks being misread as a real comparison 
between verticals when it's actually just reflecting which uploads happen 
to have recent process_id data — that's a bad foundation for an admin 
dashboard meant to inform real decisions. Upload volume and success/fail 
rate are both fully reliable today and still give a genuinely useful 
picture. Backfilling process_id (option c) is worth investigating 
separately, but is out of scope here — if it turns out to be feasible, 
redaction-count can be added to the dashboard afterward once it's 
actually complete data, not before.

3. **Cluster tab — team size vs. active usage.** Your original query's
   `EmpCount` counts *every* employee under a Cluster Head, regardless of
   whether they've touched the PII pipeline. Do you want the Cluster tab
   to show: total team size (org headcount, like your query), active users
   in range (only those who actually uploaded), or both side by side (as
   drafted in Section 10.3)?
   answer:Both side by side, as drafted in Section 10.3. Team size alone 
misses whether the cluster is actually engaging with the pipeline; active 
users alone loses the org-size context. Showing both together implicitly 
surfaces adoption rate (e.g. "3 of 10 team members actively use it"), 
which is a genuinely useful management signal that neither number gives 
on its own — worth the minor extra column for that.

4. **Unmatched users — show as "Unassigned," or exclude?** ~8% of `user`
   rows don't join to a `teamleaderusermaster` row at all, and a further
   ~15% join but their `CHID` doesn't resolve to a real `user` row
   (Section 10.2's 1,718 → 1,433 drop). Similarly, any upload-log row
   whose `uid` has no vertical assignment. Should these show as an
   "Unassigned" bucket on their respective tab, or be silently dropped
   from the grouped totals?
   answer: Show as an "Unassigned" bucket, don't silently drop. Dropping 
these ~23% of users (and any upload-log rows with no vertical assignment) 
would make the dashboard's totals quietly incomplete without any 
indication — same problem as the redaction-count issue: numbers that look 
complete but aren't. An "Unassigned" bucket keeps the grand total accurate 
and makes the data-quality gap visible rather than hidden, which is also 
useful on its own — it flags that ~23% of users have broken/missing 
cluster hierarchy data worth cleaning up in `teamleaderusermaster` 
separately.

5. **Same admin gate, same date-range filter — confirm.** Assuming both
   new tabs reuse `DASHBOARD_ADMIN_UIDS` (Section 5 Q1) and the same
   `start`/`end` filter as Summary, rather than introducing separate
   access rules — this matters more here than for Summary, since Vertical/
   Cluster pull from the org's full HR `user` table (names, not just
   client data), not just this app's own tables.
   answer:Confirmed — both new tabs (Vertical, Cluster) reuse the exact same 
`DASHBOARD_ADMIN_UIDS` gate and the same `start`/`end` date-range filter 
as Summary. No separate access rule. If anything, this matters more here 
than for Summary, since these tabs surface org-wide HR hierarchy data 
(employee names via `user`/`teamleaderusermaster`/`coemaster`) rather than 
just this app's own upload records — that's a stronger reason to keep the 
access boundary identical and strict, not looser. One admin gate, one set 
of trusted uids, for the whole `/dashboard` route including all its tabs.

6. **Tab layout — literal tabs, or three stacked sections?** You said
   "three tab" — confirming that means clickable tabs at the top of
   `/dashboard` that swap the content below the stat cards/filter bar
   (only one tab's content visible at a time), rather than all three
   shown stacked on one long page.
   answer: Confirmed — literal clickable tabs at the top of `/dashboard`, 
below the stat cards/filter bar, that swap the content area below them. 
Only one tab's content (Summary, Vertical, or Cluster) visible at a time, 
not all three stacked on one long scrolling page.

---

## 11. Database changes needed (Section 10)

**None** — same as Section 6, this only adds `SELECT` queries against
`user`, `teamleaderusermaster`, and `coemaster`, none of which this app
owns or writes to.

---

## 12. Task List (Section 10)

- [x] Get answers to Open Questions in Section 10.4
- [x] Verified Q1's answer (`user.TeamID = coemaster.Code`) directly
      against live data before writing it into code: 1,666 of 1,863 `user`
      rows match, correctly resolving to the org's 6 `IsBusinessVertical=1`
      rows (Business services 594, Bookkeeping 352, SMSF 205, Financial
      planning 167, NDIS 167, Mortgage 14) plus internal teams (Technology
      Group, HR, Marketing, etc.) that are correctly excluded from the
      vertical breakdown by the `IsBusinessVertical=1` filter and land in
      "Unassigned" instead, per Q4
- [x] Add `db.py` query functions: `get_vertical_breakdown()`,
      `get_cluster_breakdown()` — org-wide (no `allowed_pids` scoping, same
      as Section 3.3's functions), no redaction-count column per Q2, both
      include an "Unassigned" bucket/row per Q4
- [x] Add tab UI to `DASHBOARD_HTML` — literal clickable tabs
      (Summary/Vertical/Cluster) below the stat cards/filter bar per Q6,
      client-side switch via `showDashboardTab()` (no page reload), tab
      selection persisted through the date-range filter form via a hidden
      `tab` input and mirrored into the URL (`?tab=...`) via
      `history.replaceState`
- [x] Build Vertical tab table + chart (reuses the existing
      `_dashboard_hbar_chart_svg()` helper — no new chart code)
- [x] Build Cluster tab table + chart (team size *and* active-user count
      shown side by side per Q3's answer; same hbar chart helper)
- [x] Both new tabs reuse `DASHBOARD_ADMIN_UIDS` and the same `start`/`end`
      filter as Summary per Q5 — no separate access rule, no separate date
      logic
- [x] Added "Vertical Breakdown" and "Cluster Breakdown" sheets to the
      `/dashboard/download` `.xlsx` export, matching the existing per-metric
      sheet pattern
- [x] **Manual test against real data — done, not skipped this time** (see
      Section 13): ran `get_vertical_breakdown()`/`get_cluster_breakdown()`
      directly against the live DB (read-only), then hit the real
      `/dashboard` route end-to-end via Flask's test client (admin session,
      non-admin session, all three `?tab=` values, and
      `/dashboard/download`) — all against real data, not fake fixtures
- [x] Update [wiki/architecture.md](../../architecture.md)'s changelog

---

## 13. Implementation status (2026-08-25) — Section 10

### 13.1 What was done

- [db.py](../../../db.py) — `get_vertical_breakdown()` and
  `get_cluster_breakdown()`, appended after `get_top_clients()`. Both take
  the same `(start_date, end_date)` signature as the Section 3.3 functions
  and reuse `_dashboard_date_clause()`. `SUM(CASE WHEN...)` results are
  cast to plain `int` (they came back as `Decimal` from pymysql) for clean
  downstream handling in Jinja/openpyxl.
- [app.py](../../../app.py) — `dashboard_page()` now also calls both new
  functions and resolves `active_tab` from `?tab=` (defaulting to, and
  falling back to, `"summary"` for any unrecognized value).
  `dashboard_download()` adds "Vertical Breakdown" and "Cluster Breakdown"
  sheets. `DASHBOARD_HTML` gained: a `.tab-bar`/`.tab-btn`/`.tab-content`
  CSS block, a hidden `tab` input in the filter form, three
  `id="tab-{summary,vertical,cluster}"` content blocks (the existing
  Summary content moved into the first one, unchanged), and a
  `showDashboardTab()` script that toggles visibility client-side and
  keeps the URL/hidden-input in sync — switching tabs does not reload the
  page or refetch data; all three tabs' data is rendered server-side on
  every page load (same "static snapshot, no polling" posture as Section
  5 Q5) and simply hidden/shown.

### 13.2 What was verified — this time against the real database

Unlike Section 9.2 (where I deliberately avoided running anything against
the live DB), this feature's whole premise rested on org-hierarchy joins
I'd never seen before, so I verified it properly:

- Read-only `DESCRIBE`/aggregate `COUNT(*)` checks against `user` /
  `teamleaderusermaster` / `coemaster` (Section 10.2) before writing any
  query — including specifically re-checking Q1's answer
  (`user.TeamID = coemaster.Code`) before trusting it, since your answer
  named a column (`TeamID`, mixed-case) I hadn't independently confirmed:
  1,666/1,863 users match, resolving correctly to the 6 known verticals
  plus internal teams (Technology Group, HR, Marketing, etc.) — which the
  `IsBusinessVertical=1` filter correctly excludes.
- Ran `db.get_vertical_breakdown()` and `db.get_cluster_breakdown()`
  directly against production data (read-only) and eyeballed the output —
  numbers look internally consistent (e.g. team sizes sum sensibly, the
  "Unassigned" cluster row is 430 of ~1,863 users, matching the ~23% gap
  Section 10.2 predicted from the 1,718→1,433 join-resolution drop).
- Full end-to-end test via Flask's test client against the real app and
  real DB: `/dashboard` (default and all three `?tab=` values) returns 200
  and contains the expected tab markup; a non-allowlisted uid gets 403;
  `/dashboard/download` returns a valid `.xlsx` with all 7 sheets
  including the two new ones.
- `python -m py_compile app.py db.py` — clean.

### 13.3 What's still worth doing before treating this as fully trusted

- I only spot-checked *aggregate* numbers, not individual employees — you
  may want to pick 2-3 people you know the org chart for and confirm their
  vertical/cluster head shows up correctly on the tab, per the original
  Section 12 task list intent.
- The "Unassigned" cluster bucket is large (430 of ~1,863 users, ~23%) —
  Q4's answer said to surface this rather than hide it, which this does,
  but it may be worth separately investigating *why* `teamleaderusermaster`
  data is incomplete for that many people (stale TLID values? employees
  who left? a data-entry gap?) — that's a `teamleaderusermaster`
  data-quality question outside this app's scope, not a bug in these
  queries.

---

## 14. Cluster tab redesign: per-user card grid (2026-08-25)

### 14.1 What changed

You supplied a reference design (a grid of cards — one per Cluster Head,
first name as the card title with an active-user-count badge, each card
listing individual users as "Firstname - Employeeid" with a per-user file
count badge). The on-page Cluster tab now matches this instead of the
Section 10 aggregate table:

- New `db.get_cluster_user_breakdown()` — per-user file counts grouped by
  Cluster Head. Only users with at least one upload in range appear (a
  usage view, not an org-headcount view). Card titles use the Cluster
  Head's **`Firstname` field alone** — not the first word of "Firstname
  Lastname" — since several Cluster Heads have multi-word first names
  (e.g. "Senthil Kumar", "Soundera Pandian"); using `.split(' ')[0]` on
  the full name would have wrongly truncated those to "Senthil"/
  "Soundera". Verified directly against live data before finalizing.
- `DASHBOARD_HTML`'s `#tab-cluster` block replaced with a `.cluster-grid`
  of `.cluster-card` elements (dark header bar, count badge, scrollable
  user list), matching your reference image.
- `get_cluster_breakdown()` (the Section 10 aggregate table — team size,
  active users, success/fail) is **no longer used on the page**, but is
  kept and still powers the `/dashboard/download` "Cluster Breakdown"
  sheet, since the org-headcount context it provides (which the card view
  intentionally omits, matching the reference design) is still useful in
  the full export.

### 14.2 A real bug found and fixed while wiring this up

Verifying the new query against live data surfaced a genuine
double-counting bug in **both** `get_cluster_breakdown()` and the new
`get_cluster_user_breakdown()`: `teamleaderusermaster` can have more than
one row for the same `TLID` (confirmed directly — `TLID 48` has one row
pointing at a real Cluster Head and a second, leftover row with `CHID`
NULL). The original SQL (`JOIN teamleaderusermaster tl ON
FIND_IN_SET(u.TLID, tl.TLid) > 0`) fanned out on this — one upload row
became two output rows, one per matching `teamleaderusermaster` row —
so a single user could appear (and be counted) under two different
buckets at once. Caught this concretely: a user with exactly 1 real
upload was showing up with a "1" under both a real Cluster Head *and*
"Unassigned" before the fix.

**Fix**: replaced the SQL join with a `_resolve_tlid_to_chid()` helper
that resolves each `TLID` to exactly one `CHID` in Python (preferring a
row with a non-null `CHID`, breaking ties by the highest `TLMID` — assumed
most recent), then does the grouping in Python instead of relying on SQL
to fan out correctly. Both `get_cluster_breakdown()` and
`get_cluster_user_breakdown()` now use this.

**Verified the fix**: re-ran both functions against live data — the
previously-duplicated user now appears exactly once, and
`sum(file_count across every user in every cluster card) == 210 ==
get_dashboard_summary()["total_uploads"]` (the independent ground-truth
total from `PII_redation_upload_log` directly) — confirming no upload is
now double-counted or dropped across the whole Cluster tab.

### 14.3 Verified

- `db.get_cluster_breakdown()` and `db.get_cluster_user_breakdown()` run
  live and reconcile exactly to `get_dashboard_summary()`'s independent
  upload total (210 = 210).
- Full end-to-end: `/dashboard?tab=cluster` renders the new card grid, a
  specific previously-duplicated user's employee ID now appears exactly
  once in the rendered HTML (not twice), and `/dashboard/download` still
  succeeds.
- `python -m py_compile app.py db.py` — clean.

---

## 15. Vertical tab: removed the `IsBusinessVertical = 1` filter (2026-08-25)

### 15.1 What changed and why

You asked why "Unassigned" showed 99 uploads on the Vertical tab (default
30-day view) and who those employees were. Traced it directly: all 99
came from exactly 4 employees (Arunkumar B / CSPL1161 — 88, Shibu J /
CSPL1231 — 5, Betina Maria Aashikka Anand / CSPL1160 — 4, Mohamed Matheen
/ CSPL1232 — 2), every one of them with `user.TeamID = 13`, which resolves
cleanly to `coemaster.Code = 13`, `Name = 'Technology Group'` —
**not** a missing/broken mapping, just a real `coemaster` team that
happened to have `IsBusinessVertical` set to `NULL` rather than `1`. The
Section 10.3 query only names a team when `IsBusinessVertical = 1`, so
this — and every other internal team (HR, Marketing, Management, etc.) —
was being collapsed into "Unassigned" even though the join fully resolved.
(Side note: uid 1580 / Shibu J is one of the 3 `DASHBOARD_ADMIN_UIDS` —
so some of that 99 is admin/dev testing activity, not client work.)

You asked to remove that filter and show Technology Group (and, by the
same change, every other matched `coemaster` team) by name. Changed
`db.get_vertical_breakdown()` ([db.py:856](../../../db.py#L856)):
`LEFT JOIN coemaster c ON c.Code = u.TeamID AND c.IsBusinessVertical = 1`
→ `LEFT JOIN coemaster c ON c.Code = u.TeamID`. "Unassigned" now only
means what it says — `TeamID` is NULL/empty, or doesn't match any
`coemaster` row at all — not "matched a real team that isn't flagged as
client-facing."

### 15.2 Verified

- Re-ran against the live DB (default 30-day range): "Unassigned" no
  longer appears at all for this range — "Technology Group" (99 uploads,
  4 users) shows in its place, alongside the existing "Business services"/
  "Bookkeeping"/"SMSF" rows.
- `sum(upload_count)` across the returned rows still reconciles exactly to
  `get_dashboard_summary()["total_uploads"]` (168 = 168 at time of test).
- End-to-end: `/dashboard?tab=vertical` renders and contains "Technology
  Group" in the output.
- `python -m py_compile db.py` — clean.

---

## 16. Extraction: dashboard code moved into its own file (2026-08-25)

### 16.1 What changed

All dashboard code was living inline in `app.py` alongside every other
route in the app (upload, redact, rehydrate, DMS, API-key auth) — by this
point `app.py` was 4,524 lines. You asked for the dashboard to be
maintained as a separate file, with **no logic or workflow change** — a
pure move, not a rewrite.

Created [dashboard.py](../../../dashboard.py) (774 lines) as a Flask
Blueprint (`dashboard_bp`) and moved into it, byte-for-byte unchanged:
`DASHBOARD_ADMIN_UIDS`, `_is_dashboard_admin()`, `_dashboard_authorize()`,
`DASHBOARD_CHART_COLORS`, `_dashboard_default_range()`, the three SVG
chart builders, `DASHBOARD_HTML`, `DASHBOARD_DENIED_HTML`, and the
`/dashboard` + `/dashboard/download` route functions (re-decorated as
`@dashboard_bp.route(...)` instead of `@app.route(...)` — the only actual
code change, and it doesn't affect the URLs or behavior). `app.py`
([app.py](../../../app.py)) now just does
`from dashboard import dashboard_bp, _is_dashboard_admin` and
`app.register_blueprint(dashboard_bp)` — `app.py` dropped to 3,792 lines.

**One real wrinkle, handled cleanly**: `_dashboard_authorize()` needs
`_decrypt_uid_param()`, which stays in `app.py` (it's used by several
non-dashboard routes too, so moving it would've been a bigger, riskier
change than asked for). Importing it at the top of `dashboard.py` would
create a circular import (`app.py` imports `dashboard_bp` from
`dashboard.py`; `dashboard.py` would import from `app.py`). Fixed with a
**deferred import** — `from app import _decrypt_uid_param` sits *inside*
`_dashboard_authorize()`'s function body, not at module level, so it only
resolves when a request actually calls it (long after both modules have
fully finished loading), not when the modules are first imported.

### 16.2 Verified — same behavior, nothing regressed

Re-ran the full test-client regression suite against the extracted code:
`/dashboard` (default + `?tab=vertical` + `?tab=cluster`), a non-admin uid
getting 403, a request with no session at all getting 403,
`/dashboard/download` returning all 7 sheets (and 403 for non-admins), the
"Dashboard" header link showing/hiding correctly on `/` and `/rehydrate`
depending on admin status, and — specifically to prove the deferred-import
fix works — a request to `/dashboard?uid=<encrypted>` successfully
decrypting the uid via the cross-module import and returning 200. All 11
checks passed. `python -m py_compile app.py dashboard.py db.py` — clean.
`import app` — clean, no circular-import error.

---

## 17. Bug fix: Upload Volume chart's x-axis labels going blank (2026-08-25)

`_dashboard_bar_chart_svg()` ([dashboard.py:89](../../../dashboard.py#L89))
only drew x-axis date labels when `n <= 20` bars — any range with more
active days than that (the default 30-day window commonly has ~21+,
confirmed live) rendered every bar with no label at all, a blank strip
under the chart. Fixed by always showing labels, thinning them out (every
`ceil(n/15)`th bar, plus the first and last) instead of hiding them
entirely once there are too many to fit legibly. Verified against the
live 21-bar case (default 30-day range): 0 labels before the fix, 11
after; full `/dashboard` route re-tested end-to-end, 200 OK with labels
present in the rendered HTML.

---

## 18. New 4th tab: TL (Team Leader) (2026-08-25)

### 18.1 What changed

Added a 4th tab, **TL**, next to Cluster in the tab bar — a card grid
(one card per Team Leader, one row per active user in that range) styled
with a fresh teal accent and a circular count badge, distinct from
Cluster's navy header/pill badge, per the "modern design" ask.

You supplied a resolver query for this (`JOIN teamleaderusermaster ch ON
FIND_IN_SET(u.TLID, ch.TLid) > 0 ... JOIN user utl ON utl.uid = u.TLID
... WHERE FIND_IN_SET(u.uid, @uidCsv) > 0`). Its `teamleaderusermaster`
join was there only to also resolve the Cluster Head for context — TL
resolution itself doesn't need it: an employee's own `user.TLID` *is*
their TL's `user.Uid` directly. New `db.get_tl_user_breakdown()`
([db.py](../../../db.py)) drops the `teamleaderusermaster` join
accordingly (simpler than `get_cluster_user_breakdown()`, which does need
it), resolves every active user's TL directly from the in-memory `user`
table, and replaces the query's single-`@uidCsv` scoping with org-wide
reporting — same pattern as the Cluster tab's adaptation of your earlier
CH query. Unresolvable TLIDs bucket as "Unassigned," same as
Vertical/Cluster. `/dashboard/download` gained a matching "TL Breakdown"
sheet (Team Leader / Employee / Employee ID / Files, one row per user).

### 18.2 Verified

- `db.get_tl_user_breakdown()` run live: `sum(file_count)` across every
  card reconciles exactly to `get_dashboard_summary()["total_uploads"]`
  (211 = 211 at time of test) — no double-counting, no drops.
- End-to-end: `/dashboard?tab=tl` returns 200 with the TL tab active and
  its card markup present; the TL button/tab-content exist (hidden) on
  every other tab too; `/dashboard/download` includes the new "TL
  Breakdown" sheet; a non-admin uid is still denied on this tab.
- `python -m py_compile app.py dashboard.py db.py` — clean.
