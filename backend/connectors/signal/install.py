"""App-managed signal-cli: a one-time download of signal-cli and the Java
runtime it runs on, so connecting Signal needs no Homebrew or terminal.

Both archives are pinned (version, size and SHA-256) and verified while they
stream; extraction accepts only plain files and directories inside the
target. The install is assembled in a temporary directory, checked by
running ``signal-cli --version`` on the downloaded Java, and only then
renamed into ``DATA_DIR/signal-runtime/<release>``, so a half-done or broken
install is never used. Signal's keys stay in ``DATA_DIR/signal`` (see
``cli.signal_dir``); reinstalling never touches them.

Updating signal-cli (Signal stops accepting old clients every few months)
means bumping the pins below; the new release installs next to the old one
and the old one is removed once the new one works.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import platform
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

import httpx

from backend import config
from backend.tasks import spawn

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Archive:
    url: str
    sha256: str
    size: int


SIGNAL_CLI_VERSION = "0.14.9"
SIGNAL_CLI = Archive(
    f"https://github.com/AsamK/signal-cli/releases/download/v{SIGNAL_CLI_VERSION}/signal-cli-{SIGNAL_CLI_VERSION}.tar.gz",
    "c32b87f587198cbd277c9d71228716ee6047ee2b63e8e88728e8280d9407d65a",
    124262811,
)

# Eclipse Temurin JRE (signal-cli 0.14 needs Java 25), per CPU
JRE_VERSION = "25.0.4.1+1"
_TEMURIN = "https://github.com/adoptium/temurin25-binaries/releases/download/jdk-25.0.4.1%2B1"
JRE: dict[str, Archive] = {
    "arm64": Archive(
        f"{_TEMURIN}/OpenJDK25U-jre_aarch64_mac_hotspot_25.0.4.1_1.tar.gz",
        "5b02bcc908da092e7e00b9772d4f9bbb87f44a60b7c90f7cf5a91a1d98f37e1b",
        56208642,
    ),
    "x86_64": Archive(
        f"{_TEMURIN}/OpenJDK25U-jre_x64_mac_hotspot_25.0.4.1_1.tar.gz",
        "099cbc182af99ab454991a4230f0bdbf2b2ee3f61ce462d637aceaa12709380d",
        42138276,
    ),
}

RELEASE = f"signal-cli-{SIGNAL_CLI_VERSION}_jre-{JRE_VERSION}"
VERSION_CHECK_TIMEOUT = 120  # first JVM start on a fresh install can be slow
_HTTP_TIMEOUT = httpx.Timeout(30.0, read=60.0)


class InstallError(Exception):
    """Setting up signal-cli failed; the message is shown as is."""


def runtime_root() -> Path:
    return Path(config.DATA_DIR) / "signal-runtime"


def _release_dir() -> Path:
    return runtime_root() / RELEASE


def _signal_cli(home: Path) -> Path:
    return home / "signal-cli" / "bin" / "signal-cli"


def _java_home(home: Path) -> Path:
    return home / "jre" / "Contents" / "Home"


def installed() -> Optional[tuple[str, str]]:
    """``(signal-cli executable, JAVA_HOME)`` of the app-managed install."""
    home = _release_dir()
    exe, java_home = _signal_cli(home), _java_home(home)
    if os.access(exe, os.X_OK) and os.access(java_home / "bin" / "java", os.X_OK):
        return str(exe), str(java_home)
    return None


def jre_archive() -> Archive:
    machine = platform.machine()
    archive = JRE.get(machine)
    if archive is None:
        raise InstallError(f"Signal support isn't available for this Mac's processor ({machine}).")
    return archive


def download_size() -> int:
    """Bytes to download for a setup on this Mac."""
    return SIGNAL_CLI.size + jre_archive().size


# ── state ────────────────────────────────────────────────────────────────────

@dataclass
class InstallState:
    status: str = "idle"  # idle | downloading | unpacking | checking | done | error
    downloaded: int = 0
    total: int = 0
    error: Optional[str] = None

    @property
    def running(self) -> bool:
        return self.status in ("downloading", "unpacking", "checking")


_state = InstallState()
_task: Optional[asyncio.Task] = None


def state() -> InstallState:
    return _state


def start() -> InstallState:
    """Start setting up (no-op while a setup is running)."""
    global _state, _task
    if _state.running:
        return _state
    try:
        total = download_size()
    except InstallError:
        total = 0  # _install reports the error
    _state = InstallState(status="downloading", total=total)
    _task = spawn(_install(_state), name="signal_cli_install", on_error=lambda e: _fail(_state, e))
    return _state


def _fail(st: InstallState, exc: BaseException) -> None:
    st.status = "error"
    st.error = str(exc) or type(exc).__name__


# ── steps ────────────────────────────────────────────────────────────────────

async def _download(client: httpx.AsyncClient, archive: Archive, dest: Path, st: InstallState) -> None:
    """Stream ``archive`` to ``dest``, hashing as it goes (fail closed)."""
    digest = hashlib.sha256()
    received = 0
    async with client.stream("GET", archive.url) as resp:
        resp.raise_for_status()
        with dest.open("wb") as f:
            async for chunk in resp.aiter_bytes(1 << 16):
                received += len(chunk)
                if received > archive.size:
                    raise InstallError("A download was larger than expected; it may have been tampered with.")
                digest.update(chunk)
                f.write(chunk)
                st.downloaded += len(chunk)
    if received != archive.size or digest.hexdigest() != archive.sha256:
        raise InstallError("A download didn't match its expected checksum; it may be corrupted or tampered with.")


def _extract(archive_path: Path, dest: Path) -> None:
    """Unpack the archive's single top directory into ``dest``. Only regular
    files and directories, never outside ``dest``."""
    dest.mkdir(parents=True)
    with tarfile.open(archive_path, "r:gz") as tf:
        for member in tf.getmembers():
            parts = [p for p in PurePosixPath(member.name).parts if p != "."]
            if member.name.startswith("/") or ".." in parts:
                raise InstallError(f"Unsafe path in download: {member.name}")
            rel = parts[1:]  # strip the top directory
            if not rel:
                continue
            if not (member.isfile() or member.isdir()):
                raise InstallError(f"Unexpected entry in download: {member.name}")
            target = dest.joinpath(*rel)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            assert src is not None
            with src, target.open("wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)


async def _version_ok(home: Path) -> None:
    env = {**os.environ, "JAVA_HOME": str(_java_home(home))}
    proc = await asyncio.create_subprocess_exec(
        str(_signal_cli(home)), "--version",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), VERSION_CHECK_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise InstallError("signal-cli didn't start in time after setup.")
    text = out.decode(errors="replace")
    if proc.returncode != 0 or SIGNAL_CLI_VERSION not in text:
        detail = (err.decode(errors="replace") or text).strip().splitlines()[-1:] or [f"exit code {proc.returncode}"]
        raise InstallError(f"signal-cli didn't run after setup ({detail[0]}).")


async def _install(st: InstallState) -> None:
    root = runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    work = root / f".setup-{os.getpid()}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    try:
        jre = jre_archive()
        st.total = SIGNAL_CLI.size + jre.size
        loop = asyncio.get_running_loop()
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            await _download(client, SIGNAL_CLI, work / "signal-cli.tar.gz", st)
            await _download(client, jre, work / "jre.tar.gz", st)
        st.status = "unpacking"
        staged = work / RELEASE
        await loop.run_in_executor(None, _extract, work / "signal-cli.tar.gz", staged / "signal-cli")
        await loop.run_in_executor(None, _extract, work / "jre.tar.gz", staged / "jre")
        st.status = "checking"
        await _version_ok(staged)
        final = _release_dir()
        shutil.rmtree(final, ignore_errors=True)
        os.replace(staged, final)
        # Older releases and leftovers of interrupted setups
        for other in root.iterdir():
            if other != final and other != work:
                shutil.rmtree(other, ignore_errors=True)
        st.status = "done"
        log.info("Signal support set up: %s", RELEASE)
    except InstallError as e:
        st.status, st.error = "error", str(e)
    except httpx.HTTPError as e:
        st.status, st.error = "error", f"Download failed ({e.__class__.__name__}). Check your internet connection and try again."
    except OSError as e:
        st.status, st.error = "error", f"Couldn't save Signal support ({e.strerror or e})."
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if st.status == "error":
            log.warning("Signal support setup failed: %s", st.error)
