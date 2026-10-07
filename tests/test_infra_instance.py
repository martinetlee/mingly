"""Single-instance lock and port resolution."""

import json
import subprocess
import sys
import textwrap

import pytest

from backend import config


def test_second_process_sees_lock_holder(tmp_path):
    lock = tmp_path / "mingly.lock"
    script = textwrap.dedent(f"""
        import sys, time
        import backend.instance as inst
        from pathlib import Path
        inst.LOCK_FILE = Path({str(lock)!r})
        try:
            inst.acquire_instance_lock(int(sys.argv[1]))
        except inst.InstanceAlreadyRunning as e:
            print("RUNNING", e.port, flush=True)
            sys.exit(3)
        print("LOCKED", flush=True)
        time.sleep(float(sys.argv[2]))
    """)
    first = subprocess.Popen([sys.executable, "-c", script, "9001", "10"], stdout=subprocess.PIPE, text=True)
    try:
        assert first.stdout.readline().strip() == "LOCKED"
        second = subprocess.run([sys.executable, "-c", script, "9002", "0"], capture_output=True, text=True)
        assert second.returncode == 3
        assert second.stdout.strip() == "RUNNING 9001"
    finally:
        first.kill()
        first.wait()
    # Lock is released by the OS when the holder dies.
    third = subprocess.run([sys.executable, "-c", script, "9003", "0"], capture_output=True, text=True)
    assert third.stdout.strip() == "LOCKED"


@pytest.fixture
def frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(config, "_PORT_FILE", tmp_path / "server_port.json")
    return tmp_path


def test_dev_uses_fixed_port(monkeypatch):
    monkeypatch.delenv("MINGLY_PORT", raising=False)
    assert config._resolve_port("127.0.0.1") == 8765


def test_frozen_free_default_port(frozen, monkeypatch):
    monkeypatch.setattr(config, "_port_in_use", lambda h, p: False)
    assert config._resolve_port("127.0.0.1") == 8765
    assert not (frozen / "server_port.json").exists()


def test_frozen_port_held_by_mingly_is_kept(frozen, monkeypatch):
    monkeypatch.setattr(config, "_port_in_use", lambda h, p: True)
    monkeypatch.setattr(config, "_is_mingly", lambda h, p: True)
    assert config._resolve_port("127.0.0.1") == 8765


def test_frozen_port_held_by_other_program_is_persisted(frozen, monkeypatch):
    busy = {8765}
    monkeypatch.setattr(config, "_port_in_use", lambda h, p: p in busy)
    monkeypatch.setattr(config, "_is_mingly", lambda h, p: False)
    port = config._resolve_port("127.0.0.1")
    assert port != 8765
    assert json.loads((frozen / "server_port.json").read_text()) == {"port": port}
    # Next launch reuses it even once 8765 frees up → stable origin.
    busy.clear()
    assert config._resolve_port("127.0.0.1") == port
