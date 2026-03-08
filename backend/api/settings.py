import json
import os

from fastapi import APIRouter
from pydantic import BaseModel

from backend.config import DATA_DIR

router = APIRouter()

_PREFS_FILE = DATA_DIR / "prefs.json"


def _load_prefs() -> dict:
    if _PREFS_FILE.exists():
        try:
            return json.loads(_PREFS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_prefs(data: dict) -> None:
    _PREFS_FILE.write_text(json.dumps(data))
    os.chmod(_PREFS_FILE, 0o600)


def get_notif_pref() -> str:
    """Returns 'all', 'tracked', or 'off'."""
    return _load_prefs().get("notif_pref", "tracked")


class NotifPrefRequest(BaseModel):
    pref: str  # 'all' | 'tracked' | 'off'


@router.get("/prefs")
async def get_prefs():
    return _load_prefs()


@router.post("/prefs/notifications")
async def set_notif_pref(req: NotifPrefRequest):
    if req.pref not in ("all", "tracked", "off"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid preference")
    prefs = _load_prefs()
    prefs["notif_pref"] = req.pref
    _save_prefs(prefs)
    return {"success": True, "pref": req.pref}
