"""What uploads send: the rows each contract uploads, Telegram-id
resolution for v1, channel-neutral v2 payloads (hashed handles, opaque refs,
per-channel content opt-in) and trace scrubbing."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import select

from backend.db.models import Account, Conversation, Identity, Message, ScamFlag, ScamReport
from backend.prefs import get_pref
from backend.scam.channel_ids import (
    cloud_refs,
    CloudRefs,
    TELEGRAM,
    telegram_message_id,
    telegram_peer_ids,
    telegram_user_ids,
)


CONTENT_UPLOAD_PREF = "shield_cloud_upload_content"


# Message-derived text (flagged text, pipeline input, prompts, traces) per
# channel under v2. Chats keep what v1 uploads for Telegram; email bodies are
# off by default (verdict metadata only). Unknown channels: off.
DEFAULT_CONTENT_UPLOAD = {"telegram": True, "signal": True, "sms": True, "gmail": False}


def content_upload_allowed(channel: Optional[str]) -> bool:
    prefs = get_pref(CONTENT_UPLOAD_PREF, None)
    if isinstance(prefs, dict) and isinstance(prefs.get(channel), bool):
        return prefs[channel]
    return DEFAULT_CONTENT_UPLOAD.get(channel, False)


_TRACE_KEYS = ("status", "pipeline_version", "duration_ms")


_TRACE_STEP_KEYS = ("node_id", "type", "status", "duration_ms", "model_name", "provider_name",
                    "max_tokens", "temperature")


def trace_without_content(trace: dict) -> dict:
    """A pipeline trace reduced to its structure (no inputs, prompts,
    responses or tool calls)."""
    out = {k: trace[k] for k in _TRACE_KEYS if k in trace}
    steps = trace.get("steps")
    if isinstance(steps, list):
        out["steps"] = [{k: s[k] for k in _TRACE_STEP_KEYS if k in s} for s in steps if isinstance(s, dict)]
    return out


def telegram_flag_condition():
    """Flags cloud contract v1 can carry (Telegram senders)."""
    return ScamFlag.identity_id.in_(select(Identity.id).where(Identity.channel == TELEGRAM))


def telegram_message_condition():
    """Messages cloud contract v1 can carry (Telegram conversations)."""
    return Message.conversation_id.in_(
        select(Conversation.id).join(Account, Account.id == Conversation.account_id)
        .where(Account.channel == TELEGRAM)
    )


def flag_upload_conditions(contract: int) -> list:
    """Extra WHERE clauses for the flags ``contract`` uploads (v2: all)."""
    return [] if contract >= 2 else [telegram_flag_condition()]


def message_upload_conditions(contract: int) -> list:
    """Extra WHERE clauses for the scan rows ``contract`` uploads (v2: all)."""
    return [] if contract >= 2 else [telegram_message_condition()]


@dataclass
class TelegramRefs:
    users: dict  # identity_id -> Telegram user id
    peers: dict  # conversation_id -> Telegram peer id
    messages: dict  # message_id -> Telegram message id
    senders: dict = field(default_factory=dict)  # message_id -> Telegram sender id (see channel_ids.telegram_scan_ids)


async def telegram_refs(db, *, identity_ids=(), conversation_ids=(), message_ids=()) -> TelegramRefs:
    """Telegram ids of the given rows; rows of other channels (or deleted
    rows) are simply absent from the maps."""
    mids = [m for m in set(message_ids) if m]
    rows = (await db.execute(
        select(Message.id, Message.conversation_id, Message.seq).where(Message.id.in_(mids))
    )).all() if mids else []
    peers = await telegram_peer_ids(db, set(conversation_ids) | {r.conversation_id for r in rows})
    return TelegramRefs(
        users=await telegram_user_ids(db, identity_ids),
        peers=peers,
        messages={r.id: telegram_message_id(r) for r in rows if r.conversation_id in peers},
    )


_CLOUD_KIND = {"direct": "private"}  # cloud v1 chat types


def parse_trace(trace_json: Optional[str]) -> dict:
    if not trace_json:
        return {}
    try:
        data = json.loads(trace_json)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def extract_pipeline_input(trace: dict) -> Optional[str]:
    if trace.get("input"):
        return trace["input"]
    for step in trace.get("steps", []):
        inp = step.get("input") or {}
        if isinstance(inp, dict) and "$message" in inp:
            return inp["$message"]
    return None


def extract_models_used(trace: dict) -> Optional[list]:
    models = []
    seen = set()
    for step in trace.get("steps", []):
        if step.get("type") != "llm":
            continue
        model_name = step.get("model_name")
        if model_name and model_name not in seen:
            seen.add(model_name)
            models.append({
                "node_id": step.get("node_id"),
                "model_name": model_name,
                "provider": step.get("provider_name"),
                "max_tokens": step.get("max_tokens"),
                "temperature": step.get("temperature"),
            })
    return models or None


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def v1_flag(f: ScamFlag, telegram_id: int) -> dict:
    return {
        "telegram_id": telegram_id,
        "source": f.source,
        "status": f.status,
        "confidence": f.confidence,
        "reason": f.reason,
        "category": f.category or "unknown",
        "flagged_message_text": f.flagged_message_text,
        "flagged_at": iso(f.flagged_at),
        "dismissed_at": iso(f.dismissed_at),
    }


def v2_flag(f: ScamFlag, refs: CloudRefs) -> dict:
    channel = refs.identity_channels[f.identity_id]
    content = content_upload_allowed(channel)
    return {
        "channel": channel,
        "sender": refs.handles[f.identity_id],
        "source": f.source,
        "status": f.status,
        "confidence": f.confidence,
        "reason": f.reason,
        "category": f.category or "unknown",
        "content_included": content,
        "flagged_message_text": f.flagged_message_text if content else None,
        "flagged_at": iso(f.flagged_at),
        "dismissed_at": iso(f.dismissed_at),
    }


def scan_verdict(m: Message, trace: dict) -> dict:
    return {
        "scam_score": m.scam_score,
        "scam_reason": m.scam_reason,
        "scam_category": m.scam_category,
        "scan_status": m.scam_status or "unknown",
        "scan_error": m.scam_error,
        "manual_verdict": m.scam_manual_verdict,
        "effective_is_scam": bool(m.effective_is_scam),
        "date": iso(m.date),
        "updated_at": iso(m.scam_updated_at),
        "pipeline_version": trace.get("pipeline_version"),
    }


def v1_scan(m: Message, refs: TelegramRefs) -> dict:
    trace = parse_trace(m.scam_trace_json)
    return {
        "chat_id": refs.peers.get(m.conversation_id),
        "tg_message_id": telegram_message_id(m),
        "sender_id": refs.senders[m.id] if m.id in refs.senders else refs.users.get(m.sender_identity_id),
        **scan_verdict(m, trace),
        "pipeline_input": extract_pipeline_input(trace),
        "models_used": extract_models_used(trace),
    }


def v2_scan(m: Message, refs: CloudRefs) -> dict:
    trace = parse_trace(m.scam_trace_json)
    channel = refs.conversation_channels.get(m.conversation_id)
    content = content_upload_allowed(channel)
    return {
        "channel": channel,
        "conversation": refs.conversations.get(m.conversation_id),
        "message": refs.messages.get(m.id),
        "sender": refs.handles.get(m.sender_identity_id),
        **scan_verdict(m, trace),
        "models_used": extract_models_used(trace),
        "content_included": content,
        "pipeline_input": extract_pipeline_input(trace) if content else None,
    }


def cloud_v1(data: dict) -> dict:
    """The Telegram ids snapshotted when the dismissal was recorded (see
    ``verdict._add_cloud_v1_ids``); {} for other channels and older rows."""
    v1 = data.get("cloud_v1")
    return v1 if isinstance(v1, dict) else {}


async def existing_rows(db, *, identity_ids=(), conversation_ids=(), message_ids=()) -> tuple[set, set]:
    """(identity ids, conversation ids) among the given ones that still exist."""
    iids = [i for i in set(identity_ids) if i]
    cids = [c for c in set(conversation_ids) if c]
    identities = set((await db.execute(select(Identity.id).where(Identity.id.in_(iids)))).scalars()) if iids else set()
    conversations = set(
        (await db.execute(select(Conversation.id).where(Conversation.id.in_(cids)))).scalars()
    ) if cids else set()
    return identities, conversations


def v1_dismissal(data: dict, refs: TelegramRefs) -> dict:
    telegram_id = refs.users.get(data.get("identity_id"), cloud_v1(data).get("telegram_id"))
    return {**{k: v for k, v in data.items() if k not in ("identity_id", "cloud_v1")}, "telegram_id": telegram_id}


def v2_dismissal(data: dict, refs: CloudRefs) -> dict:
    iid = data.get("identity_id")
    if iid in refs.handles:
        channel, sender = refs.identity_channels[iid], refs.handles[iid]
    else:  # sender deleted since: its Telegram snapshot
        channel, sender = TELEGRAM, {"id": str(cloud_v1(data)["telegram_id"])}
    content = content_upload_allowed(channel)
    return {
        "channel": channel,
        "sender": sender,
        "source": data.get("source"),
        "confidence": data.get("confidence"),
        "reason": data.get("reason"),
        "category": data.get("category"),
        "content_included": content,
        "flagged_message_text": data.get("flagged_message_text") if content else None,
        "flagged_at": data.get("flagged_at"),
    }


def dismissal_trace(data: dict) -> dict:
    trace = data.get("trace") or {}
    return trace if isinstance(trace, dict) else {}


def v1_dismissed_scan(data: dict, refs: TelegramRefs) -> dict:
    trace = data.get("trace") or {}
    v1 = cloud_v1(data)
    return {
        "chat_id": refs.peers.get(data.get("conversation_id"), v1.get("chat_id")),
        "tg_message_id": refs.messages.get(data.get("message_id"), v1.get("tg_message_id")),
        "sender_id": refs.users.get(data.get("identity_id"), v1.get("sender_id")),
        "scam_score": data.get("scam_score"),
        "scam_reason": data.get("scam_reason"),
        "scam_category": data.get("scam_category"),
        "scan_status": "dismissed",
        "dismissed": True,
        "pipeline_version": trace.get("pipeline_version") if isinstance(trace, dict) else None,
        "pipeline_input": extract_pipeline_input(trace) if isinstance(trace, dict) else None,
        "models_used": extract_models_used(trace) if isinstance(trace, dict) else None,
        "llm_prompt": data.get("llm_prompt"),
        "llm_response": data.get("llm_response"),
        "trace": trace or None,
    }


def v2_dismissed_scan(data: dict, refs: CloudRefs) -> dict:
    trace = dismissal_trace(data)
    cid = data.get("conversation_id")
    channel = refs.conversation_channels[cid]
    content = content_upload_allowed(channel)
    v1 = cloud_v1(data) if channel == TELEGRAM else {}
    message = refs.messages.get(data.get("message_id"))
    if message is None and v1.get("tg_message_id") is not None:  # deleted since
        message = {"id": str(v1["tg_message_id"])}
    sender = refs.handles.get(data.get("identity_id"))
    if sender is None and v1.get("sender_id") is not None:
        sender = {"id": str(v1["sender_id"])}
    return {
        "channel": channel,
        "conversation": refs.conversations[cid],
        "message": message,
        "sender": sender,
        "scam_score": data.get("scam_score"),
        "scam_reason": data.get("scam_reason"),
        "scam_category": data.get("scam_category"),
        "scan_status": "dismissed",
        "dismissed": True,
        "pipeline_version": trace.get("pipeline_version"),
        "models_used": extract_models_used(trace),
        "content_included": content,
        "pipeline_input": extract_pipeline_input(trace) if content else None,
        "llm_prompt": data.get("llm_prompt") if content else None,
        "llm_response": data.get("llm_response") if content else None,
        "trace": (trace if content else trace_without_content(trace)) or None,
    }


def v2_period(period: dict) -> dict:
    """An analytics period with v2 conversation kinds ("private" → "direct")."""
    kinds = dict(period.get("chat_types") or {})
    if "private" in kinds:
        kinds["direct"] = kinds.pop("private")
    return {**period, "chat_types": kinds}


async def report_payloads(db, reports: list, salt: Optional[bytes] = None) -> list[tuple]:
    """[(report, cloud payload)] for the reports ``reports`` the contract can
    carry: v1 (no salt) the Telegram ones, v2 every report whose channel is
    still known. Stored reports carry internal ids (``messages_json`` entries
    an ``identity_id``, local detection scores a ``message_id``); the payload
    gets the contract's ids."""
    parsed = {r.id: (json_list(r.messages_json), json_obj(r.local_detection_json)) for r in reports}
    identity_ids = {r.reported_identity_id for r in reports}
    message_ids: set = set()
    for msgs, detection in parsed.values():
        identity_ids.update(m.get("identity_id") for m in msgs if isinstance(m, dict))
        message_ids.update(s.get("message_id") for s in (detection or {}).get("scores") or [] if isinstance(s, dict))
    if salt is not None:
        refs = await cloud_refs(
            db, salt, identity_ids=identity_ids, conversation_ids=[r.reported_in_conversation_id for r in reports],
            message_ids=message_ids,
        )
        return [(r, p) for r in reports if (p := v2_report(r, parsed[r.id], refs)) is not None]
    tg = await telegram_refs(
        db, identity_ids=identity_ids, conversation_ids=[r.reported_in_conversation_id for r in reports],
        message_ids=message_ids,
    )

    out = []
    for r in reports:
        if r.reported_in_conversation_id is not None:
            is_telegram = r.reported_in_conversation_id in tg.peers
        else:
            is_telegram = r.reported_identity_id in tg.users
        if not is_telegram:
            continue
        msgs, detection = parsed[r.id]
        cloud_msgs = []
        for m in msgs:
            if isinstance(m, dict):
                m = {**{k: v for k, v in m.items() if k != "identity_id"},
                     "telegram_id": tg.users.get(m.get("identity_id"))}
            cloud_msgs.append(m)
        if detection is not None:
            detection = {**detection, "scores": [
                {**{k: v for k, v in sc.items() if k != "message_id"},
                 "tg_message_id": tg.messages.get(sc.get("message_id"))}
                for sc in detection.get("scores") or [] if isinstance(sc, dict)
            ]}
        out.append((r, {
            "report_id": r.report_id,
            "reported_telegram_id": tg.users.get(r.reported_identity_id),
            "reported_in_chat_id": tg.peers.get(r.reported_in_conversation_id),
            "chat_type": _CLOUD_KIND.get(r.chat_type, r.chat_type),
            "reporter_verdict": r.reporter_verdict,
            "scam_category": r.scam_category,
            "custom_category": r.custom_category,
            "messages_json": json.dumps(cloud_msgs, default=str),
            "local_detection_json": json.dumps(detection, default=str) if detection is not None else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }))
    return out


