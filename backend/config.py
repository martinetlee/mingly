import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent

if getattr(sys, "frozen", False):
    # Running inside a PyInstaller .app bundle — persist data in ~/Library
    DATA_DIR = Path.home() / "Library" / "Application Support" / "Mingly"
else:
    DATA_DIR = BASE_DIR / "data"

DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = f"sqlite+aiosqlite:///{DATA_DIR}/mingly.db"

API_HOST = "127.0.0.1"
_DEFAULT_PORT = 8765


def _find_free_port(host: str, preferred: int) -> int:
    """Return *preferred* if available, otherwise ask the OS for a free port."""
    import socket

    try:
        with socket.create_connection((host, preferred), timeout=0.2):
            pass
        # Connection succeeded → port is in use, pick a random free one
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
            return s.getsockname()[1]
    except OSError:
        # Connection refused → port is free
        return preferred


API_PORT = _find_free_port(API_HOST, _DEFAULT_PORT)

TG_API_ID = int(os.getenv("TG_API_ID") or "0")
TG_API_HASH = os.getenv("TG_API_HASH") or ""
