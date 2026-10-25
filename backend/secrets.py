"""
Encrypted secret storage using Fernet + PBKDF2.

Vault file format (DATA_DIR/vault.enc):
  {"salt": "<hex 16 bytes>", "ciphertext": "<fernet token>"}

The Fernet key is derived from the user's master password on each unlock.
The derived key and decrypted secrets dict are cached in memory for the
lifetime of the process (cleared on lock or app exit).

Writes go through ``atomic_write_bytes`` so a crash mid-save can never leave a
truncated vault. ``unlock`` distinguishes a wrong password (returns False)
from an unreadable/corrupt vault file (raises ``VaultCorruptError``) so the UI
can offer Reset instead of "Incorrect password" forever.

Other modules observe lock/unlock through listeners (session tokens, MCP
sessions and vault-backed settings), which keeps this module free of
imports from the API layer.
"""
import base64
import binascii
import json
import logging
import os
from typing import Callable, List, Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from backend.config import DATA_DIR
from backend.fsutil import atomic_write_bytes

logger = logging.getLogger(__name__)

_VAULT_FILE = DATA_DIR / "vault.enc"

# ── In-memory state (never written to disk) ──────────────────────────────────
_fernet: Optional[Fernet] = None
_salt: Optional[bytes] = None
_cache: Optional[dict] = None

_lock_listeners: List[Callable[[], None]] = []
_unlock_listeners: List[Callable[[], None]] = []


class VaultCorruptError(Exception):
    """The vault file exists but cannot be parsed (not a wrong password)."""


# ── Listeners ─────────────────────────────────────────────────────────────────

def add_lock_listener(fn: Callable[[], None]) -> None:
    """Call *fn* whenever the vault is locked or reset (idempotent)."""
    if fn not in _lock_listeners:
        _lock_listeners.append(fn)


def add_unlock_listener(fn: Callable[[], None]) -> None:
    """Call *fn* after every successful unlock or vault initialization (idempotent)."""
    if fn not in _unlock_listeners:
        _unlock_listeners.append(fn)


def _notify(listeners: List[Callable[[], None]]) -> None:
    for fn in list(listeners):
        try:
            fn()
        except Exception:
            logger.exception("Vault listener %r failed", fn)


# ── Key derivation ────────────────────────────────────────────────────────────

def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def _read_vault_file() -> "tuple[bytes, str]":
    """Return (salt, ciphertext) or raise VaultCorruptError.

    Also checks the Fernet token's structure (version byte, minimum length,
    block alignment) so a damaged ciphertext is reported as corruption rather
    than as a wrong password.
    """
    try:
        raw = json.loads(_VAULT_FILE.read_bytes())
        salt = bytes.fromhex(raw["salt"])
        ciphertext = raw["ciphertext"]
        if not isinstance(ciphertext, str) or len(salt) != 16:
            raise ValueError("bad field types")
        token = base64.urlsafe_b64decode(ciphertext.encode())
    except (OSError, ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise VaultCorruptError(f"Vault file is unreadable: {exc}") from exc
    # Fernet: version(1) | timestamp(8) | iv(16) | ciphertext(16n, n>=1) | hmac(32)
    if len(token) < 73 or token[0] != 0x80 or (len(token) - 57) % 16 != 0:
        raise VaultCorruptError("Vault ciphertext is damaged")
    return salt, ciphertext


# ── Vault lifecycle ───────────────────────────────────────────────────────────

def vault_status() -> str:
    """Return 'unset', 'locked', or 'unlocked'."""
    if not _VAULT_FILE.exists():
        return "unset"
    return "unlocked" if _cache is not None else "locked"


def initialize_vault(password: str) -> None:
    """Create a new vault protected by *password*. Leaves vault unlocked."""
    global _fernet, _salt, _cache
    salt = os.urandom(16)
    fernet = Fernet(_derive_key(password, salt))
    _write(fernet, salt, {})
    _fernet, _salt, _cache = fernet, salt, {}
    _notify(_unlock_listeners)


def unlock(password: str) -> bool:
    """Decrypt vault with *password*.

    Returns True on success and False for a wrong password (or no vault).
    Raises VaultCorruptError when the file itself is damaged.
    """
    global _fernet, _salt, _cache
    if not _VAULT_FILE.exists():
        return False
    salt, ciphertext = _read_vault_file()
    f = Fernet(_derive_key(password, salt))
    try:
        plaintext = f.decrypt(ciphertext.encode())
    except InvalidToken:
        return False
    try:
        data = json.loads(plaintext)
        if not isinstance(data, dict):
            raise ValueError("vault payload is not an object")
    except ValueError as exc:
        # The MAC verified, so the password is right but the payload is bad.
        raise VaultCorruptError(f"Vault contents are unreadable: {exc}") from exc
    _fernet, _salt, _cache = f, salt, data
    _notify(_unlock_listeners)
    return True


def lock() -> None:
    """Forget the key and decrypted secrets, then notify lock listeners
    (which revoke session tokens, close WebSockets and MCP sessions)."""
    global _fernet, _salt, _cache
    _fernet = _salt = _cache = None
    _notify(_lock_listeners)


def reset_vault() -> None:
    """Delete the vault file and clear in-memory state (lost data warning)."""
    lock()
    if _VAULT_FILE.exists():
        _VAULT_FILE.unlink()


# ── Secret access (requires unlocked vault) ───────────────────────────────────

def _write(fernet: Fernet, salt: bytes, data: dict) -> None:
    ciphertext = fernet.encrypt(json.dumps(data).encode()).decode()
    payload = json.dumps({"salt": salt.hex(), "ciphertext": ciphertext}).encode()
    atomic_write_bytes(_VAULT_FILE, payload, 0o600)


def _commit(data: dict) -> None:
    """Persist *data* first, then adopt it, so a failed write leaves the
    in-memory cache consistent with the file on disk."""
    global _cache
    if _fernet is None or _cache is None or _salt is None:
        raise RuntimeError("Vault is locked")
    _write(_fernet, _salt, data)
    _cache = data


def save_secret(key: str, value: str) -> None:
    if _cache is None:
        raise RuntimeError("Vault is locked — call unlock() first")
    data = dict(_cache)
    data[key] = value
    _commit(data)


def get_secret(key: str) -> Optional[str]:
    if _cache is None:
        return None
    return _cache.get(key)


def secret_keys(prefix: str) -> list[str]:
    """The stored keys starting with ``prefix`` (none while locked)."""
    if _cache is None:
        return []
    return [k for k in _cache if k.startswith(prefix)]


def delete_secret(key: str) -> None:
    if _cache is None or key not in _cache:
        return
    data = dict(_cache)
    data.pop(key, None)
    _commit(data)
