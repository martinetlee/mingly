"""
Development entry point. Runs the FastAPI backend only.
Start the frontend separately with: cd frontend && npm run dev

Port: MINGLY_PORT or 8765 (the Vite proxy targets 8765).
"""
import sys

import uvicorn

from backend.config import API_HOST, API_PORT
from backend.instance import InstanceAlreadyRunning, acquire_instance_lock

if __name__ == "__main__":
    try:
        acquire_instance_lock(API_PORT)
    except InstanceAlreadyRunning as exc:
        sys.exit(f"{exc}. Stop it first (same data directory).")
    uvicorn.run(
        "backend.main:app",
        host=API_HOST,
        port=API_PORT,
        reload=False,
        log_level="info",
    )
