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
API_PORT = 8765

TG_API_ID = int(os.getenv("TG_API_ID") or "0")
TG_API_HASH = os.getenv("TG_API_HASH") or ""
