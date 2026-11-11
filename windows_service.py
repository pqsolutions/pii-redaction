"""
Windows Service wrapper for PII Redaction Pipeline.

DEPRECATED: process supervision is now handled by PM2 (see
ecosystem.config.js and wiki/deployment.md, "PM2" section). This module is
kept for reference but is no longer installed or started by setup_iis.ps1 —
as built, it has no configured crash-recovery (Windows Service Recovery
actions were never set) and defaults to Manual startup unless changed by
hand, so it didn't reliably provide either auto-restart-on-crash or
auto-start-on-boot. See
wiki/feature-updates/phase-1/plan-pm2-implementation.md for the full
reasoning.

Run as Administrator:
    python windows_service.py install    # register service
    python windows_service.py start      # start it
    python windows_service.py stop       # stop it
    python windows_service.py restart    # restart
    python windows_service.py remove     # uninstall
"""
import sys
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
os.chdir(str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR))

try:
    import win32serviceutil
    import win32service
    import win32event
    import servicemanager
except ImportError:
    print("pywin32 not installed.  Run:  pip install pywin32", flush=True)
    sys.exit(1)


class PIIRedactionService(win32serviceutil.ServiceFramework):
    _svc_name_         = "PIIRedactionPipeline"
    _svc_display_name_ = "PII Redaction Pipeline"
    _svc_description_  = "Web-based PII redaction pipeline (Flask + Waitress on port 5000)"

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.hWaitStop)

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        self._main()

    def _main(self):
        from waitress import serve
        from app import app
        serve(app, host="127.0.0.1", port=5009, threads=8, channel_timeout=7200)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(PIIRedactionService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(PIIRedactionService)
