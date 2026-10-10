"""What the cloud may install: allowlisted manifest names, endpoint
shapes, artifact verification and the atomic model-bundle swap. Paths are
always resolved inside the data directory the caller passes."""

import hashlib
import io
import json
import re
import shutil
import uuid
import zipfile
from pathlib import Path

from backend.fsutil import atomic_write_text


MAX_BUNDLE_BYTES = 300 * 1024 * 1024


# Files the cloud may install into DATA_DIR. App-owned files (prefs.json,
# llm_config.json, vault...) can never match.
JSON_NAME_RE = re.compile(
    r"^(?:scam_[a-z0-9_]+|scenarios|safe_patterns|[a-z0-9_]+_(?:library|patterns|scenarios))\.json$"
)


_DOMAIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


BUNDLE_NAME_RE = re.compile(r"^ml_models/([A-Za-z0-9][A-Za-z0-9_\-]{0,63})\.zip$")


_BUNDLE_MEMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")


def contained(path: Path, base: Path) -> Path:
    """``path`` resolved, refused when it escapes ``base`` (the data directory)."""
    resolved = path.resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise ValueError(f"Path escapes the data directory: {path}")
    return resolved


def valid_endpoint(endpoint: str) -> bool:
    return bool(endpoint) and endpoint.startswith("/") and not endpoint.startswith("//") \
        and "://" not in endpoint and not any(c.isspace() for c in endpoint)


def verify_artifact(data: bytes, file_info: dict, name: str) -> None:
    """The single gate every downloaded file passes before it is installed
    (and, for model bundles, before anything could unpickle it).

    Today: the manifest's sha256, when given (transport is https-only).
    Bundle *signatures* need the cloud to sign them; when it does, verify the
    manifest/bundle signature here and nowhere else.
    """
    expected = file_info.get("sha256")
    if expected:
        actual = hashlib.sha256(data).hexdigest()
        if actual.lower() != str(expected).strip().lower():
            raise ValueError(f"sha256 mismatch for {name}: expected {expected}, got {actual}")


def install_bundle(content: bytes, domain: str, version: str, data_dir: Path) -> None:
    """Extract a model bundle into a temp dir next to the live one under
    ``data_dir/ml_models``, validate it, then swap it in by rename (blocking;
    run in a thread)."""
    if not _DOMAIN_RE.match(domain):
        raise ValueError(f"invalid model domain {domain!r}")
    models_dir = data_dir / "ml_models"
    models_dir.mkdir(parents=True, exist_ok=True)
    target = contained(models_dir / domain, data_dir)
    tmp = contained(models_dir / f".tmp-{domain}-{uuid.uuid4().hex}", data_dir)
    old = models_dir / f".old-{domain}-{uuid.uuid4().hex}"
    tmp.mkdir()
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                if not _BUNDLE_MEMBER_RE.match(name):
                    raise ValueError(f"unexpected file in bundle: {name!r}")
                dest = contained(tmp / name, data_dir)
                with zf.open(info) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        if not (tmp / "lgbm.joblib").exists():
            raise ValueError("bundle is missing lgbm.joblib")

        thresholds_file = tmp / "thresholds.json"
        thresholds = {}
        if thresholds_file.exists():
            thresholds = json.loads(thresholds_file.read_text(encoding="utf-8"))
            if not isinstance(thresholds, dict):
                raise ValueError("thresholds.json is not an object")
        thresholds["version"] = version
        atomic_write_text(thresholds_file, json.dumps(thresholds, indent=2), 0o644)

        if target.exists():
            target.rename(old)
        tmp.rename(target)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        if old.exists() and not target.exists():
            old.rename(target)
        raise
    shutil.rmtree(old, ignore_errors=True)
