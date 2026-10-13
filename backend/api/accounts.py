"""Connected accounts across channels: list them, sign in, sign out, remove.

Sign-in is the connector's step machine (see ``backend.connectors.base``):
POST ``/{key}/auth`` returns the first step (``key`` = an account key to
re-authorize, a single-account channel, or a multi-account channel name to
add a new account); POST ``/{key}/auth/{flow_id}`` answers it with the next
one until ``kind == "done"``.
"""

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Response

from backend.connectors.base import AuthFlowError, Connector
from backend.connectors.manager import manager
from backend.db import crud
from backend.db.database import AsyncSessionLocal

router = APIRouter()


def _connector(key: str) -> Connector:
    c = manager.get(key)
    if c is None:
        raise HTTPException(status_code=404, detail=f"Unknown account or channel: {key}")
    return c


@router.get("")
async def list_accounts():
    """Connected accounts plus the channels that can be connected."""
    await manager.load_accounts()
    async with AsyncSessionLocal() as db:
        rows = await crud.get_accounts(db)
    by_id = {r.id: r for r in rows}
    first_of_channel = {}
    for r in rows:
        first_of_channel.setdefault(r.channel, r)
    accounts = []
    for c in manager.all():
        d = c.describe()
        row = by_id.get(c.account_id) if c.account_id is not None else first_of_channel.get(c.channel)
        d["label"] = row.label if row is not None else None
        accounts.append(d)
    return {"accounts": accounts, "channels": manager.channels()}


@router.post("/{key}/auth")
async def begin_auth(key: str, params: dict[str, Any] = Body(default_factory=dict)):
    step = await manager.begin_auth(key, params)
    if step is None:
        raise HTTPException(status_code=404, detail=f"Unknown account or channel: {key}")
    return step.to_dict()


@router.post("/{key}/auth/{flow_id}")
async def continue_auth(key: str, flow_id: str, data: dict[str, Any] = Body(default_factory=dict)):
    try:
        step = await manager.continue_auth(flow_id, data)
    except AuthFlowError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return step.to_dict()


@router.delete("/{key}/auth/{flow_id}", status_code=204)
async def cancel_auth(key: str, flow_id: str, stop: bool = False) -> Response:
    """The user closed a sign-in: end the flow (Signal's link process, a
    pending OAuth consent). Background work the flow started (Signal's setup
    download) keeps going unless ``stop`` (the step's ``stop_label`` button).
    Idempotent: 204 for unknown or finished flows too."""
    await manager.cancel_auth(flow_id, stop=stop)
    return Response(status_code=204)


@router.post("/{key}/logout")
async def logout(key: str):
    """Sign one account out. The session and its WebSockets stay up (other
    accounts keep syncing); the connector reports the change through
    ``connector_status``."""
    await _connector(key).logout()
    return {"success": True}


@router.delete("/{key}")
async def remove_account(key: str):
    """Disconnect an account of a multi-account channel and delete what was
    synced from it."""
    _connector(key)
    if not await manager.remove(key):
        raise HTTPException(status_code=409, detail="This account can't be removed; log out instead")
    return {"success": True}
