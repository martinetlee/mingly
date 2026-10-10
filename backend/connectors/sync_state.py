"""Per-account sync bookkeeping in ``accounts.sync_state_json`` (cursors,
tokens, failure lists). Channel-neutral: a connector's loops each own
top-level keys and merge them without overwriting each other's."""

import json
import re
from typing import Any

from sqlalchemy import case, func, update

from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Account


async def load_sync_state(account_id: int) -> dict:
    """``accounts.sync_state_json`` of an account (e.g. Gmail: the mail keys
    (``history_id``, ``last_sync_at``) and the ``calendar`` key)."""
    async with AsyncSessionLocal() as db:
        acc = await crud.get_account(db, account_id)
    try:
        state = json.loads(acc.sync_state_json) if acc is not None and acc.sync_state_json else {}
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


async def merge_sync_state(account_id: int, **values: Any) -> None:
    """Set top-level keys of ``accounts.sync_state_json`` in one UPDATE, so
    the loops of an account (e.g. Gmail's mail and calendar) never overwrite
    each other's keys."""
    if not values:
        return
    args: list = []
    for key, value in values.items():
        if not re.fullmatch(r"[a-z_]+", key):
            raise ValueError(f"Invalid sync state key: {key}")
        args += [f"$.{key}", func.json(json.dumps(value))]
    current = case((func.json_valid(Account.sync_state_json), Account.sync_state_json), else_="{}")
    async with AsyncSessionLocal() as db:
        await db.execute(update(Account).where(Account.id == account_id)
                         .values(sync_state_json=func.json_set(current, *args)))
        await db.commit()
