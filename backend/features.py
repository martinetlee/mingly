"""Feature switches. Read at call time (``config.SHIELD_ENABLED``) so tests
can flip them.

Shield (scam detection) is off by default: its cloud backend was retired.
When off, nothing scans, syncs or loads models, and the UI hides every
Shield surface (it reads GET /api/features). The code and stored data are
kept so Shield can be re-enabled, e.g. against a new detection endpoint via
backend.scam.scan.ScamAPIClient. Enable with MINGLY_SHIELD=1.
"""

from backend import config


def shield_enabled() -> bool:
    return bool(config.SHIELD_ENABLED)


def features_payload() -> dict:
    return {"shield": shield_enabled()}
