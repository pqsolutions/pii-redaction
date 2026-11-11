# Plan: PM2 Process Supervision for the Production Server

Status: **PARTIALLY IMPLEMENTED 2026-08-18** — all Open Questions in Section 5
answered; the repo-level changes (config file, deploy script, docs) are done
and verified. The actual system-level steps — installing PM2/pm2-windows-service
on the target machine, registering the Windows Service, and switching the
live `serve.py` process over to PM2 — have **not** been executed; they touch
a process currently serving real traffic and require Administrator rights, so
they're left for you to run (via `setup_iis.ps1` or by hand). See Section 9.
Owner: TBC
Created: 2026-08-18
Updated: 2026-08-18 — answers recorded, repo changes implemented.

---

## 1. Goal

Put the production web process (`serve.py`, waitress serving the Flask app on
`127.0.0.1:5009`, fronted by IIS) under [PM2](https://pm2.keymetrics.io/)
supervision, so it: restarts automatically if it crashes, restarts
automatically when the machine reboots, and gives a simple CLI
(`pm2 status` / `pm2 logs` / `pm2 restart`) for checking on and managing it —
instead of relying on someone remembering to manually run `python serve.py`
in a terminal window and leave it open.

This plan covers **only** process supervision — it does not touch IIS/ARR
reverse-proxy config, `serve.py`'s waitress tuning, or anything about the
redaction pipeline itself.

---

## 2. Current State (what exists today)

### 2.1 The documented supervision path exists, but isn't actually running on this machine

`wiki/deployment.md` and `setup_iis.ps1` describe a full production setup:
IIS site → ARR reverse proxy → `windows_service.py` (a `pywin32`-based
Windows Service wrapping waitress) on `127.0.0.1:5009`, auto-start on boot,
auto-restart on failure via the Windows Service Control Manager.

**That path is not what's actually deployed on this machine right now.**
Checked directly: `Get-Service PIIRedactionPipeline` returns nothing — no
such service is registered. The process actually answering on port 5009
during this session was a plain `python serve.py` started manually
(no service, no supervisor) — confirmed while testing the tour feature
earlier today: it had been running since well before this session started,
with nothing to restart it if it crashed or the machine rebooted.

This is the real motivating problem PM2 would solve here — not a gap in
`windows_service.py`'s design, but a gap between what's documented and
what's actually running. This matters for Open Question #1: PM2 could
either replace the never-actually-deployed `windows_service.py` path, or
sit alongside it — worth being explicit about which, so we don't end up
with a repo that documents two different "how production runs" stories.

### 2.2 What `serve.py` already does (unaffected by this plan)

[serve.py](../../../serve.py) — imports `app` from `app.py`, serves it via
`waitress.serve()`: `host="0.0.0.0"`, `port=5009`, `threads=8`,
`channel_timeout=7200` (2 hours, to survive long pipeline runs),
`connection_limit=100`. None of this changes — PM2 just becomes the thing
that starts this script and watches whether it's still alive.

### 2.3 IIS / reverse proxy (unaffected by this plan)

[web.config](../../../web.config) — IIS ARR rewrites everything to
`http://127.0.0.1:5009/{R:1}`. [setup_iis.ps1](../../../setup_iis.ps1)
creates the IIS site bound to `172.16.29.61:5009` and (today) installs
`windows_service.py` as the backend. Whatever ends up supervising the
process on `127.0.0.1:5009` — Windows Service or PM2 — IIS's reverse-proxy
rule doesn't need to change; it just keeps talking to `127.0.0.1:5009`.

### 2.4 What's already available on this machine

- Node.js v22.15.1 + npm 10.9.2 are already installed (confirmed directly)
  — PM2 itself isn't installed yet (`pm2 --version` → not found).
- `pii_env\Scripts\python.exe` is the venv interpreter PM2 would need to
  point at (same one `serve.py` already assumes).
- Nothing PM2-related exists in the repo today — no `ecosystem.config.js`,
  no `.pm2` references anywhere.

### 2.5 A concrete risk this plan needs to avoid repeating

Earlier this session, testing a separate feature, two processes ended up
bound to port 5009 simultaneously (the pre-existing manual `serve.py`, plus
a freshly-started `app.py` test instance) — Windows allowed both listeners,
and requests were served non-deterministically by whichever one happened to
answer, which silently served *stale* code for a while. The same failure
mode applies here: if PM2 starts managing a **new** copy of `serve.py`
without the currently-running manual one being stopped first, we'd have two
supervisors' worth of process fighting over the same port. Task list
Section 7 calls this out explicitly as a stop-and-check step.

---

## 3. Proposed Design

### 3.1 What PM2 supervises

PM2 should launch **`serve.py`** (same as today), not `app.py` directly —
keeping the existing waitress tuning (8 threads, 2-hour timeout) rather than
falling back to Flask's single-threaded dev server, which `app.py`'s
`if __name__ == "__main__"` block would otherwise use.

### 3.2 `ecosystem.config.js` (recommended over ad hoc `pm2 start` flags)

A committed config file means "how to start this in production" is one
command (`pm2 start ecosystem.config.js`) instead of a flag string someone
has to remember or re-type correctly on a new machine:

```js
module.exports = {
  apps: [{
    name: "pii-redaction",
    script: "serve.py",
    interpreter: "./pii_env/Scripts/python.exe",
    cwd: __dirname,
    watch: false,              // MUST stay false — see Open Question #4
    autorestart: true,
    max_restarts: 10,
    min_uptime: "30s",
    instances: 1,               // NOT cluster mode — see Open Question #3
    out_file: "./logs/pm2-out.log",
    error_file: "./logs/pm2-error.log",
    time: true,
  }],
};
```

`.env` is already loaded inside `app.py`/`serve.py` via `python-dotenv`, so
PM2 doesn't need an `env:` block duplicating those values — it just needs to
launch the script from the right working directory (`cwd`), which it does.
`*.log` is already in `.gitignore`, so a project-local `./logs/` output
directory won't accidentally get committed.

### 3.3 Boot-start on Windows

PM2's own daemon needs *something* to restart it after a reboot — two
established options, both via extra npm packages (Open Question #2):

- **`pm2-windows-startup`** — registers a Task Scheduler entry that runs
  `pm2 resurrect` at user logon. Quick to set up, but ties startup to a
  user session logging in (behavior on an unattended reboot depends on
  whether the machine auto-logs-in a user).
- **`pm2-windows-service`** — installs the PM2 daemon itself as a genuine
  Windows Service (via `node-windows` under the hood), which then manages
  `serve.py` as its child process. Closer to how `windows_service.py`
  behaves today (starts before any user logs in, managed via `services.msc`
  / SCM), more moving parts to install.

### 3.4 Relationship to the existing (undeployed) Windows Service path

Since `windows_service.py` already promises the exact same two things PM2
would provide (auto-restart on crash, auto-start on boot) and simply isn't
registered on this machine (Section 2.1), running **both** long-term would
be redundant at best and a repeat of the Section 2.5 port-collision risk at
worst. Recommendation: PM2 **replaces** the `windows_service.py` path
rather than sitting alongside it — but this is Open Question #1, since only
you know whether there's a reason (team familiarity with PM2's tooling from
other projects, planned future move to a Linux host where PM2 is much more
at home, etc.) to keep both documented.

---

## 4. Process diagram (proposed)

```
Windows boot
    │
    ▼
pm2-windows-service (Windows Service)  OR  pm2-windows-startup (Task Scheduler @ logon)
    │
    ▼
PM2 daemon — resurrects the saved process list (`pm2 save`)
    │
    ▼
"pii-redaction" process (serve.py → waitress → app.py's Flask app, 127.0.0.1:5009)
    │  crashes?
    └──► PM2 auto-restarts it (autorestart: true, backs off after max_restarts)

                    ▲
                    │  reverse proxy — UNCHANGED
IIS site "PIIRedaction" (172.16.29.61:5009) ── ARR/URL-rewrite ──► http://127.0.0.1:5009
```

---

## 5. Open Questions (need your answers)

1. **Why PM2, given `windows_service.py` already does this and just isn't
   deployed (Section 2.1)?** Is the goal to finally deploy real process
   supervision and PM2 is simply the tool of choice (team familiarity,
   nicer CLI/log UX), or is there a different driver — e.g. prepping for a
   future move to a Linux host where PM2 is a much more natural fit than a
   `pywin32` Windows Service? The answer decides whether
   `windows_service.py`/`setup_iis.ps1`'s service-install step gets retired
   or kept as a documented alternative (Section 3.4).
   answer:PM2, because `windows_service.py` as it stands today doesn't 
actually deliver reliable process supervision — verified by reading the 
code: it has no `--startup=auto` flag baked into the install command 
(pywin32's default is Manual startup, so reboot-recovery depends on 
whether that flag was used at install time — unverified), and it has no 
crash-recovery logic at all (`SvcDoRun` blocks on `serve()` with no 
exception handling or restart loop; Windows Service Recovery settings 
would need to be configured separately via Services.msc, and aren't 
today). PM2 gives auto-restart-on-crash and (via `pm2 startup`) 
reboot-recovery out of the box, with less manual configuration to get 
right and verify. This isn't primarily about a future Linux move — it's 
that the existing Windows Service path needs real fixes (an explicit 
`--startup=auto` install step, plus Recovery-tab configuration) before 
it actually does what's being assumed of it. Given that, `windows_service.
py`/`setup_iis.ps1`'s service-install step should be retired in favor of 
PM2, not kept as a parallel alternative — maintaining two under-tested 
supervision paths is worse than picking one and configuring it correctly.

2. **Boot-start mechanism (Section 3.3)** — `pm2-windows-startup`
   (Task Scheduler, simpler, ties to a logged-in session) or
   `pm2-windows-service` (PM2-as-a-Windows-Service, closer to today's
   `windows_service.py` behavior, starts before any login)?
   answer:`pm2-windows-service`. The whole reason for moving to PM2 was 
reliable auto-restart on server reboot (Section 2.1's finding that 
`windows_service.py` isn't reliably configured for this today) — 
`pm2-windows-startup`'s Task Scheduler approach requires a logged-in 
session, which defeats that goal for an unattended reboot (Windows 
update, power loss recovery) where no one logs in. `pm2-windows-service` 
starts before any login, matching what a production server actually 
needs and what `windows_service.py` was originally intended to provide. 
The extra setup complexity is small relative to the reliability this 
guarantees.


3. **Single instance only — confirm.** This app does file-path-based
   temp/output handling and MySQL writes per run
   ([wiki/architecture.md](../../architecture.md)); running more than one
   PM2 instance (`instances: 2+` / cluster mode) would mean two processes
   racing on the same uploaded files and DB rows, and both trying to bind
   the same port. Section 3.2 assumes `instances: 1` — confirming there's
   no reason to run more than one.
   answer:Confirmed — `instances: 1` only. This app isn't built for 
concurrent instances: it does file-path-based temp/output handling (races 
if two processes touch the same paths), writes to MySQL per run (race 
risk on the same rows), and binds a single fixed port (127.0.0.1:5009) 
that a second instance couldn't also bind. PM2's cluster mode is designed 
for stateless, horizontally-scalable apps — this one is neither, so 
there's no reason to run more than one instance.

4. **`watch: false` — confirm this must stay off.** PM2 can auto-restart a
   process whenever a watched file changes, which would be actively
   dangerous here: a redaction run can take minutes, and a restart mid-run
   would kill it partway through processing a client's document. Section
   3.2 hardcodes `watch: false` — confirming there's no reason to want file
   watching.
   answer: Confirmed — `watch: false` must stay off. This app writes and 
modifies files continuously during a run (uploads, temp processing 
artifacts, output files) as part of normal operation, not as a code 
change. If `watch` were on, that routine file activity would itself 
trigger PM2 to restart the process mid-run, killing an in-progress 
redaction partway through a client's document — actively worse than no 
supervision at all. `watch` is meant for restarting on source-code edits 
during development, not for a production pipeline that treats the 
filesystem as working storage during normal operation.

5. **Log handling** — is PM2's own log output (`./logs/pm2-out.log` /
   `pm2-error.log`, Section 3.2) sufficient on its own, or does it need to
   sit alongside/feed into the existing Windows Event Log entries that
   `windows_service.py` currently writes via `servicemanager.LogMsg`? Also:
   do you want `pm2-logrotate` set up (caps log file size/count), given a
   redaction pipeline can be fairly chatty over a long-running process?
   answer:PM2's own file logs are sufficient on their own — no need to also 
feed Windows Event Log. Event Log was mainly there for service-lifecycle 
visibility (start/stop/crash), and PM2 already surfaces that via `pm2 
logs`/`pm2 status`; maintaining two parallel logging destinations adds 
complexity without a clear requirement driving it. 

Yes, set up `pm2-logrotate` — a long-running, chatty pipeline risks 
unbounded log growth and eventual disk-space exhaustion. The setup cost 
is minimal (one `pm2 install pm2-logrotate` plus size/count config) 
relative to that risk.

6. **Should `ecosystem.config.js` be committed to the repo** (so the next
   machine/deploy is `npm install -g pm2 && pm2 start ecosystem.config.js`,
   one documented command), or is this meant as a one-off, local setup on
   this specific machine only?
   answer:Commit it to the repo. This makes deployment reproducible and 
documented — a future machine setup (server migration, disaster recovery, 
a new environment) becomes one documented command 
(`npm install -g pm2 && pm2 start ecosystem.config.js`) instead of 
recreating the config from memory. It also preserves the deliberate 
choices made here (`instances: 1`, `watch: false`) so they can't drift 
accidentally on a different machine, and any future tuning (e.g. 
log-rotation settings) gets tracked in git history. No downside — this 
is a plain config file with no secrets/credentials in it.

7. **Does `setup_iis.ps1` need updating** to install/start PM2 instead of
   (or in addition to) `windows_service.py`, so that script stays the
   accurate "one script sets up a fresh machine" path — or is this PM2
   change scoped to this one already-running machine only, with
   `setup_iis.ps1` left as documentation for a from-scratch install that
   doesn't use PM2?
   answer:Update `setup_iis.ps1` to install/start PM2 instead of 
`windows_service.py`. This follows directly from the earlier decision to 
retire `windows_service.py` rather than keep it as a parallel alternative 
— leaving the script unupdated would make it stale, misleading 
documentation that still points a future fresh-machine setup toward the 
supervision method just decided against. The script's whole value is 
being a single accurate command for a from-scratch install; if it drifts 
from the real deploy process, it stops serving that purpose and someone 
following it would deploy the wrong thing, then have to manually 
discover and correct it.

---

## 6. Database changes needed

**None.** This plan only changes how the existing process gets started and
supervised — no application code, schema, or route changes.

---

## 7. Task List

- [x] Get answers to Open Questions in Section 5
- [x] Write `ecosystem.config.js` (Section 3.2), committed to the repo root
      per Open Question #6 — `instances: 1`, `watch: false`, logs to
      `./logs/pm2-out.log` / `./logs/pm2-error.log` (already covered by the
      repo's `*.log` gitignore rule)
- [x] Update [setup_iis.ps1](../../../setup_iis.ps1) to install PM2 +
      `pm2-windows-service` (Open Question #2's answer), register PM2 as a
      Windows Service, `pm2 start ecosystem.config.js`, `pm2 save`, and
      install/configure `pm2-logrotate` (10MB/file, 14 retained, compressed
      — Open Question #5) — replacing the old `windows_service.py`
      install step entirely, per Open Question #1/#7. Added an explicit
      warning step reminding the operator to stop any process already bound
      to port 5009 before `pm2 start` runs (Section 2.5's port-collision
      risk). Verified: script parses as valid PowerShell
      (`[ScriptBlock]::Create()` on the full file content — the more direct
      `Parser]::ParseFile()` check gave false-positive errors on this file's
      em-dash/arrow characters, reproduced identically on the untouched
      original, so that check was discarded as unreliable rather than acted
      on).
- [x] Added a deprecation notice to the top of
      [windows_service.py](../../../windows_service.py)'s docstring pointing
      at PM2/this plan, so anyone opening the file directly sees it's no
      longer the active path — file itself left in place for reference, not
      deleted.
- [x] Updated [wiki/deployment.md](../../deployment.md): replaced the
      "Windows Service" section with a "PM2 (Auto-start on Boot + Crash
      Recovery)" section covering the one-time setup commands, the
      `ecosystem.config.js` properties, day-to-day `pm2` commands, and the
      same port-5009 collision warning.
- [ ] **Not yet executed on any machine — needs Administrator access and
      touches a currently-live process, see Section 9:** `npm install -g pm2
      pm2-windows-service`, confirm/stop whatever's currently on port 5009,
      `pm2-service-install`, `pm2 start ecosystem.config.js`, `pm2 save`,
      install `pm2-logrotate` — either by running the updated
      `setup_iis.ps1` end-to-end or the equivalent commands by hand
- [ ] If the (currently unregistered, per Section 2.1) `windows_service.py`
      Windows Service turns out to be registered on some *other* target
      machine — deregister it there too
- [ ] Test: kill the `pii-redaction` process's PID directly (simulating a
      crash) and confirm PM2 restarts it within a few seconds, and that
      `channel_timeout`/threads settings still match `serve.py`'s intended
      config after the restart
- [ ] Test: reboot the machine (or stop/restart the `PM2` Windows Service)
      and confirm the app comes back up automatically with no manual steps
- [ ] Test: start a redaction run, then trigger an unrelated `pm2 restart`
      of a *different* PM2-managed app (if any exist on the same host) to
      confirm it doesn't disturb the in-flight run — sanity check that
      `watch: false` (Open Question #4) is actually taking effect

---

## 8. Out of Scope (for this plan)

- Migrating the deployment from Windows to Linux — PM2 is a much more
  natural fit on Linux, but that's a separate decision; flagged only in
  case Open Question #1's answer reveals this is actually the long-term
  direction.
- Any change to IIS/ARR reverse-proxy configuration (`web.config`) — PM2
  only changes what supervises the process IIS already proxies to.
- PM2 cluster/multi-instance mode (Section 5, Open Question #3) — not used.
- Any change to `serve.py`'s waitress tuning (thread count, timeouts,
  connection limits) — out of scope, unchanged by this plan.
- Application-level health checks/monitoring beyond what PM2's own
  `autorestart` provides (e.g. hitting `/` on a timer and alerting) —
  not covered unless you want it added as a follow-up.

---

## 9. Implementation status (2026-08-18)

### 9.1 What was done

All repo-level artifacts are in place and verified:

- [ecosystem.config.js](../../../ecosystem.config.js) — new file, repo root.
- [setup_iis.ps1](../../../setup_iis.ps1) — Section 7 (Windows Service
  install) replaced with PM2 install/registration/start/logrotate; header
  comment, variables, and closing summary updated to match; Section 5/6/8
  (waitress+pywin32 install, IIS site creation, firewall) untouched, since
  `pywin32` is still needed elsewhere (`dual_redaction_pipeline.py`,
  `redaction_formats.py` — checked directly, not just assumed) and none of
  that logic depends on which process supervisor is used.
- [windows_service.py](../../../windows_service.py) — deprecation notice
  added to the docstring; file otherwise untouched, kept for reference.
- [wiki/deployment.md](../../deployment.md) — "Windows Service" section
  replaced with "PM2 (Auto-start on Boot + Crash Recovery)".

### 9.2 What was deliberately *not* done

The actual install/execution steps — `npm install -g pm2
pm2-windows-service`, stopping whatever's currently on port 5009,
`pm2-service-install`, `pm2 start ecosystem.config.js`, `pm2 save`,
`pm2 install pm2-logrotate` — were **not** run against this machine.
Reasons:

- They require Administrator privileges.
- They register a new Windows Service and a background daemon — state that
  persists beyond this session and isn't trivially reversible.
- Most importantly: this machine currently has a real `python serve.py`
  process (confirmed earlier this session — Get-Service showed
  `PIIRedactionPipeline` was never registered, so the running process was
  started ad hoc) actually answering requests via the IIS reverse proxy.
  Stopping it to hand the port to PM2 is a live-traffic-affecting action,
  not a safe default to take unprompted.

Answering the plan's Open Questions settled the *design* — it wasn't
instruction to execute Administrator-level changes against a
possibly-in-use production process. That's a separate go/no-go.

### 9.3 What's needed to finish this

Either run the updated `setup_iis.ps1` end-to-end (as Administrator) on the
target machine, or run the equivalent commands from
[wiki/deployment.md](../../deployment.md)'s new "PM2" section by hand. Either
way, **check what's currently bound to port 5009 first**
(`netstat -ano | findstr :5009`) and stop it cleanly before `pm2 start` —
Section 2.5's port-collision risk is real and was reproduced once already
this session on unrelated work. Let me know when you'd like to do this (or
want me to walk through it live) and whether it's this machine or a
different target.
