"""App-managed signal-cli setup (backend/connectors/signal/install.py):
pinned downloads verified while streaming, safe extraction, the post-install
run check, atomic placement, and that detection prefers the result. Archives
are small fakes served through httpx's MockTransport; nothing is downloaded.
"""

import hashlib
import io
import os
import platform
import tarfile

import httpx
import pytest

from backend.connectors.signal import cli, install

SIGNAL_URL = "https://downloads.test/signal-cli.tar.gz"
JRE_URL = "https://downloads.test/jre.tar.gz"


def _tar(entries: list[tuple]) -> bytes:
    """entries: (name, content|None for dir, mode) or (name, "SYMLINK", target)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content, extra in entries:
            info = tarfile.TarInfo(name)
            if content is None:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tf.addfile(info)
            elif content == "SYMLINK":
                info.type, info.linkname = tarfile.SYMTYPE, extra
                tf.addfile(info)
            else:
                data = content.encode()
                info.size, info.mode = len(data), extra
                tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _signal_cli_tar(version_exit: int = 0) -> bytes:
    # The real archive's members start with "./" (and include "." itself)
    script = (
        "#!/bin/sh\n"
        '[ -x "$JAVA_HOME/bin/java" ] || { echo "no java" >&2; exit 3; }\n'
        f"[ {version_exit} -eq 0 ] || {{ echo 'boom' >&2; exit {version_exit}; }}\n"
        f'echo "signal-cli {install.SIGNAL_CLI_VERSION}"\n'
    )
    return _tar([
        (".", None, None),
        ("./signal-cli-x/", None, None),
        ("./signal-cli-x/bin/signal-cli", script, 0o755),
        ("./signal-cli-x/lib/signal-cli.jar", "jar", 0o644),
    ])


def _jre_tar(extra: tuple = ()) -> bytes:
    return _tar([
        ("jdk-x-jre/Contents/Home/bin/java", "#!/bin/sh\nexit 0\n", 0o755),
        ("jdk-x-jre/Contents/Home/lib/modules", "modules", 0o644),
        *extra,
    ])


@pytest.fixture
def serve(monkeypatch):
    """Pin the archives to fakes and serve them; returns a setter."""
    served: dict[str, bytes] = {}
    pins: dict[str, bytes] = {}
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        body = served.get(str(request.url))
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)

    def set_archives(signal_tar: bytes, jre_tar: bytes, *, pin_signal: bytes = None, pin_jre: bytes = None):
        served[SIGNAL_URL], served[JRE_URL] = signal_tar, jre_tar
        pins["signal"], pins["jre"] = pin_signal or signal_tar, pin_jre or jre_tar
        monkeypatch.setattr(install, "SIGNAL_CLI", install.Archive(
            SIGNAL_URL, hashlib.sha256(pins["signal"]).hexdigest(), len(pins["signal"])))
        monkeypatch.setattr(install, "JRE", {platform.machine(): install.Archive(
            JRE_URL, hashlib.sha256(pins["jre"]).hexdigest(), len(pins["jre"]))})

    set_archives.requests = requests
    return set_archives


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(install, "_state", install.InstallState())
    monkeypatch.setattr(install, "_task", None)


async def _run() -> install.InstallState:
    st = install.start()
    await install._task
    return st


def _leftovers() -> list[str]:
    root = install.runtime_root()
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


@pytest.mark.asyncio
async def test_setup_installs_and_detection_prefers_it(serve, monkeypatch, tmp_path):
    serve(_signal_cli_tar(), _jre_tar())
    stale = install.runtime_root() / "signal-cli-0.0.1_jre-1"
    stale.mkdir(parents=True)
    st = await _run()
    assert st.status == "done", st.error
    assert st.downloaded == st.total == install.SIGNAL_CLI.size + install.jre_archive().size
    exe, java_home = install.installed()
    assert exe.endswith("signal-cli/bin/signal-cli") and os.access(exe, os.X_OK)
    assert os.access(os.path.join(java_home, "bin", "java"), os.X_OK)
    assert oct(os.stat(os.path.join(java_home, "lib", "modules")).st_mode & 0o777) == "0o644"
    assert _leftovers() == [install.RELEASE]  # old release and the work dir are gone

    # The managed install wins over a Homebrew one and brings its own Java
    monkeypatch.setattr(cli.shutil, "which", lambda *a, **k: "/opt/homebrew/bin/signal-cli")
    monkeypatch.setenv("JAVA_HOME", str(tmp_path / "old-java"))
    assert cli.find_signal_cli() == exe
    assert cli.child_env()["JAVA_HOME"] == java_home
    _, version = await cli.check_installed()
    assert install.SIGNAL_CLI_VERSION in version


@pytest.mark.asyncio
async def test_tampered_download_is_rejected(serve):
    good = _signal_cli_tar()
    bad = bytearray(good)
    bad[-1] ^= 0xFF
    serve(bytes(bad), _jre_tar(), pin_signal=good)
    st = await _run()
    assert st.status == "error" and "checksum" in st.error
    assert install.installed() is None and _leftovers() == []


@pytest.mark.asyncio
async def test_oversized_download_is_cut_off(serve):
    jre = _jre_tar()
    serve(_signal_cli_tar(), jre + b"x" * 10, pin_jre=jre)
    st = await _run()
    assert st.status == "error" and "larger than expected" in st.error
    assert install.installed() is None and _leftovers() == []


@pytest.mark.asyncio
async def test_path_escaping_the_target_is_rejected(serve, tmp_path):
    serve(_signal_cli_tar(), _jre_tar(extra=(("jdk-x-jre/../../../evil", "x", 0o644),)))
    st = await _run()
    assert st.status == "error" and "Unsafe path" in st.error
    assert not any(p.name == "evil" for p in tmp_path.rglob("evil"))
    assert install.installed() is None and _leftovers() == []


@pytest.mark.asyncio
async def test_links_in_archives_are_rejected(serve):
    serve(_signal_cli_tar(), _jre_tar(extra=(("jdk-x-jre/Contents/Home/lib/link", "SYMLINK", "/etc/passwd"),)))
    st = await _run()
    assert st.status == "error" and "Unexpected entry" in st.error
    assert install.installed() is None


@pytest.mark.asyncio
async def test_install_that_does_not_run_is_not_kept(serve):
    serve(_signal_cli_tar(version_exit=1), _jre_tar())
    st = await _run()
    assert st.status == "error" and "didn't run after setup (boom)" in st.error
    assert install.installed() is None and _leftovers() == []


@pytest.mark.asyncio
async def test_failed_download_reports_connection(serve):
    serve(_signal_cli_tar(), _jre_tar())
    install.JRE[platform.machine()] = install.Archive("https://downloads.test/missing", "0" * 64, 1)
    st = await _run()
    assert st.status == "error" and st.error.startswith("Download failed")
    assert install.installed() is None and _leftovers() == []


@pytest.mark.asyncio
async def test_start_while_running_is_one_setup(serve):
    serve(_signal_cli_tar(), _jre_tar())
    first = install.start()
    task = install._task
    assert first.total == install.download_size()  # known before the first byte
    assert install.start() is first and install._task is task
    await task
    assert first.status == "done"
    assert len(serve.requests) == 2


@pytest.mark.asyncio
async def test_cancel_stops_the_download_and_cleans_up(serve, monkeypatch):
    serve(_signal_cli_tar(), _jre_tar())
    real_download = install._download

    async def cancel_during_second(client, archive, dest, st):
        if archive is not install.SIGNAL_CLI:
            install.cancel()  # mid-setup, as the user's "Cancel setup"
        await real_download(client, archive, dest, st)

    monkeypatch.setattr(install, "_download", cancel_during_second)
    st = await _run()
    assert st.status == "cancelled" and st.error is None and not st.running
    assert install.installed() is None and _leftovers() == []
    assert install.cancel() is st and st.status == "cancelled"  # a no-op once ended

    # A new setup after a cancel runs from scratch
    monkeypatch.setattr(install, "_download", real_download)
    again = await _run()
    assert again is not st and again.status == "done" and not again.cancel_requested


def test_unknown_processor_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(install.platform, "machine", lambda: "riscv64")
    with pytest.raises(install.InstallError, match="riscv64"):
        install.download_size()
