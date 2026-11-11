# Deployment

## Development (Flask built-in server)

Good for local testing. Not suitable for concurrent users or production.

```powershell
& "c:\Users\User_Shibu\Downloads\script_pii\pii_env\Scripts\Activate.ps1"
python app.py
# Listening on http://0.0.0.0:5009
```

---

## Production (Waitress WSGI Server)

Waitress is a pure-Python production-grade WSGI server with no external dependencies. It handles multiple threads, long-running requests (the pipeline can take minutes), and proper HTTP keep-alive.

```powershell
& "c:\Users\User_Shibu\Downloads\script_pii\pii_env\Scripts\Activate.ps1"
python serve.py
# PII Redaction Pipeline → http://127.0.0.1:5009
```

**serve.py configuration:**

| Setting | Value | Notes |
|---------|-------|-------|
| `host` | `127.0.0.1` | Loopback only — put nginx/IIS in front for external access |
| `port` | `5009` | |
| `threads` | `8` | Concurrent request workers |
| `channel_timeout` | `7200` | 2 hours — allows long pipeline runs without disconnect |
| `connection_limit` | `100` | Max simultaneous connections |
| `ident` | `PIIRedaction/1.0` | Server header |

---

## PM2 (Auto-start on Boot + Crash Recovery)

Process supervision — auto-restart on crash, auto-start on boot, and a simple
CLI for status/logs/restart — is provided by [PM2](https://pm2.keymetrics.io/),
supervising `serve.py` (same waitress process and tuning as above).

> This replaces the older `windows_service.py`-based Windows Service approach.
> That path is still in the repo for reference, but is retired: as built, it
> had no configured crash-recovery (Windows Service Recovery actions were
> never set) and defaulted to Manual startup unless changed by hand in
> `services.msc`, so it didn't reliably provide either of the two things it
> was meant to. See
> [plan-pm2-implementation.md](feature-updates/phase-1/plan-pm2-implementation.md)
> for the full reasoning.

**Requires:** Node.js + npm installed, and running as Administrator for the
service-registration step.

```powershell
# One-time setup — run in an elevated (Administrator) PowerShell

# Install PM2 and the Windows-Service wrapper for it (global npm packages)
npm install -g pm2 pm2-windows-service

# Register PM2's own daemon as a Windows Service (starts before any user logs in)
# — will prompt for PM2_HOME and a couple of other settings; defaults are fine.
pm2-service-install

# Start the app under PM2, using the committed ecosystem.config.js
cd "c:\Users\User_Shibu\Downloads\script_pii"
pm2 start ecosystem.config.js

# Save the process list so it's resurrected automatically after a reboot
pm2 save

# Optional: cap PM2's own log file size/count
pm2 install pm2-logrotate
pm2 set pm2-logrotate:max_size 10M
pm2 set pm2-logrotate:retain 14
pm2 set pm2-logrotate:compress true
```

`setup_iis.ps1` performs all of the above automatically as part of a
from-scratch machine setup.

**App details ([ecosystem.config.js](../ecosystem.config.js)):**

| Property | Value | Notes |
|----------|-------|-------|
| App name | `pii-redaction` | Used in `pm2` commands below |
| `instances` | `1` | Not cluster mode — this app does per-run file/DB state that a second concurrent instance would race |
| `watch` | `false` | Must stay off — the app rewrites files as part of normal operation (uploads, temp artifacts, output); watching the tree would restart it mid-run |
| `autorestart` | `true` | Restarts automatically if the process dies |

**Day-to-day commands:**

```powershell
pm2 status                    # Is it running?
pm2 logs pii-redaction        # Tail logs
pm2 restart pii-redaction     # Restart
pm2 stop pii-redaction        # Stop
```

**Before switching an already-running manual `python serve.py` over to PM2:**
stop that process first (check with `netstat -ano | findstr :5009`) — starting
PM2's copy while the old one is still bound to the port leaves two processes
competing for it, and requests get served non-deterministically by whichever
one happens to answer.

---

## Keeping Ollama Running

Ollama must be running for Tier 3 LLM decisions to work. To start it as a background service:

```powershell
# Start Ollama (runs on http://localhost:11434)
Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden
```

Or install the Ollama Windows app which adds a system-tray icon and starts automatically.

---

## Firewall / Port Access

By default the app binds to `127.0.0.1` (loopback only). To allow access from other machines on the network, change `serve.py`:

```python
host = "0.0.0.0"   # Listen on all interfaces
```

Then open port 5009 in Windows Firewall:

```powershell
New-NetFirewallRule -DisplayName "PII Redaction" -Direction Inbound -Protocol TCP -LocalPort 5009 -Action Allow
```

> **Security note:** The pipeline handles sensitive documents. Exposing it on `0.0.0.0` without authentication is a security risk. Consider placing it behind a reverse proxy (nginx, IIS) with HTTPS and basic auth, or restrict to a VPN.
