"""Vault atomicity / corruption handling and the prefs cache."""

import json
import os
import stat

import pytest

from backend import prefs as prefs_mod
from backend import secrets as vault
from backend.api import dependencies as deps


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_VAULT_FILE", tmp_path / "vault.enc")
    monkeypatch.setattr(prefs_mod, "PREFS_FILE", tmp_path / "prefs.json")
    prefs_mod.invalidate_cache()
    vault.lock()
    yield tmp_path
    vault.lock()
    prefs_mod.invalidate_cache()
    deps.clear_session_token()


# ── Vault ────────────────────────────────────────────────────────────────────

def test_vault_write_is_atomic_and_private(isolated_files):
    vault.initialize_vault("pw-123456")
    vault.save_secret("k", "v")
    path = isolated_files / "vault.enc"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not [p for p in os.listdir(isolated_files) if ".tmp" in p]
    vault.lock()
    assert vault.unlock("pw-123456") is True
    assert vault.get_secret("k") == "v"


def test_wrong_password_returns_false(isolated_files):
    vault.initialize_vault("right-pass")
    vault.lock()
    assert vault.unlock("wrong-pass") is False
    assert vault.vault_status() == "locked"


@pytest.mark.parametrize("content", [
    b'{"salt": "00',  # truncated JSON (crash mid-write before atomic writes)
    b"",
    b'{"salt": "zz", "ciphertext": "x"}',
    b'["not", "an", "object"]',
])
def test_corrupt_vault_raises_distinct_error(isolated_files, content):
    (isolated_files / "vault.enc").write_bytes(content)
    with pytest.raises(vault.VaultCorruptError):
        vault.unlock("anything")


def test_damaged_ciphertext_is_corrupt_not_wrong_password(isolated_files):
    vault.initialize_vault("pw-123456")
    vault.lock()
    path = isolated_files / "vault.enc"
    raw = json.loads(path.read_text())
    raw["ciphertext"] = raw["ciphertext"][:-7]  # truncated token
    path.write_text(json.dumps(raw))
    with pytest.raises(vault.VaultCorruptError):
        vault.unlock("pw-123456")


def test_failed_write_keeps_cache_consistent(isolated_files, monkeypatch):
    vault.initialize_vault("pw-123456")
    vault.save_secret("a", "1")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(vault, "atomic_write_bytes", boom)
    with pytest.raises(OSError):
        vault.save_secret("b", "2")
    assert vault.get_secret("b") is None
    assert vault.get_secret("a") == "1"


def test_lock_revokes_all_sessions(isolated_files):
    vault.initialize_vault("pw-123456")
    t1 = deps.generate_session_token()
    t2 = deps.generate_session_token()
    vault.lock()
    assert not deps.is_valid_session_token(t1)
    assert not deps.is_valid_session_token(t2)


def test_unlock_listener_runs(isolated_files):
    calls = []
    fn = lambda: calls.append(1)  # noqa: E731
    vault.add_unlock_listener(fn)
    try:
        vault.initialize_vault("pw-123456")
        vault.lock()
        vault.unlock("pw-123456")
        assert len(calls) == 2
    finally:
        vault._unlock_listeners.remove(fn)


# ── Prefs ────────────────────────────────────────────────────────────────────

def test_prefs_cached_and_invalidated_on_save(isolated_files, monkeypatch):
    prefs_mod.save_prefs({"a": 1})
    reads = []
    real_read_text = type(prefs_mod.PREFS_FILE).read_text

    def counting_read_text(self, *a, **k):
        reads.append(1)
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(type(prefs_mod.PREFS_FILE), "read_text", counting_read_text)
    for _ in range(5):
        assert prefs_mod.get_pref("a") == 1
    assert reads == []  # served from cache populated by save
    prefs_mod.save_prefs({"a": 2})
    assert prefs_mod.load_prefs() == {"a": 2}


def test_prefs_picks_up_external_edit(isolated_files):
    prefs_mod.save_prefs({"a": 1})
    path = isolated_files / "prefs.json"
    path.write_text(json.dumps({"a": 1, "b": 22}))
    os.utime(path, ns=(1, 1))  # force a different mtime
    assert prefs_mod.load_prefs() == {"a": 1, "b": 22}


def test_load_returns_private_copy(isolated_files):
    prefs_mod.save_prefs({"orgs": ["x"]})
    p = prefs_mod.load_prefs()
    p["orgs"].append("y")
    assert prefs_mod.load_prefs() == {"orgs": ["x"]}


def test_corrupt_prefs_backed_up_before_overwrite(isolated_files):
    path = isolated_files / "prefs.json"
    path.write_text('{"notif_pref": "all", "user_organizations": ["Acme"')  # truncated
    assert prefs_mod.load_prefs() == {}
    backups = [p for p in os.listdir(isolated_files) if p.startswith("prefs.json.corrupt-")]
    assert len(backups) == 1
    assert "Acme" in (isolated_files / backups[0]).read_text()
    prefs_mod.save_prefs({"notif_pref": "off"})
    assert "Acme" in (isolated_files / backups[0]).read_text()
    assert json.loads(path.read_text()) == {"notif_pref": "off"}


def test_update_prefs(isolated_files):
    prefs_mod.save_prefs({"n": 1})
    prefs_mod.update_prefs(lambda d: d.__setitem__("n", d["n"] + 1))
    assert prefs_mod.get_pref("n") == 2
