"""Finding and running the ``signal-cli`` executable.

signal-cli is a Java program. Mingly sets it up itself on first use
(``install.py``: signal-cli plus a Java runtime under ``DATA_DIR``), and that
install is always preferred. A Homebrew signal-cli is used when there is no
app-managed one; the packaged app inherits launchd's minimal PATH, so
Homebrew's bin directories are searched explicitly and prepended to the
child's PATH (signal-cli's launcher in turn has to find Java).

All of signal-cli's state for Mingly (keys, linked accounts, received
attachments) lives in ``DATA_DIR/signal`` (mode 0700), passed as ``--config``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from backend import config
from backend.connectors.signal import install

HOMEBREW_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
JAVA_HOMES = ("/opt/homebrew/opt/openjdk", "/usr/local/opt/openjdk")
VERSION_TIMEOUT = 60  # first JVM start after an install can be slow
STREAM_LIMIT = 16 * 1024 * 1024  # one JSON line (a contact list) can be large


class SignalCliMissing(Exception):
    """signal-cli (or the Java runtime it needs) is not usable; setting up
    the app-managed one fixes it. ``broken`` = found but failed to run."""

    def __init__(self, message: str, broken: bool = False) -> None:
        super().__init__(message)
        self.broken = broken


def signal_dir() -> Path:
    """signal-cli's ``--config`` directory, created private (0700)."""
    path = Path(config.DATA_DIR) / "signal"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def attachments_dir() -> Path:
    return signal_dir() / "attachments"


def find_signal_cli() -> Optional[str]:
    managed = install.installed()
    if managed is not None:
        return managed[0]
    found = shutil.which("signal-cli")
    if found:
        return found
    for d in HOMEBREW_BIN_DIRS:
        candidate = os.path.join(d, "signal-cli")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def child_env() -> dict[str, str]:
    """The environment for signal-cli: the app-managed Java runtime when
    Mingly set signal-cli up (it needs that exact Java, whatever JAVA_HOME
    says), else Homebrew dirs on PATH so its launcher finds ``java``."""
    env = dict(os.environ)
    managed = install.installed()
    if managed is not None:
        env["JAVA_HOME"] = managed[1]
        return env
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    for d in reversed(HOMEBREW_BIN_DIRS):
        if d not in parts:
            parts.insert(0, d)
    env["PATH"] = os.pathsep.join(parts)
    if not env.get("JAVA_HOME"):
        for home in JAVA_HOMES:
            if os.path.isfile(os.path.join(home, "bin", "java")):
                env["JAVA_HOME"] = home
                break
    return env


def java_available() -> bool:
    env = child_env()
    if env.get("JAVA_HOME") and os.path.isfile(os.path.join(env["JAVA_HOME"], "bin", "java")):
        return True
    java = shutil.which("java", path=env["PATH"])
    # /usr/bin/java is a stub on macOS that only offers to install a JDK.
    return bool(java) and java != "/usr/bin/java"


@dataclass
class CliResult:
    returncode: int
    stdout: str
    stderr: str


async def run(executable: str, *args: str, timeout: float = 120) -> CliResult:
    """Run a one-shot signal-cli command."""
    proc = await asyncio.create_subprocess_exec(
        executable, *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=child_env(),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return CliResult(proc.returncode, out.decode(errors="replace"), err.decode(errors="replace"))


async def check_installed() -> tuple[str, str]:
    """``(executable, version)``; raises SignalCliMissing with a message
    the sign-in screen shows as is."""
    exe = find_signal_cli()
    if exe is None:
        raise SignalCliMissing("Signal support isn't set up yet.")
    try:
        res = await run(exe, "--version", timeout=VERSION_TIMEOUT)
    except (OSError, asyncio.TimeoutError) as e:
        res = CliResult(-1, "", str(e) or type(e).__name__)
    if res.returncode != 0:
        if install.installed() is None and not java_available():
            raise SignalCliMissing("signal-cli was found but there's no Java runtime to run it.", broken=True)
        detail = (res.stderr or res.stdout).strip().splitlines()[-1:] or ["unknown error"]
        raise SignalCliMissing(f"signal-cli failed to run ({detail[0]}).", broken=True)
    return exe, res.stdout.strip()


def config_args() -> list[str]:
    return ["--config", str(signal_dir())]


_NUMBER = re.compile(r"\+\d{6,15}")


def parse_numbers(text: str) -> list[str]:
    """Phone numbers from ``listAccounts`` / ``link`` output (plain or JSON)."""
    try:
        data = json.loads(text)
    except ValueError:
        return _NUMBER.findall(text)
    if isinstance(data, list):
        return [d["number"] for d in data if isinstance(d, dict) and d.get("number")]
    return []


async def list_accounts(executable: str) -> Optional[list[str]]:
    """Numbers linked in Mingly's signal-cli config; None when unknown."""
    try:
        res = await run(executable, *config_args(), "-o", "json", "listAccounts", timeout=VERSION_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return None
    return parse_numbers(res.stdout) if res.returncode == 0 else None


def account_aci(number: str) -> Optional[str]:
    """The account's ACI uuid from signal-cli's account index, if recorded."""
    try:
        data = json.loads((signal_dir() / "data" / "accounts.json").read_text())
    except (OSError, ValueError):
        return None
    for acc in data.get("accounts") or []:
        if isinstance(acc, dict) and acc.get("number") == number and acc.get("uuid"):
            return str(acc["uuid"])
    return None
