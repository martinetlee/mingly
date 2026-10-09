import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent

if os.environ.get("MINGLY_DATA_DIR"):
    DATA_DIR = Path(os.environ["MINGLY_DATA_DIR"]).expanduser()
elif "pytest" in sys.modules:
    # Any test process (whatever conftest it loads) gets a throwaway data dir,
    # so no test or test-like script can ever touch the real app data.
    import tempfile

    DATA_DIR = Path(tempfile.mkdtemp(prefix="mingly-test-data-"))
elif getattr(sys, "frozen", False):
    # Running inside a PyInstaller .app bundle — persist data in ~/Library
    DATA_DIR = Path.home() / "Library" / "Application Support" / "Mingly"
else:
    DATA_DIR = BASE_DIR / "data"

DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = f"sqlite+aiosqlite:///{DATA_DIR}/mingly.db"

# Shield (scam detection) — retired upstream; off unless MINGLY_SHIELD=1.
# See backend/features.py.
SHIELD_ENABLED = os.environ.get("MINGLY_SHIELD", "0") == "1"

API_HOST = "127.0.0.1"
_DEFAULT_PORT = 8765
# Remembers a non-default port chosen because 8765 was taken by another
# program. The webview origin (and so its localStorage) depends on the port,
# so it must stay the same from launch to launch.
_PORT_FILE = DATA_DIR / "server_port.json"


def _port_in_use(host: str, port: int) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _is_mingly(host: str, port: int) -> bool:
    """True if a Mingly backend answers on *port* (see /api/health)."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=0.5) as resp:
            return json.loads(resp.read()).get("app") == "mingly"
    except Exception:
        return False


def _read_persisted_port() -> int:
    try:
        port = int(json.loads(_PORT_FILE.read_text())["port"])
        return port if 1024 <= port <= 65535 else 0
    except Exception:
        return 0


def _resolve_port(host: str) -> int:
    """Pick the server port.

    Development uses a fixed port (MINGLY_PORT or 8765) because the Vite
    proxy targets it; a busy port fails loudly at bind time.

    The packaged app uses the persisted port (or 8765). If that port is held
    by another Mingly, we keep it: the single-instance lock (backend/instance)
    then hands off to the running instance. If another program holds it, we
    pick a free port once and persist it so the origin stays stable.
    """
    if not getattr(sys, "frozen", False):
        return int(os.getenv("MINGLY_PORT") or _DEFAULT_PORT)
    preferred = _read_persisted_port() or _DEFAULT_PORT
    if not _port_in_use(host, preferred) or _is_mingly(host, preferred):
        return preferred
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        port = s.getsockname()[1]
    try:
        from backend.fsutil import atomic_write_text

        atomic_write_text(_PORT_FILE, json.dumps({"port": port}))
    except OSError:
        pass
    return port


API_PORT = _resolve_port(API_HOST)

TG_API_ID = int(os.getenv("TG_API_ID") or "0")
TG_API_HASH = os.getenv("TG_API_HASH") or ""
