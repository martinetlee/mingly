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


class BriefingScheduleRequest(BaseModel):
    schedule: str  # 'daily' | 'weekly'


class BriefingTimeRequest(BaseModel):
    time: str  # "HH:MM" in 24h format, e.g. "09:00"


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


@router.post("/prefs/briefing-schedule")
async def set_briefing_schedule(req: BriefingScheduleRequest):
    if req.schedule not in ("daily", "weekly"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid schedule")
    prefs = _load_prefs()
    prefs["briefing_schedule"] = req.schedule
    _save_prefs(prefs)
    return {"success": True, "schedule": req.schedule}


@router.post("/prefs/briefing-time")
async def set_briefing_time(req: BriefingTimeRequest):
    """Set the hour/minute at which the briefing auto-runs (local time, 24h HH:MM)."""
    import re
    if not re.match(r"^\d{2}:\d{2}$", req.time):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid time format. Use HH:MM.")
    prefs = _load_prefs()
    prefs["briefing_time"] = req.time
    _save_prefs(prefs)
    return {"success": True, "time": req.time}


@router.post("/prefs/briefing-last-run")
async def set_briefing_last_run():
    """Mark that the briefing was just run (stores current UTC ISO timestamp)."""
    from datetime import datetime
    prefs = _load_prefs()
    prefs["briefing_last_run"] = datetime.utcnow().isoformat()
    _save_prefs(prefs)
    return {"success": True, "briefing_last_run": prefs["briefing_last_run"]}
