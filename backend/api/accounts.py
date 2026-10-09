"""Connected accounts across channels: list them, sign in, sign out.

Sign-in is the connector's step machine (see ``backend.connectors.base``):
POST ``/{key}/auth`` returns the first step, POST ``/{key}/auth/{flow_id}``
answers it with the next one until ``kind == "done"``.
"""

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from backend.connectors.base import AuthFlowError, Connector
from backend.connectors.manager import manager
from backend.ws.manager import ws_manager

router = APIRouter()


def _connector(key: str) -> Connector:
    c = manager.get(key)
    if c is None:
        raise HTTPException(status_code=404, detail=f"Unknown account or channel: {key}")
    return c


@router.get("")
async def list_accounts():
    return [c.describe() for c in manager.all()]


@router.post("/{key}/auth")
async def begin_auth(key: str, params: dict[str, Any] = Body(default_factory=dict)):
    step = await _connector(key).begin_auth(params)
    return step.to_dict()


@router.post("/{key}/auth/{flow_id}")
async def continue_auth(key: str, flow_id: str, data: dict[str, Any] = Body(default_factory=dict)):
    try:
        step = await _connector(key).continue_auth(flow_id, data)
    except AuthFlowError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return step.to_dict()


@router.post("/{key}/logout")
async def logout(key: str):
    await _connector(key).logout()
    # The vault stays unlocked; drop the sockets carrying the old account's
    # events (same as the legacy /api/auth/logout).
    await ws_manager.close_all(reason=f"{key} logged out")
    return {"success": True}
