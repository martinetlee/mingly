"""Shield detection settings (prefs + vault) and the client they select."""

from typing import Optional

from backend import prefs
from backend.scam.client import ExternalScamAPIClient, LocalPipelineClient
from backend.scam.scan import ScamAPIClient
from backend.secret_prefs import get_secret_pref

SCAM_API_KEY = "scam_detection_api_key"


def get_scam_detection_settings() -> dict:
    """Return scam detection settings from prefs."""
    data = prefs.load_prefs()
    return {
        "mode": data.get("scam_detection_mode", "local_llm"),
        "api_url": data.get("scam_detection_api_url", ""),
        "api_key": get_secret_pref(SCAM_API_KEY),
        "max_tokens": data.get("scam_detection_max_tokens", 200),
    }


def build_scam_client(settings: Optional[dict] = None) -> ScamAPIClient:
    """The detection client the settings select: the external API when
    configured with a URL, else the local DAG pipeline."""
    from backend.scam.pipeline import get_pipeline_manager, init_pipeline_manager

    s = settings if settings is not None else get_scam_detection_settings()
    if s["mode"] == "external" and s["api_url"]:
        return ExternalScamAPIClient(base_url=s["api_url"], api_key=s["api_key"])
    mgr = get_pipeline_manager() or init_pipeline_manager()
    return LocalPipelineClient(mgr.get_config(), max_tokens=s.get("max_tokens", 200))


def reconfigure_scam_service() -> None:
    """Swap the live service's client to match the current settings."""
    from backend.scam.service import get_scam_service

    svc = get_scam_service()
    if svc is not None:
        svc.set_client(build_scam_client())
