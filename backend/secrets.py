"""
Encrypted secret storage using Fernet + PBKDF2.

Vault file format (DATA_DIR/vault.enc):
  {"salt": "<hex 16 bytes>", "ciphertext": "<fernet token>"}

The Fernet key is derived from the user's master password on each unlock.
The derived key and decrypted secrets dict are cached in memory for the
lifetime of the process (cleared on lock or app exit).
"""
import base64
import json
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from backend.config import DATA_DIR

_VAULT_FILE = DATA_DIR / "vault.enc"

# ── In-memory state (never written to disk) ──────────────────────────────────
_fernet: Optional[Fernet] = None
_salt: Optional[bytes] = None
_cache: Optional[dict] = None


# ── Key derivation ────────────────────────────────────────────────────────────

def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


# ── Vault lifecycle ───────────────────────────────────────────────────────────

def vault_status() -> str:
    """Return 'unset', 'locked', or 'unlocked'."""
    if not _VAULT_FILE.exists():
        return "unset"
    return "unlocked" if _cache is not None else "locked"


def initialize_vault(password: str) -> None:
    """Create a new vault protected by *password*. Leaves vault unlocked."""
    global _fernet, _salt, _cache
    _salt = os.urandom(16)
    _fernet = Fernet(_derive_key(password, _salt))
    _cache = {}
    _flush()


def unlock(password: str) -> bool:
    """Decrypt vault with *password*. Returns True on success."""
    global _fernet, _salt, _cache
    if not _VAULT_FILE.exists():
        return False
    try:
        raw = json.loads(_VAULT_FILE.read_text())
        salt = bytes.fromhex(raw["salt"])
        key = _derive_key(password, salt)
        f = Fernet(key)
        plaintext = f.decrypt(raw["ciphertext"].encode())
        _fernet = f
        _salt = salt
        _cache = json.loads(plaintext)
        return True
    except (InvalidToken, Exception):
        return False


def lock() -> None:
    global _fernet, _salt, _cache
    _fernet = _salt = _cache = None


def reset_vault() -> None:
    """Delete the vault file and clear in-memory state (lost data warning)."""
    lock()
    if _VAULT_FILE.exists():
        _VAULT_FILE.unlink()


# ── Secret access (requires unlocked vault) ───────────────────────────────────

def _flush() -> None:
    if _fernet is None or _cache is None or _salt is None:
        raise RuntimeError("Vault is locked")
    ciphertext = _fernet.encrypt(json.dumps(_cache).encode()).decode()
    _VAULT_FILE.write_text(json.dumps({"salt": _salt.hex(), "ciphertext": ciphertext}))
    os.chmod(_VAULT_FILE, 0o600)


def save_secret(key: str, value: str) -> None:
    if _cache is None:
        raise RuntimeError("Vault is locked — call unlock() first")
    _cache[key] = value
    _flush()


def get_secret(key: str) -> Optional[str]:
    if _cache is None:
        return None
    return _cache.get(key)


def delete_secret(key: str) -> None:
    if _cache is None:
        return
    _cache.pop(key, None)
    _flush()
