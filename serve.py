"""
Production WSGI server — run directly or via Windows Service.
"""
import sys
import os
import socket
from pathlib import Path

BASE_DIR = Path(__file__).parent
os.chdir(str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR))

try:
    from waitress import serve
except ImportError:
    print("waitress not installed.  Run:  pip install waitress", flush=True)
    sys.exit(1)

from app import app

if __name__ == "__main__":
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", 5009))
    try:
        _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _s.connect(("8.8.8.8", 80))
        lan_ip = _s.getsockname()[0]
        _s.close()
    except Exception:
        lan_ip = "127.0.0.1"
    print(f"PII Redaction Pipeline -> http://{lan_ip}:{port}", flush=True)
    serve(
        app,
        host=host,
        port=port,
        threads=8,
        channel_timeout=7200,       # 2 hours — allow long pipeline runs
        connection_limit=100,
        cleanup_interval=30,
        ident="PIIRedaction/1.0",
    )
