"""API keys that the user enters in Settings (Shield, external scam API).

They live in the vault. Older versions kept them in plaintext ``prefs.json``;
a legacy plaintext value is moved into the vault on first access while the
vault is unlocked (and on every unlock).
"""

import logging

from backend import prefs
from backend import secrets as vault

logger = logging.getLogger(__name__)

SECRET_PREF_KEYS = ("scam_detection_api_key", "shield_api_key")


def migrate_plaintext_secrets() -> "list[str]":
    """Move legacy plaintext API keys from prefs.json into the vault.

    No-op while the vault is locked. A value already in the vault wins (new
    code never writes these keys to prefs). Returns the keys removed from prefs.
    """
    if vault.vault_status() != "unlocked":
        return []
    data = prefs.load_prefs()
    moved = [k for k in SECRET_PREF_KEYS if k in data]
    if not moved:
        return []
    for key in moved:
        value = data.pop(key)
        if value and not vault.get_secret(key):
            vault.save_secret(key, str(value))
    prefs.save_prefs(data)
    logger.info("Moved %s from prefs.json into the vault", ", ".join(moved))
    return moved


def get_secret_pref(key: str) -> str:
    """Return an API key from the vault, falling back to a not-yet-migrated
    plaintext value (e.g. at startup, before the first unlock)."""
    value = vault.get_secret(key)
    if value:
        return value
    legacy = prefs.get_pref(key) or ""
    if legacy and vault.vault_status() == "unlocked":
        migrate_plaintext_secrets()
    return legacy


def clear_secret_pref(key: str) -> None:
    vault.delete_secret(key)
    data = prefs.load_prefs()
    if data.pop(key, None) is not None:
        prefs.save_prefs(data)


def get_shield_api_key() -> str:
    return get_secret_pref("shield_api_key")