def v2_report(r: ScamReport, parsed: tuple, refs: CloudRefs) -> Optional[dict]:
    """A user-submitted report: the messages the user chose to include are
    sent whatever the content preference (submitting is the consent)."""
    channel = refs.channel_of(conversation_id=r.reported_in_conversation_id, identity_id=r.reported_identity_id)
    if channel is None:
        return None
    msgs, detection = parsed
    out_msgs = [
        {**{k: v for k, v in m.items() if k != "identity_id"}, "sender": refs.handles.get(m.get("identity_id"))}
        if isinstance(m, dict) else m
        for m in msgs
    ]
    if detection is not None:
        detection = {**detection, "scores": [
            {**{k: v for k, v in sc.items() if k != "message_id"}, "message": refs.messages.get(sc.get("message_id"))}
            for sc in detection.get("scores") or [] if isinstance(sc, dict)
        ]}
    return {
        "report_id": r.report_id,
        "channel": channel,
        "reported": refs.handles.get(r.reported_identity_id),
        "conversation": refs.conversations.get(r.reported_in_conversation_id),
        "reporter_verdict": r.reporter_verdict,
        "scam_category": r.scam_category,
        "custom_category": r.custom_category,
        "messages": out_msgs,
        "local_detection": detection,
        "created_at": iso(r.created_at),
    }


def json_list(raw: Optional[str]) -> list:
    try:
        data = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def json_obj(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None
