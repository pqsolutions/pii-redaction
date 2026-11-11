# Plan: Manual Review Timer (Select → last Download)

Status: **IMPLEMENTED 2026-09-17 — migration `0009` still to be run on each DB; browser click-through (Step 6) still to do.**
All Open Questions in Section 5 answered. Section 8 lists what shipped and where it differs from Section 3.
Owner: TBC
Created: 2026-09-17
Updated: 2026-09-17 — answers received, implemented (Section 8); idle tracking explained (Section 9).

---

## 1. Goal

A redaction run has two parts:

1. **System time.** The pipeline runs by itself. This is already recorded: each
   file gets a `PII_redation_upload_log` row with `uploaded_at` (set on insert in
   `db.log_upload()`) and `completed_on` (set in `db.update_upload_status()`),
   and every row in a batch shares one `process_id`
   (see [db.py:87-151](../../db.py#L87-L151)).
2. **Manual review time.** After the pipeline finishes, the user checks the
   preview, uses the **Select** tool to box missed values, runs Add & Re-redact,
   ticks the acknowledgement box and downloads. **Nothing measures this today.**

We want to measure (2):

- A **timer shown in the right corner** once the system process has finished.
- It **starts** when the user clicks the **Select** button
  (`#select-tool-btn`, [app.py:943](../../app.py#L943)).
- It **ends** when the user clicks the **last Download** button
  (`.dl-btn` inside `#result-list`, [app.py:1295-1340](../../app.py#L1295-L1340)).
- It **pauses** when the user switches to another tab.
- It **pauses** when the mouse and keyboard have been idle for **15 seconds**.
- The final value is **saved to a DB table** against the `process_id`, next to
  the system time we already store, so both can be reported together.

---

## 2. Current State (what exists today)

| Piece | Where | Notes |
|---|---|---|
| Pipeline run starts | `runPipeline()` [app.py:1185](../../app.py#L1185) | POST `/upload` returns `job_id`, which is the `process_id` |
| System process finishes | SSE `done` listener [app.py:1234-1244](../../app.py#L1234-L1244) | Calls `showResults()` and `loadResultDetail(job_id)` |
| Select tool | `selectToolBtn` click listener [app.py:1655-1659](../../app.py#L1655-L1659) | Toggles `selectMode`. It is a toggle, so it can be clicked many times |
| Download buttons | `renderDownloadList()` [app.py:1295](../../app.py#L1295) | One `.dl-btn` per file, plus a **Download All (ZIP)** button when there is more than one file. All are locked until `#ack-checkbox` is ticked |
| Download click guard | [app.py:1282-1285](../../app.py#L1282-L1285) | Blocks the click when the box isn't ticked |
| Re-lock after edits | `relockDownload()` [app.py:1882](../../app.py#L1882) | Add, edit or delete of a row unticks the box, so the user may download, edit again and download again |
| System-time log | `PII_redation_upload_log` | Per-file rows: `uploaded_at`, `completed_on`, `process_id`, `uid` |
| Auth helper | `_require_uid()` | Used by every `/api/*` route that writes data |

**Consequences for the design:**

- "Last download" is not a single event. A batch can have N per-file buttons
  plus a ZIP, and edits re-lock downloading. So the timer must save **on every
  download click**, and the stored value is whatever was saved at the latest one
  (see Q2).
- Only the browser knows about idle time and tab switches, so the timer is
  measured **client-side** and the server stores it after sanity checks.

---

## 3. Proposed Design

### 3.1 Timer state machine (frontend)

```
            first click on Select
 NOT_STARTED ─────────────────────▶ RUNNING ◀──────────────┐
                                     │  │  │               │ mouse/keyboard activity
              15 s no input ─────────┘  │  └── tab hidden  │ (and tab visible)
                     ▼                  │         ▼        │
               PAUSED(idle) ────────────┼──── PAUSED(tab) ─┘
                                        │
                   Download click ──────┘──▶ PAUSED(downloaded)
                                               │ any further activity
                                               └──▶ RUNNING (see Q2)

 New "Run Redaction" or "Clear all" ──▶ reset to NOT_STARTED
```

Implementation notes:

- **Time comes from `performance.now()` deltas, not from counting
  `setInterval` ticks.** Browsers throttle timers in background tabs, so a tick
  counter drifts. `activeMs` grows by `now - segmentStart` whenever a running
  segment closes. The 1-second interval only repaints the display and runs the
  idle check.
- **Activity events.** `mousemove`, `mousedown`, `keydown`, `wheel`,
  `touchstart` and `scroll` are listened for on `document`, with `capture: true`
  and `passive: true`. Each one updates `lastActivityAt`. (Shipped without
  throttling; the handler only stores a number. See Section 9.)
- **Idle rule.** On each 1 s tick, if the timer is `RUNNING` and
  `now - lastActivityAt >= 15000`, the timer pauses. The segment is closed at
  **`lastActivityAt`**, not `now`, so the 15 idle seconds are not counted
  (see Q3).
- **Tab switch.** On `document.visibilitychange` to hidden, the segment is
  closed at `now` and the state becomes `PAUSED(tab)`. Whether `window.blur`
  (switching to another application with the tab still visible) also pauses
  is Q4.
- **Resume.** The next activity event while the tab is visible resumes the
  timer.
- **Pause counters.** `idlePauses` and `tabPauses` are kept for reporting.

### 3.2 Timer UI

A small pill on the right side of the **Output** section label
([app.py:925](../../app.py#L925)). It stays hidden until the system process
finishes (the `done` event).

```
Output                                   ⏱ Review time 00:02:31  ● Running
Output                                   ⏱ Review time 00:02:31  ○ Paused – idle
Output                                   ⏱ Review time 00:00:00  ○ Click “Select” to start
```

Format is `HH:MM:SS`. Colours come from the existing CSS variables. Placement
is Q7.

### 3.3 Saving the time (backend)

**New migration** `migrations/0009_pii_process_review_time.sql`:

```sql
-- Manual review time per batch: from first click on the Select tool to the
-- last Download click, excluding idle (15 s without input) and tab-hidden
-- time. System (pipeline) time stays in PII_redation_upload_log
-- (uploaded_at -> completed_on); both join on process_id.
CREATE TABLE IF NOT EXISTS pii_process_review_time (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    process_id        VARCHAR(50) NOT NULL,
    uid               INT NOT NULL,
    active_seconds    INT NOT NULL DEFAULT 0,
    idle_pause_count  INT NOT NULL DEFAULT 0,
    tab_pause_count   INT NOT NULL DEFAULT 0,
    download_count    INT NOT NULL DEFAULT 0,
    started_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_download_at  TIMESTAMP NULL,
    updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_review_time_process_uid (process_id, uid)
);
```

(`0008` is the latest migration today. If another migration lands first, use
the next free number.)

**`db.py`**: add `save_review_time(process_id, uid, active_seconds, idle_pauses,
tab_pauses)`. It runs `INSERT ... ON DUPLICATE KEY UPDATE`, sets
`active_seconds = GREATEST(active_seconds, VALUES(active_seconds))` so an
out-of-order request can't lower it, adds 1 to `download_count`, and sets
`last_download_at = CURRENT_TIMESTAMP`. Also update the module docstring's list
of expected migrations.

**`app.py`**: add a new route.

```
POST /api/process/<process_id>/review-time
Body: { "active_seconds": 151, "idle_pauses": 2, "tab_pauses": 1 }
```

- Guarded by `_require_uid()`, like `/api/batch/<id>/cancel`.
- Validates that the values are non-negative integers. `active_seconds` is
  capped at 24 h.
- Checks that the `process_id` exists and belongs to the session `uid`, using
  the same ownership check as the existing process routes.
- DB errors are logged with `[WARN]` and return 500. The download itself never
  depends on this call.
- New swagger file `docs/swagger/review_time.yml`, wired with `@swag_from`.

**Frontend send.** In the existing `resultList` click handler
([app.py:1282](../../app.py#L1282)), when a `.dl-btn` click is allowed:
pause the timer as `PAUSED(downloaded)`, then
`fetch(url, { method:'POST', keepalive:true, ... })`. `keepalive` lets the
request finish even while the browser handles the attachment download.

### 3.4 Data flow

```
Browser                                   Flask (app.py)                 MySQL
───────                                   ──────────────                 ─────
Run Redaction ── POST /upload ──────────▶ log_upload() per file ───────▶ PII_redation_upload_log
                                                                          (uploaded_at)
SSE "done" ◀──── pipeline finishes ─────  update_upload_status() ──────▶ (completed_on)   ← SYSTEM TIME
show timer pill (NOT_STARTED)
click Select ─▶ RUNNING
  idle 15 s / tab hidden ─▶ PAUSED ─▶ activity ─▶ RUNNING
tick ack + click Download ─▶ PAUSED(downloaded)
  └─ POST /api/process/<id>/review-time ─▶ save_review_time() ─────────▶ pii_process_review_time
                                                                          (active_seconds)  ← MANUAL TIME
```

---

## 4. Files to change

| File | Change |
|---|---|
| `migrations/0009_pii_process_review_time.sql` | **New** table (Section 3.3) |
| `db.py` | `save_review_time()`, docstring migration list |
| `app.py` (Python) | `POST /api/process/<process_id>/review-time` |
| `app.py` (HTML/CSS) | Timer pill markup and styles next to the Output label |
| `app.py` (JS) | Timer state machine, activity and visibility listeners, start hook in the `selectToolBtn` listener, save hook in the `resultList` click handler, reset in `runPipeline()` and `clearFiles()` |
| `docs/swagger/review_time.yml` | **New** |
| `wiki/architecture.md` | Short "Review time tracking" note |

---

## 5. Open Questions (need your answers)

1. **Start trigger.** (a) Start only on the **first click on the Select tool
   button**, as requested. If a user reviews the preview and downloads without
   ever using Select, no time is recorded. (b) Start as soon as results appear
   (SSE `done`). (c) Start on the **first of** Select click, table edit, Add &
   Re-redact, or ack checkbox tick, so every reviewed batch gets a time.
   Recommendation: **(c)**. It matches the intent without losing batches where
   nothing was missed.
   answer:(a) Select button first click

2. **What counts as the "last download".** A batch has several download buttons
   (per file, plus ZIP), and edits re-lock downloading. (a) Stop for good on the
   **first** download click. (b) Save on **every** download click. The timer
   pauses and resumes if the user keeps working, and the DB keeps the value from
   the latest click. (c) Stop only once **every file** (or the ZIP) has been
   downloaded. Recommendation: **(b)**.
   answer:(b)

3. **Idle seconds.** When the timer pauses after 15 s without input, should
   those 15 s be (a) **removed** from the time (rewind to the last activity) or
   (b) **kept**? Recommendation: **(a)**. Also, should the 15 s threshold live
   in `.env` (e.g. `REVIEW_IDLE_SECONDS=15`) or be fixed in code?
   answer:Idle 15 seconds remove and it log in the table also for understanting and  15-second threshold .env-il configurable

4. **What "switch to another tab" covers.** (a) Only when the browser tab is
   hidden (another tab, minimised window): `visibilitychange`. (b) Also when
   the user switches to **another application** with the tab still visible:
   `window.blur`. Recommendation: **(a) plus the idle rule**. `blur` also fires
   in harmless cases, such as the browser's download prompt, and the 15 s idle
   rule already catches someone working in another app.
   answer:Pause tracking when the browser tab is actually hidden using visibilitychange, and use a 15-second idle rule to handle normal inactivity.

5. **Resume behaviour.** After a pause, (a) resume **automatically** on the
   next mouse or keyboard activity, or (b) require an explicit **Resume**
   click? Recommendation: **(a)**.
   answer:When the timer is paused due to inactivity or a tab switch, automatically resume it when the user next interacts with the page using mouse or keyboard activity, without requiring an explicit Resume button.

6. **Granularity.** One time per **batch** (`process_id`), or one per **file**
   (time spent while each file tab is active)? Recommendation: **per batch**.
   Per-file tracking is possible but adds a `file_id` column and tab-switch
   bookkeeping.
   answer:option a

7. **Timer placement.** (a) Right corner of the **Output** section header
   (right column). (b) Right corner of the **page header**, next to
   DMS / Rehydrate / Dashboard. Recommendation: **(a)**. It is visible while
   reviewing and doesn't crowd the nav.
   answer:option a

8. **Storage location.** (a) **New table** `pii_process_review_time`, one row
   per batch per user, joined to `PII_redation_upload_log` on `process_id`.
   (b) New columns on `PII_redation_upload_log`. That table has one row per
   **file**, so a batch-level time would be duplicated on every row.
   Recommendation: **(a)**.
   answer:option a

9. **Leaving without downloading.** If the user refreshes or closes the page
   after starting the timer but before any download, (a) **discard** the time,
   or (b) save it with a status such as `abandoned` (via `pagehide` +
   `sendBeacon`)? Recommendation: **(a)** for now.
   answer:option a

10. **Show system time too?** Should the pill also show the pipeline time for
    the batch, e.g. `System 00:04:12 · Review 00:02:31`, computed from
    `MIN(uploaded_at)` to `MAX(completed_on)` for the `process_id`?
    answer:System represents processing time, while Review represents user review time. Displaying them separately makes it easy to compare batch processing time with the actual human review time.

11. **Dashboard.** Should `/dashboard` report average review time and system
    time per user, client or job in this plan, or in a later one?
    Recommendation: **later**. Keep this plan to capture and storage.
    answer:First, correctly capture and store both Review Time and System Time in the database. User/client/job-wise average reports in /dashboard can be added in a later phase.

12. **Should the user see the timer at all?** Visible (as requested), or record
    silently? Visible was assumed.
    answer:Visible

---

## 6. Task List (step by step, after Section 5 is answered)

- [x] **Step 0.** Get answers to Section 5. Deviations from Sections 3–4 are
      recorded in Section 8.
- [x] **Step 1. DB.** Migration `0009_pii_process_review_time.sql`,
      `db.get_system_seconds()`, `db.get_review_time()`,
      `db.save_review_time()`, `db.py` docstring.
      - [ ] **Run the migration** on the dev and prod DBs (not run yet).
- [x] **Step 2. API.** `GET` + `POST /api/process/<process_id>/review-time`
      with auth, body validation and a client-permission check. Swagger:
      `review_time_get.yml`, `review_time_save.yml`. Tested with Flask's test
      client and a mocked `db` for: no session 403, unknown process 404,
      bad values 400 (negative, string, boolean), a client the user isn't
      allowed 403, a valid save 200, and the 24 h cap.
- [x] **Step 3. Timer core (JS).** State machine, `performance.now()`
      accounting, activity and visibility listeners, and the idle check.
      Verified in Node with a fake clock: idle rewind, tab-hidden pause,
      no resume on a stray mousemove after a download, and a second download
      adding only the new active time.
- [x] **Step 4. Timer UI.** Pill in the Output header. It shows once the
      results load and resets on a new Run Redaction.
- [x] **Step 5. Wire start and stop.** Start in the `selectToolBtn` listener.
      Save with a `keepalive` POST in the `resultList` download handler,
      which covers both per-file and ZIP buttons.
- [ ] **Step 6. Manual browser test** (after the migration is run).
  - Idle 20 s: the time doesn't grow and the 15 s are excluded.
  - Switch tab for 30 s: the time doesn't grow.
  - Download, edit, download again: the DB row updates and
    `download_count = 2`.
  - Multi-file batch: the ZIP button also saves.
  - New run: the timer resets.
- [x] **Step 7. Docs.** `wiki/architecture.md` Recent Changes, `.env.example`.

---

## 7. Out of Scope (for this plan)

- The headless `POST /api/redact` flow. It has no UI, so there is no review
  step to time.
- The `/rehydrate` page.
- Dashboard charts and reports (unless Q11 says otherwise).
- Changing how system time is recorded. `PII_redation_upload_log` is used as-is.
- Anti-tampering beyond server-side sanity caps. The time is measured in the
  browser, so a determined user could fake it.

---

## 8. What shipped (and where it differs from Section 3)

Answers applied: Q1 (a), Q2 (b), Q3 remove the idle time, log it, threshold
in `.env`, Q4 `visibilitychange` only, Q5 automatic resume, Q6 per batch,
Q7 Output header, Q8 new table, Q9 discard, Q10 show System and Review
separately, Q11 store both times now and add the dashboard later, Q12 visible.

**Database.** [migrations/0009_pii_process_review_time.sql](../../migrations/0009_pii_process_review_time.sql)

The table has more columns than Section 3.3:
- `idle_seconds` and `tab_hidden_seconds` (Q3: log the excluded time).
- `idle_threshold_seconds`: the threshold used when the row was saved.
- `system_seconds` (Q11: store both times). It is copied from
  `PII_redation_upload_log` on each save and is `NULL` when the batch has no log
  rows (upload with no `uid`) or a file is still pending.
- `started_at` is back-dated on the first save from the browser's elapsed wall
  time (`CURRENT_TIMESTAMP - wall_seconds`), so the client clock is never used.

Every column is updated with `GREATEST()`, so a late request can't lower a
value.

**Fix 2026-09-17: rows weren't being inserted.** The DB is **MySQL 5.5.41**,
which allows only one `TIMESTAMP ... DEFAULT CURRENT_TIMESTAMP` column per
table. The table was created with `DATETIME NOT NULL` `created_at` and
`updated_at` columns that have no default, so every insert failed with
`1364 Field 'created_at' doesn't have a default value`. The route returned 500,
and the browser only logged that to the console. `db.save_review_time()` now
sets `created_at` and `updated_at` explicitly, and the migration file uses
`DATETIME` to match. Verified against the real DB inside a rolled-back
transaction: insert, update, and a stale request that doesn't lower the
totals.

**Config.** `REVIEW_IDLE_SECONDS` (default 15) is read in `app.py` and rendered
into the page as `REVIEW_IDLE_MS`.

**API.**
- `POST /api/process/<process_id>/review-time` saves the time, as planned.
- A new `GET` on the same path returns `system_seconds` and the saved `review`
  row. The pill needs system time as soon as results show. When a batch is
  reopened (for example through the DMS signed link), the timer starts from the
  saved totals, so `GREATEST()` keeps adding up instead of throwing away the new
  session's time.
- Both routes need `_require_uid()`, a known `uid` (`_resolve_uid()`), an
  existing `process_id`, and the batch's client in `db.get_allowed_pids(uid)`.

**Frontend** (all in `app.py`: search for `Review timer`).
- States: `not_started`, `running`, `idle`, `hidden`, `downloaded`.
- The timer is initialised in `loadResultDetail()`, so it also works for a batch
  opened from a signed link. It resets in `runPipeline()`.
- **Changed:** "Clear all" no longer resets the timer. It only clears the
  left-hand file picker, and the results being reviewed stay on screen.
- **Superseded (see "Change: Download stops the timer" below):** after a
  download, a mouse move alone no longer restarted the timer, but a click,
  key press, wheel, scroll or touch did.
- If the tab is hidden while already idle, idle counting stops and the rest
  counts as tab-hidden time, so the same seconds are never counted twice.
- The display shows the running segment live. When the idle pause starts, it
  jumps back by the idle stretch, which is expected.

**Known limits.**
- Time since the latest download is discarded if the page is closed without
  another download (Q9).
- Users without a `uid` in their session (not opened via the signed link) don't
  see the pill. The GET returns 400 or 403 and the pill stays hidden.

**Change 2026-09-17: durations stored as HH:MM:SS.** Migration
[0010_pii_process_review_time_hhmmss.sql](../../migrations/0010_pii_process_review_time_hhmmss.sql)
replaces the `active_seconds`, `idle_seconds`, `tab_hidden_seconds` and
`system_seconds` INT columns with `active_time`, `idle_time`, `tab_hidden_time`
and `system_time` TIME columns (e.g. `00:01:11`). Existing rows were converted
with `SEC_TO_TIME()`. `TIME` was chosen over `VARCHAR` so values still compare
and sum. `idle_threshold_seconds` stays INT because it's a setting.

`db.py` writes with `SEC_TO_TIME()` and reads with `TIME_TO_SEC()`, so the API
JSON keys (`active_seconds`, …) and the browser timer are unchanged. The
migration has been applied to the DB in `.env`.

**Change 2026-09-17: no gaps in `id`.** `INSERT ... ON DUPLICATE KEY UPDATE`
used up an AUTO_INCREMENT id on every repeat download for the same batch
(`innodb_autoinc_lock_mode=1`). Rolled-back test inserts had also used up ids,
so the first two rows got ids 4 and 8. `db.save_review_time()` now runs an
`UPDATE` first and only `INSERT`s when no row exists. If a concurrent download
inserts first, it retries the `UPDATE`. The existing rows were renumbered to
1 and 2, with `AUTO_INCREMENT` reset to 3.

An id can still be skipped in rare cases, such as an insert that fails, because
MySQL never reuses an allocated id. Nothing references `id`, so a gap is
harmless. Use `created_at` for ordering.

---

## 9. How idle time is tracked

Idle tracking runs **entirely in the browser**. The page listens for input
events, remembers when the last one happened, and checks every second whether
the user has been quiet for the threshold. The server is only called on a
Download click (Section 3.3). All code is in `app.py`: search for
`Review timer`.

### 9.1 Listen for input

`onReviewActivity()` is attached to `document` for these events:

```js
['mousemove', 'mousedown', 'keydown', 'wheel', 'touchstart', 'scroll']
```

- **Options:** `capture: true`, so input anywhere on the page is seen,
  including inside the preview pane and the reference table. Also
  `passive: true`, so scrolling is never slowed down.
- **Effect:** each event stores the current `performance.now()` in
  `lastActivityAt`. If the timer is paused for idle or tab hidden, it also
  restarts it (Q5: automatic resume). Input does **not** restart a timer that
  a Download click stopped; only Select does (Section 9.6).

### 9.2 Check every second

A single `setInterval` runs once a second:

```js
if (review.state === 'running' && performance.now() - review.lastActivityAt >= REVIEW_IDLE_MS)
    pauseReviewTimer('idle', review.lastActivityAt);
```

`REVIEW_IDLE_MS` comes from `REVIEW_IDLE_SECONDS` in `.env` (default 15). It is
read once in `app.py` at startup and rendered into the page
(`{{ review_idle_seconds|int }} * 1000`), so changing it needs a server
restart.

### 9.3 Remove the idle stretch from review time (Q3)

The pause is recorded at **`lastActivityAt`, the last input**, not at the
moment the check fires. The quiet seconds before the pause are never counted as
review time.

```
last input ────── 15 s with no input ────── check fires ··· paused ··· next input
     │◀────────────────────────── idle_time ────────────────────────────────▶│
review time stops here                  (review clock jumps back ~15 s)
```

This is why the pill's **Review** value drops back by about 15 s when the state
changes to "Paused – idle". That is expected.

### 9.4 Resume and log the idle time

On the next input, `closeReviewPause()` adds the whole gap
(`now - pauseStart`, from the last input to the new one) to `idleMs` and
restarts review time. Each idle pause adds 1 to `idle_pause_count`. On Download
the totals are sent in seconds and stored as:

| Column | Meaning |
|---|---|
| `active_time` | Review time, excluding idle and tab-hidden time |
| `idle_time` | Total time paused for inactivity, including the threshold seconds removed from review time |
| `idle_pause_count` | How many times the timer paused for inactivity |
| `tab_hidden_time` | Total time paused while the tab was hidden (Section 9.5) |
| `tab_pause_count` | How many times the tab was hidden while the timer ran |
| `idle_threshold_seconds` | The `REVIEW_IDLE_SECONDS` value in effect at save time |

### 9.5 Tab hidden vs idle

- **Tab hidden.** A `visibilitychange` listener pauses the timer as `hidden`
  when the tab is hidden (Q4). A hidden tab gets no input events, so this is
  tracked separately.
- **Hidden while already idle.** Idle counting stops at that moment and the
  rest counts as tab-hidden time, so the same seconds are never counted in both
  `idle_time` and `tab_hidden_time`.
- **Tab visible again.** The timer stays paused until the next input.

### 9.6 Design choices

- **A monotonic clock, not a tick counter.** Every duration is a
  `performance.now()` difference. That clock isn't affected by changes to the
  system time. The 1-second interval only decides when to pause and refreshes
  the display. Browsers slow intervals down in background tabs, so adding up
  ticks would drift.
- **A Download click stops the timer; only Select restarts it.** Input events
  are ignored while stopped, so moving the mouse, typing or scrolling after a
  download adds nothing. Clicking Select again restarts it from the running
  total.
- **No throttling on input events.** The handler only stores a number and
  returns, so running it on every `mousemove` costs almost nothing.

### 9.7 Limits

- **Input on this page only.** Reading the preview without touching the mouse
  or keyboard for longer than the threshold counts as idle. If reviewers read
  for long stretches, raise `REVIEW_IDLE_SECONDS`.
- **Measured in the browser.** A determined user could fake the numbers. The
  server only rejects impossible values: non-integers, negatives, and anything
  over 24 h (Section 7).
- **Time since the latest Download is discarded** if the page is closed
  without another download (Q9).

**Change 2026-09-17: Download stops the timer (replaces Q2 (b) resume
behaviour).** Before, a Download click only paused the timer and the next
input restarted it. Now:

- A Download click **stops** the timer and saves. The state is `stopped` and
  the pill shows "Stopped – click Select to continue".
- Mouse, keyboard, scroll and touch input **never** restart a stopped timer.
- Clicking **Select** again restarts it from the running total. Review work
  after a download (boxing a missed value, Add & Re-redact) is counted, and the
  next Download saves the new total.
- A repeat Download while stopped re-sends the same totals. The saved time
  doesn't change; only `download_count` goes up.

```
Select ─▶ Running ⇄ Paused (idle / tab hidden)
             │
         Download ─▶ STOPPED (saved) ── mouse/keys/scroll: stays stopped
                         │
                   click Select ─▶ Running (from saved total) ─▶ Download ─▶ STOPPED
```

Verified in Node with a fake clock: 10 s review, then Download, then input and
40 s wait (still 10 s), a repeat Download (10 s), then Select, 5 s, Download
(15 s).

**Change 2026-09-21: pill shows Idle and Elapsed.** The Output-header pill now
reads:

```
System 00:01:06 · Review 00:00:15 · Idle 00:00:35 · Elapsed 00:01:20  ● Running
```

- **Idle:** total idle time (`idle_time`), counting up live while paused for
  inactivity.
- **Elapsed:** Review + Idle + tab-hidden time, i.e. everything since the first
  Select click. Time while stopped after a Download is not counted. When an idle
  pause starts, Review drops by the threshold and Idle gains the same amount,
  so Elapsed never jumps.

This is display only: the DB columns are unchanged, and Elapsed can be derived
as `active_time + idle_time + tab_hidden_time`. The pill now wraps onto two
lines when the column is narrow.

**Change 2026-09-21: review time on `/dashboard` (Q11's later phase).**

- **Time cards.** A row of 4 cards under the existing stat cards shows
  **Avg System**, **Avg Review**, **Avg Idle** and **Avg Elapsed** time per
  reviewed batch, with totals in the sub-line. They follow the dashboard's date
  filter, applied to `last_download_at`.
- **"Review Time" tab** (next to Summary / Vertical / Cluster / TL):
  - **By user:** batches, files, total review, and average system, review,
    idle and elapsed.
  - **By client:** the same columns.
  - **Recent batches:** the latest 25, with per-batch System, Review, Idle,
    Elapsed and download count.
- **Download Report (.xlsx).** The Summary sheet gains the averages. There are
  three new sheets: *Review Time by User*, *Review Time by Client* and
  *Review Time Batches* (all batches). Durations are Excel time values
  formatted `[h]:mm:ss`, so they can still be summed and sorted.
- **Data.** `db.get_review_time_breakdown()` reads `pii_process_review_time`,
  joined to `pii_files` (client, process name, file count), `project` (client
  name) and `user` (name, employee id). Elapsed = review + idle + tab-hidden.
  Average system time skips batches with no system time.
- **Access.** Same as the rest of `/dashboard`: admin allowlist only.
