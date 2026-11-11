# Plan: First-Visit Guided Product Tour

Status: **IMPLEMENTED 2026-08-18** — Part 1 (first page load) built and verified in a
real browser (Playwright against a live instance of `app.py`); see Section 9 for
what shipped and Section 7 for the checked-off task list. `/rehydrate` (Open
Question #5) is explicitly out of scope for this pass, per your answer.
Owner: TBC
Created: 2026-08-18
Updated: 2026-08-18 — implemented and verified.

---

## 1. Goal

When a user opens the app for the first time, we want an on-screen guided tour
that walks them through the page and explains **what each tool/section is for**
before they touch anything — Process Name / Client / Job / Output Format, the
upload drop zone, Run Redaction, the live console, the results/preview area,
the reference table + "Add missing value" flow, the acknowledgement checkbox,
and the header links (DMS, Rehydrate a file). The tour should not force itself
on returning users — once seen, it stays out of the way, but the user can
bring it back on demand.

This plan covers **only** the tour mechanism itself (trigger, step content,
UI chrome, persistence, replay) — it does not change any existing
functionality; it is purely additive.

---

## 2. Current State (what exists today)

### 2.1 No onboarding of any kind exists

- Searched `app.py` for `localStorage`, `sessionStorage`, `onboarding`,
  `tour`, "first time" — **no matches**. There is nothing in the codebase
  today that distinguishes a first-time visitor from a returning one, and no
  client-side persistence is used anywhere.
- The very first thing a new user sees is the empty form (Section 2.3) with a
  static placeholder in the output column ("No output yet" —
  [app.py:843-844](../../../app.py#L843-L844)) and no explanatory copy beyond
  field labels and placeholder text.

### 2.2 Frontend architecture (constrains the implementation approach)

- **No build step, no npm/webpack/React** — the entire UI is one Python
  triple-quoted string (`HTML`, [app.py:239](../../../app.py#L239), ~1500
  lines) rendered via `render_template_string(HTML, dms_href=dms_href)` from
  `index()` ([app.py:2345-2355](../../../app.py#L2345-L2355)). CSS and
  vanilla JS are both inlined in the same string (`<style>` block, then
  `<script>` starting at [app.py:899](../../../app.py#L899)).
- The only external resource loaded is a Google Fonts stylesheet
  ([app.py:245](../../../app.py#L245)) — no other CDN dependency exists
  today, and no JS libraries (no jQuery, no UI framework) are used anywhere.
  This is a strong precedent for hand-rolling the tour in plain JS/CSS rather
  than pulling in a tour library (Section 3.1, Open Question #2).
- Top-level script runs immediately on parse, e.g. `loadClients();` at
  [app.py:959](../../../app.py#L959) — there's no `DOMContentLoaded` wrapper
  needed since the `<script>` tag sits at the end of `<body>`. A tour
  init call (`initTour();`) would follow the same pattern.
- CSS already defines a full design-token set on `:root`
  ([app.py:247-264](../../../app.py#L247-L264)) — `--accent`, `--surface`,
  `--border`, `--radius`, `--shadow`, etc. — the tour's overlay/tooltip/spotlight
  styling should reuse these tokens rather than introduce new colors, so it
  looks native to the app instead of bolted on.

### 2.3 Inventory of tools/sections a tour would need to cover

From the main page (`index()` → `HTML`, [app.py:729-896](../../../app.py#L729-L896)):

| Area | Element(s) | Always visible? |
|---|---|---|
| Header | Logo/title, `DMS` link, `Rehydrate a file →` link ([app.py:729-741](../../../app.py#L729-L741)) | Yes |
| Left panel — Client & Job | Process Name input, Client select, Job select (disabled until a client is picked) ([app.py:757-772](../../../app.py#L757-L772)) | Yes |
| Left panel — Output Format | `excel` vs `as_is` selector ([app.py:773-779](../../../app.py#L773-L779)) | Yes |
| Left panel — Upload | Drop zone / file browse, accepted types note, file list ([app.py:783-799](../../../app.py#L783-L799)) | Yes |
| Left panel — Actions | `Run Redaction` button (disabled until files are added), `Clear all` ([app.py:801-808](../../../app.py#L801-L808)) | Yes |
| Left panel — Progress | Spinner, status text, progress bar ([app.py:811-820](../../../app.py#L811-L820)) | Only during a run |
| Left panel — Console | Live scrolling log output ([app.py:822-829](../../../app.py#L822-L829)) | Only during/after a run |
| Right panel — empty state | "No output yet" placeholder ([app.py:839-845](../../../app.py#L839-L845)) | Only before first run |
| Right panel — Results | File tabs (multi-file batches), preview pane, `Select` box-draw tool + hint text, reference table (Original Value → Masked As), "Add missing value" form (value + free-text category + `+` new-category button + `Add & Re-redact`), edit/cancel controls ([app.py:848-884](../../../app.py#L848-L884)) | **Only after a run produces output** — the whole `#detail-section` is `display:none` until then |
| Right panel — Acknowledgement | "I reviewed the redacted file…" checkbox ([app.py:886-889](../../../app.py#L886-L889)) | Yes (but only meaningful post-run) |
| Right panel — Download list | `#result-list` ([app.py:891](../../../app.py#L891)) | Only after a run |

This split — some tools are on-screen from the moment the page loads, others
only exist in the DOM once a redaction run has produced output — is the
central design fork for this plan (Section 3.3 / Open Question #4).

### 2.4 Other pages that could plausibly also want a tour

- `/rehydrate` — a separate, self-contained HTML template
  (`REHYDRATE_HTML`, [app.py:1773](../../../app.py#L1773)), auth-gated behind
  `session["uid"]` — a single-purpose upload-and-restore page, much simpler
  than the main page.
- `/dms` — currently just a placeholder card ("Document Management Suite
  entry point is now available…", [app.py:2358-2424](../../../app.py#L2358-L2424))
  with no real tools yet — a tour here would have nothing to point at.

---

## 3. Proposed Design

### 3.1 Implementation approach — hand-rolled, no new dependency (recommended)

Given Section 2.2 (no build step, no JS libraries anywhere in the app today,
only one existing external resource which is a font, not a script), the
recommendation is to **hand-roll the tour** as a small, self-contained JS
module inside the existing `<script>` block, plus a `<style>` block reusing
the existing design tokens — not pull in a third-party tour library (e.g.
Shepherd.js, Intro.js, driver.js) via CDN.

Reasoning:
- Keeps the "single Python string, zero external JS dependencies" pattern
  intact — no new CDN call that could fail/be blocked on a locked-down
  internal network (this app is deployed behind IIS on an internal network,
  per [wiki/deployment.md](../../deployment.md)), and no new supply-chain
  surface.
- A guided tour for ~10 fixed steps is a small, well-understood amount of
  code (position a tooltip near a target element, dim the rest of the page,
  Next/Back/Skip buttons, a dot/step-counter) — it doesn't need a general
  library's flexibility.
- Matches how this codebase already solved a similar-shaped problem: the
  redaction-map table, preview pane, and box-select tool (Section 2.3) were
  all built from scratch in vanilla JS rather than pulling in a grid/canvas
  library — this plan follows the same convention.

This is still flagged as **Open Question #2** in case there's a reason to
prefer a battle-tested library instead (e.g. plans to reuse the same tour
mechanism across several unrelated pages later, where a library's
step-definition format would pay for itself).

### 3.2 Tour style — spotlight/anchored vs. static walkthrough

**Option A — Spotlight-anchored tour (recommended).** Each step highlights
the *actual* live element on the page (a dimmed overlay everywhere else, a
"cutout" around the target, a tooltip card next to it with a short
explanation + Next/Back/Skip). This is what most users recognize as a
"product tour" and directly satisfies "what is the purpose of this tool" by
pointing at the real tool. Needs: computing each target's bounding box,
repositioning on window resize/scroll, and handling elements that don't
exist yet (Section 3.3).

**Option B — Static modal/carousel.** A single centered modal that pages
through slides describing each tool with a short blurb (optionally a small
static screenshot or icon), not tied to the element's real on-page position.
Simpler to build (no positioning/resize logic), but weaker at building the
user's spatial map of the page ("the button described in slide 4 is *where*
on my screen?").

Recommendation: **Option A**, since the ask is explicitly about explaining
what each visible tool does, and Option A is what best serves that. Flagged
as **Open Question #1**.

### 3.3 Handling elements that don't exist until after a run

Per Section 2.3, the preview pane / reference table / "Add missing value"
form / download list only exist in the DOM once a run has finished — a
brand-new user, by definition, hasn't run anything yet. Three ways to handle
this, from simplest to most complete:

- **(a) Tour only covers the always-visible left-panel tools** (Process
  Name, Client/Job, Output Format, Upload, Run Redaction) plus the header
  links, and ends by telling the user "once you run a redaction, results,
  preview, and the reference table will appear here" pointing at the empty
  right panel — no explanation of the post-run tools until the user actually
  reaches that state.
- **(b) Two-part tour.** Part 1 runs on first page load (left panel +
  header, as in (a)). Part 2 auto-triggers the *first time* `#detail-section`
  becomes visible (i.e. right after the user's first-ever completed run),
  walking through preview/table/add-value/download — so every tool
  eventually gets explained, just split across two natural moments.
- **(c) Force-preview the post-run UI during the first tour.** Temporarily
  show `#detail-section` with placeholder/mock content (a fake row in the
  reference table, a placeholder preview image) purely for the tour, then
  hide it again. Most complete in one sitting, but adds meaningfully more
  code (fake data that must never leak into really-empty state) for a
  once-per-user benefit.

Recommendation: **(b)** — every real tool gets explained using its real,
actual state (no fake data to maintain), and it naturally teaches the
right-panel tools at the exact moment they become relevant instead of
describing something the user can't see yet. Flagged as **Open Question
#4**.

### 3.4 Trigger & persistence — how do we know this is a first visit?

There is no login/user-account system in this app — the closest thing to an
identity is the encrypted `uid` optionally present in the `?uid=...` link
(`session["uid"]`, [app.py:2348-2353](../../../app.py#L2348-L2353)), and even
that is only ever set when the app is opened via a signed link from the host
system, not a real authentication mechanism (see
[plan-api-key-implementation.md, Section 2.1](plan-api-key-implementation.md)
for the caveat that this uid isn't a hard credential). Three options for
"has this visitor seen the tour":

- **Option A — `localStorage` flag (recommended default).** Set
  `localStorage.setItem('pii_tour_seen_v1', '1')` once the tour is
  dismissed/completed. Zero backend change, works immediately, no DB
  migration. Downside: it's per-browser-profile, not per-person — a new
  browser, incognito window, or cleared site data will show the tour again;
  two different people sharing one browser profile only see it once between
  them.
- **Option B — `sessionStorage` flag.** Same as A but resets every new
  browser tab/window — the tour would replay far more often (every fresh
  session), which doesn't match "first time" as commonly understood, but
  guarantees the tour is always fresh context if this is mostly used as an
  ephemeral kiosk-style tab.
- **Option C — server-side, keyed by `uid`.** New tiny table (e.g.
  `pii_tour_seen(uid INT PRIMARY KEY, seen_at TIMESTAMP)`) or a column
  elsewhere, checked/set via a small `GET/POST /api/tour-seen` pair. More
  durable (survives browser/device changes for the *same* linked uid), but:
  requires a migration, only works for visits that arrive with a uid (a
  bare `http://host:5009/` visit with no `?uid=...` has nothing to key on),
  and is meaningfully more work for a feature whose only job is "don't
  nag the user twice."

Recommendation: **Option A**, since it needs no backend/DB change, works
for every visitor including ones with no uid, and matches the "first open"
framing most literally. Flagged as **Open Question #3**.

### 3.5 Manual replay

Regardless of the auto-trigger, add a small persistent control (e.g. a `?`
icon button in the header, next to the `DMS` / `Rehydrate a file` links) that
lets any user re-launch the tour on demand — useful for a new team member
being walked through it by someone else, or a returning user who forgot what
a control does. Clicking it does **not** touch the "seen" flag from Section
3.4 (replaying manually shouldn't reset or otherwise interfere with the
first-visit auto-trigger logic).

### 3.6 Step content (draft — needs your edits)

Draft step list assuming Option A (spotlight) + Option (b) (two-part) design.
Wording below is a first draft only — please edit freely in Open Question #6.

**Part 1 — on first page load:**

1. Welcome card (centered, not anchored) — 1-2 sentences on what this app
   does overall.
2. `Process Name` field — "Give this batch a name so you can find it again
   later."
3. `Client` / `Job` selects — "Pick which client and job this upload belongs
   to."
4. `Output Format` select — "Choose Excel (everything flattened into one
   sheet) or As it is (keeps the original file format)."
5. Upload drop zone — "Drop or browse for the files you want redacted —
   PDF, Excel, Word, or images."
6. `Run Redaction` button — "Once you've added files, click here to start
   the pipeline."
7. Header links — `DMS` and `Rehydrate a file` — one line each on what they
   lead to.
8. Closing card — "That's it for now — once your first run finishes, we'll
   show you the results tools too." Sets the "seen" flag (Section 3.4).

**Part 2 — auto-triggered the first time results appear:**

9. Preview pane — "This shows your redacted file so you can check it before
   downloading."
10. `Select` tool — "Spot a value the pipeline missed? Drag a box around it
    here."
11. Reference table — "Every value that got masked, and what it was replaced
    with."
12. "Add missing value" form — "Missed something? Type it here, pick or add
    a category, and re-redact."
13. Acknowledgement checkbox — "Tick this once you've reviewed the output."
14. Download list — "Grab your redacted file(s) here."

### 3.7 Skip / dismiss behavior

- `Skip` is available on every step and immediately ends the tour, sets the
  "seen" flag (so it won't auto-replay), same as finishing normally.
- `Esc` key and a close (`×`) button on the tooltip card do the same as
  `Skip`.
- Clicking the dimmed overlay itself does **not** dismiss (avoids accidental
  dismissal while the user is trying to read the tooltip) — only the
  explicit controls do.

---

## 4. Data flow / sequence

```
Page load (GET /)
    │
    ▼
initTour() runs after existing init (loadClients(), etc.)
    │
    ├─ localStorage.pii_tour_seen_v1 present?
    │      yes → do nothing (tour available via manual "?" replay button only)
    │      no  → run Part 1 (Section 3.6, steps 1-8)
    │              on Skip/Finish → localStorage.setItem('pii_tour_seen_v1','1')
    │
    ▼
User uploads files, clicks Run Redaction (existing flow, unchanged)
    │
    ▼
SSE 'done' handler (existing, app.py ~569-609-equivalent) shows #detail-section
    │
    ├─ Is this the first time #detail-section has ever been shown
    │  this browser? (a second localStorage flag, e.g. pii_tour_part2_seen_v1)
    │      yes → auto-run Part 2 (Section 3.6, steps 9-14)
    │      no  → do nothing
    │
    ▼
"?" header button → re-runs Part 1 + Part 2 back to back on demand,
                     regardless of either flag's state
```

---

## 5. Open Questions (need your answers)

1. **Tour style (Section 3.2)** — Option A: spotlight tour anchored to the
   real on-page elements (recommended), or Option B: a static modal/carousel
   not tied to element positions?
   answer:Option A

2. **Implementation approach (Section 3.1)** — hand-roll in vanilla JS
   (recommended, matches how the rest of this app is built), or pull in an
   existing tour library via CDN (e.g. Shepherd.js/Intro.js/driver.js)?
   answer:Option A — Hand-roll in vanilla JS

3. **Persistence of "has this visitor seen the tour" (Section 3.4)** —
   Option A: `localStorage` (recommended, no backend change), Option B:
   `sessionStorage` (replays every new tab), or Option C: server-side keyed
   by `uid` (durable across devices, needs a migration, only works when a
   uid is present)?
   answer:Option A — localStorage

4. **How to handle post-run tools that don't exist on first load (Section
   3.3)** — (a) only cover the always-visible left panel, (b) two-part tour
   split across first-load and first-results (recommended), or (c)
   force-show placeholder/mock results content so the whole tour runs in one
   sitting on first load?
   answer:Two-part tour

5. **Which pages get a tour** — just the main page (`/`), or also
   `/rehydrate` (Section 2.4)? (`/dms` has no real tools yet, so out of scope
   regardless unless you say otherwise.)
   answer:Just the main page (`/`) for this pass. `/rehydrate` can get its 
own tour as a follow-up once we've validated the tour pattern works well 
on the main page — bundling both into one implementation risks scope 
creep, and `/rehydrate`'s onboarding needs may differ enough to warrant 
its own dedicated pass rather than reusing the same tour steps.

6. **Step content/copy (Section 3.6)** — is the draft step list/wording
   acceptable as a starting point, or do you want to rewrite/reorder/add/drop
   steps before implementation? Please mark up directly.
   answer:Acceptable as a starting point — no major rewrite needed. Just 
verify step 4's option labels ("Excel" / "As it is") match the exact UI 
text in app.py before finalizing copy, since wording drift there would be 
the one visible inconsistency. Everything else reads clearly and matches 
its target element's actual function.

7. **Manual replay control (Section 3.5)** — add a persistent "?" button in
   the header to replay the tour anytime, or is auto-trigger-only sufficient?
   answer:Yes, add the "?" button. The plan's own Section 3.5 already 
identifies real use cases this serves (onboarding a new team member, a 
returning user who forgot a control) that auto-trigger alone can't cover 
once the "seen" flag is set. The cost is low since it just reuses the 
already-built Part 1 + Part 2 logic, and without it there's no recovery 
path for a user who skipped or missed the tour the first time.

8. **Tone/branding** — any specific tone (formal/casual), or company style
   guide to follow for the tour copy and visuals?
   answer:No formal style guide to follow — match the tone already used in 
the Section 3.6 draft (casual-professional, direct, second-person: "Give 
this batch a name...", "Spot a value the pipeline missed?"). That fits an 
internal tool without needing a marketing-level brand voice.

---

## 6. Database changes needed

**None**, if Open Question #3 is answered Option A or B (`localStorage` /
`sessionStorage`).

**One new migration**, only if Option C (server-side, per-`uid`) is chosen:

```sql
CREATE TABLE IF NOT EXISTS pii_tour_seen (
    uid      INT PRIMARY KEY,
    seen_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

plus two small `db.py` helpers (`has_seen_tour(uid)`, `mark_tour_seen(uid)`)
and a small `GET/POST /api/tour-seen` route pair gated the same way other
`uid`-scoped routes are (`_require_uid()`-style check).

---

## 7. Task List

- [x] Get answers to Open Questions in Section 5
- [x] Write/finalize tour step copy (Section 3.6) — kept as drafted per
      Open Question #6 answer; verified the Output Format step's wording
      ("Excel" / "As it is") matches the exact `<option>` text at
      [app.py:775-778](../../../app.py#L775-L778)
- [x] Add tour CSS (dimmed overlay, highlighted-target spotlight via a
      `box-shadow: 0 0 0 9999px` trick on the target itself rather than a
      separate cutout div, tooltip card, Next/Back/Skip buttons, step
      counter) to `app.py`'s `<style>` block, reusing existing design tokens
      (`--accent`, `--surface`, `--border`, `--radius`, `--shadow`) — see
      Section 9.1 for why the spotlight has no separate DOM element
- [x] Add tour JS module inside the existing `<script>` block: step
      definitions (CSS selector + copy), positioning logic, Next/Back/
      Skip/Esc handling, window-resize repositioning
- [x] Wire first-load trigger (`maybeAutoStartTourPart1()`, bound to
      `window`'s `load` event with a 200ms settle delay, guarded against the
      signed-link embedded view — see Section 9.2)
- [x] Wire Part-2 trigger (`maybeAutoStartTourPart2()`) into
      `loadResultDetail()` right after `await loadActiveFile()` succeeds
      ([app.py:1319](../../../app.py#L1319) area) — the single place both the
      normal run-completion flow and the signed-link flow both pass through
- [x] Implement `localStorage` persistence (`pii_tour_part1_seen_v1` /
      `pii_tour_part2_seen_v1`), per Open Question #3 answer — no DB change
- [x] Add manual "?" replay control in the header
      (`#tour-help-btn`, next to `DMS` / `Rehydrate a file`), per Open
      Question #7 answer — chains Part 1 into Part 2 automatically if
      results are currently visible, regardless of either "seen" flag
- [ ] `/rehydrate` tour — explicitly out of scope for this pass (Open
      Question #5 answer); revisit as its own follow-up plan
- [ ] N/A — Option A (`localStorage`) was chosen, no migration needed
      (Section 6)
- [x] Automated browser test (Playwright against a live `app.py` instance,
      Section 9.3): first visit auto-shows the tour at Step 1 of 8; Next
      advances through steps correctly (verified up to Step 5, the Upload
      Documents step); Back returns to the previous step; Skip closes the
      overlay/highlight cleanly and sets the `localStorage` flag; a reload
      afterward does **not** re-trigger the tour; the "?" button reopens it
      on demand regardless of the flag
- [x] Visual check (screenshots): welcome card renders centered with the
      page dimmed; the Upload Documents step correctly spotlights the real
      drop-zone element (undimmed, ring highlight) with the tooltip
      positioned just above it and Back/Skip/Next all present; post-Skip
      state has zero leftover dimming/highlight artifacts
- [ ] Manual test: tooltip positioning holds up with the left panel
      collapsed before the tour starts (code auto-expands it via
      `sidebarToggle.click()` and restores it after — implemented, not yet
      manually re-verified beyond the automated pass above)
- [ ] Manual/live test: Part 2 auto-trigger after a **real** redaction run
      completes (needs the full ML pipeline + DB reachable — out of scope
      for this session's environment; the hook is wired and unit-verified
      by code review, not yet exercised end-to-end)
- [ ] Update [wiki/usage.md](../../usage.md) with a short note on the tour
      and how to replay it

---

## 8. Out of Scope (for this plan)

- Any change to existing functionality — this plan is purely additive UI.
- `/dms` — currently a placeholder page with no real tools to explain
  (Section 2.4); revisit once it has actual functionality.
- Per-step analytics/telemetry on tour completion/skip rates, unless you say
  otherwise in Open Question #8.
- Localization/translation of tour copy — assumed English-only, matching the
  rest of the app's UI text today.
- A general-purpose/reusable "tour engine" for future unrelated features —
  this plan builds exactly the steps in Section 3.6, not a configurable
  framework, unless Open Question #2's answer indicates otherwise.

---

## 9. Implementation status (2026-08-18)

All code changes are in `app.py` only — no other file touched, no new
dependency, no migration (matches Section 6). Roughly 200 lines added: CSS
in the existing `<style>` block, two small markup nodes (`#tour-overlay`,
`#tour-card`) before the closing `<script>`, one new button in the header,
and a JS module appended to the end of the existing `<script>` block plus
two one-line hooks at the actual trigger points.

### 9.1 Spotlight technique — no separate cutout element

Rather than maintaining a second absolutely-positioned "spotlight" rectangle
kept in sync with the target's bounding box (the original sketch in Section
3.2), the shipped version highlights the *actual* target element directly:
it gets a `.tour-highlight` class that bumps its `z-index` above the overlay
and gives it `box-shadow: 0 0 0 4px rgba(79,70,229,.5), 0 0 0 9999px
rgba(15,17,23,0.55)` — the second, huge-spread shadow dims the entire rest
of the page from the element's own paint, which is a well-known pure-CSS way
to get a "cutout" look without a synced second rectangle. `.tour-highlight`
also sets `pointer-events: none` so a highlighted `Run Redaction` or file
input can't actually be triggered mid-tour — only the card's own Next/Back/
Skip/close buttons (and Esc) advance or end the tour, per Section 3.7. This
is simpler and more robust than the two-element design sketched in the
original plan (nothing to resize/reposition in sync — the highlight *is* the
element), so Section 3.2/4 are superseded by this detail; no other planned
behavior changed.

### 9.2 Signed-link view guard

`resolveSignedLinkContext()` ([app.py:961](../../../app.py#L961)) is a
pre-existing flow for the embedded/signed-link view (`?token=...`) that
force-collapses the left sidebar and jumps straight to showing results. Part
1 of the tour targets left-panel fields that are deliberately hidden in that
view, so `maybeAutoStartTourPart1()` checks for a `token` query param and
skips the auto-trigger entirely when present (the manual "?" button still
works there if someone wants it — it will un-collapse the sidebar on
purpose, which is fine for an explicit click). Part 2 has no such
dependency and auto-triggers normally in that view too, since
`loadResultDetail()` is the single call site both flows share.

### 9.3 Verification performed

- `python -m py_compile app.py` — clean.
- Extracted the `HTML` string and rendered it through Jinja2 directly
  (`Template(html).render(dms_href='/dms')`) — renders without error; the
  only `{{ }}` tag in the whole template is the pre-existing `dms_href`, so
  none of the new JS's `${...}` template-literal syntax collided with
  Jinja's delimiters.
- Extracted the inline `<script>` block and ran `node --check` — clean.
- End-to-end browser test: started a throwaway copy of the app on a spare
  local port (`from app import app; app.run(port=5511)`, run from outside
  the repo) so as not to disturb the pre-existing `serve.py` instance
  already running on the app's normal port 5009 in this environment, then
  drove it with Playwright (Chromium, headless) — confirmed: tour
  auto-opens on first load at "Step 1 of 8"; `Next` × 4 correctly reaches
  "Step 5 of 8" (Upload Documents); `Back` correctly returns to "Step 4 of
  8"; `Skip` closes the overlay and sets `localStorage.pii_tour_part1_seen_v1
  = '1'`; a page reload after that does **not** re-open the tour; clicking
  the new `#tour-help-btn` ("?") reopens it from Step 1 regardless of the
  flag. Screenshots confirmed the welcome card renders centered with the
  page dimmed, and the Upload Documents step correctly spotlights the real
  drop-zone (undimmed, highlighted ring) with the tooltip card positioned
  just above it.
- One real bug was caught and fixed by this testing: the initial CSS used
  `#tour-overlay.active #tour-card { display: block; }` (a descendant
  selector), but `#tour-overlay` and `#tour-card` are siblings in the
  markup, not nested — so the rule never matched and the card stayed
  `display: none` even though its content was being set correctly (a static
  Jinja/JS-syntax check would not have caught this; only driving it in a
  real browser did). Fixed by toggling an `.active` class directly on
  `#tour-card` itself alongside the overlay's.
- Not yet exercised: Part 2 (post-run results tour) end-to-end against a
  real completed redaction run — this test environment's `/api/clients`
  returns 403/no DB, so a real run couldn't be driven through this
  session's browser test. The trigger hook is in place and was verified by
  reading the code path, not by watching it fire live.
- Test artifacts (server log, Playwright script, screenshots) were written
  to a scratch directory outside the repo and are not part of this commit.
