"""Process lifecycle helpers: single-instance lock and server readiness.

Single instance: ``acquire_instance_lock()`` takes an exclusive ``flock`` on
``DATA_DIR/mingly.lock`` for the life of the process (the OS releases it on
exit or crash, so there are no stale locks) and records the server port in
it. A second launch gets ``InstanceAlreadyRunning`` carrying the running
instance's port, can ask that instance to bring its window forward with
``focus_existing_instance(port)``, and exits.

Readiness: ``mark_ready()`` is called by the app lifespan once every startup
handler has finished; ``GET /api/health/ready`` reports it, and
``wait_until_ready`` lets the launcher poll it before opening the webview.
"""

import fcntl
import json
import logging
import os
import threading
import time
import urllib.request
from typing import Callable, Optional

from backend.config import API_HOST, DATA_DIR

logger = logging.getLogger(__name__)

LOCK_FILE = DATA_DIR / "mingly.lock"

_lock_fd: Optional[int] = None
_ready = threading.Event()
_focus_callback: Optional[Callable[[], None]] = None


class InstanceAlreadyRunning(Exception):
    def __init__(self, port: Optional[int], pid: Optional[int]):
        self.port = port
        self.pid = pid
        super().__init__(f"Mingly is already running (pid {pid}, port {port})")


def acquire_instance_lock(port: int) -> None:
    """Hold the single-instance lock or raise InstanceAlreadyRunning."""
    global _lock_fd
    if _lock_fd is not None:
        return
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            info = json.loads(os.pread(fd, 4096, 0) or b"{}")
        except ValueError:
            info = {}
        os.close(fd)
        raise InstanceAlreadyRunning(info.get("port"), info.get("pid"))
    os.ftruncate(fd, 0)
    os.pwrite(fd, json.dumps({"pid": os.getpid(), "port": port}).encode(), 0)
    _lock_fd = fd


def release_instance_lock() -> None:
    global _lock_fd
    if _lock_fd is None:
        return
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(_lock_fd)
        _lock_fd = None


def set_focus_callback(fn: Optional[Callable[[], None]]) -> None:
    """Register how to bring this instance's window to the front."""
    global _focus_callback
    _focus_callback = fn


def request_focus() -> bool:
    if _focus_callback is None:
        return False
    try:
        _focus_callback()
        return True
    except Exception:
        logger.exception("Focus callback failed")
        return False


def focus_existing_instance(port: Optional[int]) -> bool:
    """Ask the already-running instance to show its window."""
    if not port:
        return False
    req = urllib.request.Request(f"http://{API_HOST}:{port}/api/instance/focus", data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def mark_ready() -> None:
    _ready.set()


def mark_not_ready() -> None:
    _ready.clear()


def is_ready() -> bool:
    return _ready.is_set()


def wait_until_ready(
    port: int,
    server_thread: Optional[threading.Thread] = None,
    timeout: Optional[float] = None,
    interval: float = 0.2,
) -> bool:
    """Poll ``/api/health/ready`` until the server reports ready.

    Returns False if *server_thread* dies first (startup failed) or *timeout*
    seconds pass; there is no timeout by default because migrations on a
    large database can legitimately take a while.
    """
    url = f"http://{API_HOST}:{port}/api/health/ready"
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if server_thread is not None and not server_thread.is_alive():
            return False
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(interval)
