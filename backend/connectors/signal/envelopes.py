"""Translate signal-cli's JSON envelopes into channel-neutral events.

Pure functions (no I/O) so the mapping is easy to test. Signal identifies a
message by its author and the author's send timestamp, so a message's
``external_id`` is ``"<author ACI>:<timestamp>"`` (the number stands in when
signal-cli doesn't know the ACI) and ``seq`` is the timestamp. A direct
conversation's ``external_id`` is the peer's ACI (or number); a group's is
its base64 group id.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Union

from backend.ingest.normalized import NormalizedAttachment


@dataclass
class Address:
    aci: Optional[str] = None
    number: Optional[str] = None
    name: Optional[str] = None  # profile / contact name signal-cli attached

    @property
    def key(self) -> Optional[str]:
        return self.aci or self.number


@dataclass
class ConvRef:
    kind: str  # direct | group
    external_id: str
    peer: Optional[Address] = None  # direct only
    group_name: Optional[str] = None  # group only, when the envelope names it


@dataclass
class MessageEvent:
    conv: ConvRef
    external_id: str
    seq: int
    date: datetime
    sender: Address
    is_outgoing: bool
    text: Optional[str]
    attachments: list[NormalizedAttachment] = field(default_factory=list)
    reply_to_external_id: Optional[str] = None
    is_edit: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeleteEvent:
    conv: ConvRef
    target_external_id: str


@dataclass
class ReactionEvent:
    conv: ConvRef
    target_external_id: str
    reactor: Address
    emoji: Optional[str]
    remove: bool


@dataclass
class ReceiptEvent:
    state: str  # delivered | read
    timestamps: list[int]


Event = Union[MessageEvent, DeleteEvent, ReactionEvent, ReceiptEvent]


def message_key(author_key: str, timestamp: int) -> str:
    return f"{author_key}:{timestamp}"


def split_message_key(external_id: str) -> tuple[str, int]:
    """``(author key, timestamp)`` of a Signal message external id."""
    author, _, ts = external_id.rpartition(":")
    return author, int(ts)


def utc(ms: int) -> datetime:
    return datetime.utcfromtimestamp(ms / 1000)


def attachment_kind(mime: Optional[str], voice_note: bool = False) -> str:
    mime = (mime or "").lower()
    if mime == "image/gif":
        return "gif"
    if mime.startswith("image/"):
        return "photo"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "voice" if voice_note else "audio"
    return "document"


def extension_for(filename: Optional[str], mime: Optional[str]) -> str:
    if filename and "." in filename:
        return "." + filename.rsplit(".", 1)[1].lower()
    guessed = mimetypes.guess_extension(mime or "") if mime else None
    return guessed or ""


def _attachments(raw: Any) -> list[NormalizedAttachment]:
    out = []
    for a in raw or []:
        if not isinstance(a, dict):
            continue
        out.append(NormalizedAttachment(
            kind=attachment_kind(a.get("contentType"), bool(a.get("voiceNote"))),
            filename=a.get("filename"),
            mime_type=a.get("contentType"),
            size=a.get("size"),
            external_ref=a.get("id"),
        ))
    return out


def _with_mentions(text: Optional[str], mentions: Any) -> Optional[str]:
    """Signal puts U+FFFC where a mention goes; spell the name out."""
    if not text or not mentions:
        return text
    for m in sorted((m for m in mentions if isinstance(m, dict)), key=lambda m: m.get("start", 0), reverse=True):
        start, length = m.get("start"), m.get("length", 1)
        if not isinstance(start, int) or start < 0 or start >= len(text):
            continue
        name = m.get("name") or m.get("number") or m.get("uuid") or "someone"
        text = text[:start] + f"@{name}" + text[start + length:]
    return text


def _group_id(dm: dict) -> tuple[Optional[str], Optional[str]]:
    for key in ("groupInfo", "groupV2"):
        info = dm.get(key)
        if isinstance(info, dict) and (info.get("groupId") or info.get("id")):
            return str(info.get("groupId") or info.get("id")), info.get("groupName") or info.get("name")
    return None, None


def _conv(dm: dict, direct_peer: Address) -> Optional[ConvRef]:
    gid, gname = _group_id(dm)
    if gid:
        return ConvRef("group", gid, group_name=gname)
    if direct_peer.key is None:
        return None
    return ConvRef("direct", direct_peer.key, peer=direct_peer)


def _quote_target(dm: dict) -> Optional[str]:
    q = dm.get("quote")
    if not isinstance(q, dict) or q.get("id") is None:
        return None
    author = q.get("authorUuid") or q.get("author") or q.get("authorNumber")
    return message_key(author, int(q["id"])) if author else None


def _data_events(
    dm: dict, conv: ConvRef, author: Address, timestamp: int, outgoing: bool, edit_target: Optional[int] = None,
) -> list[Event]:
    if author.key is None:
        return []
    if isinstance(dm.get("remoteDelete"), dict) and dm["remoteDelete"].get("timestamp") is not None:
        return [DeleteEvent(conv, message_key(author.key, int(dm["remoteDelete"]["timestamp"])))]
    reaction = dm.get("reaction")
    if isinstance(reaction, dict) and reaction.get("targetSentTimestamp") is not None:
        target_author = reaction.get("targetAuthorUuid") or reaction.get("targetAuthor") or reaction.get("targetAuthorNumber")
        if not target_author:
            return []
        return [ReactionEvent(
            conv, message_key(target_author, int(reaction["targetSentTimestamp"])), author,
            reaction.get("emoji"), bool(reaction.get("isRemove")),
        )]

    text = _with_mentions(dm.get("message"), dm.get("mentions"))
    attachments = _attachments(dm.get("attachments"))
    meta: dict[str, Any] = {}
    sticker = dm.get("sticker")
    if isinstance(sticker, dict):
        meta["sticker"] = {k: sticker.get(k) for k in ("packId", "stickerId")}
        attachments = attachments or [NormalizedAttachment(kind="sticker")]
    if dm.get("expiresInSeconds"):
        meta["expires_in_seconds"] = dm["expiresInSeconds"]
    if dm.get("viewOnce"):
        meta["view_once"] = True
    if not text and not attachments:
        return []  # group updates, profile key updates, expiry timer changes, …
    target = edit_target if edit_target is not None else timestamp
    return [MessageEvent(
        conv=conv,
        external_id=message_key(author.key, target),
        seq=target,
        date=utc(target),
        sender=author,
        is_outgoing=outgoing,
        text=text,
        attachments=attachments,
        reply_to_external_id=_quote_target(dm),
        is_edit=edit_target is not None,
        meta=meta,
    )]


def parse_envelope(envelope: dict, self_addr: Address) -> list[Event]:
    """Events carried by one ``receive`` envelope."""
    source = Address(
        aci=envelope.get("sourceUuid") or None,
        number=envelope.get("sourceNumber") or None,
        name=envelope.get("sourceName") or None,
    )
    if source.key is None and envelope.get("source"):
        src = str(envelope["source"])
        source = Address(number=src, name=source.name) if src.startswith("+") else Address(aci=src, name=source.name)
    ts = envelope.get("timestamp")
    events: list[Event] = []

    dm = envelope.get("dataMessage")
    if isinstance(dm, dict):
        conv = _conv(dm, source)
        if conv is not None:
            events += _data_events(dm, conv, source, int(dm.get("timestamp") or ts or 0), outgoing=False)

    edit = envelope.get("editMessage")
    if isinstance(edit, dict) and isinstance(edit.get("dataMessage"), dict) and edit.get("targetSentTimestamp"):
        edm = edit["dataMessage"]
        conv = _conv(edm, source)
        if conv is not None:
            events += _data_events(edm, conv, source, int(edm.get("timestamp") or ts or 0), outgoing=False,
                                   edit_target=int(edit["targetSentTimestamp"]))

    sync = envelope.get("syncMessage")
    if isinstance(sync, dict) and isinstance(sync.get("sentMessage"), dict):
        events += _sync_sent(sync["sentMessage"], source, self_addr, ts)

    receipt = envelope.get("receiptMessage")
    if isinstance(receipt, dict) and receipt.get("timestamps"):
        state = "read" if (receipt.get("isRead") or receipt.get("isViewed")) else (
            "delivered" if receipt.get("isDelivery") else None)
        if state:
            events.append(ReceiptEvent(state, [int(t) for t in receipt["timestamps"]]))
    return events


def _sync_sent(sm: dict, source: Address, self_addr: Address, envelope_ts: Any) -> list[Event]:
    """A message the user sent from another device (their phone)."""
    me = Address(aci=source.aci or self_addr.aci, number=source.number or self_addr.number, name=self_addr.name)
    dest = Address(
        aci=sm.get("destinationUuid") or None,
        number=sm.get("destinationNumber") or None,
    )
    if dest.key is None and sm.get("destination"):
        d = str(sm["destination"])
        dest = Address(number=d) if d.startswith("+") else Address(aci=d)
    edit = sm.get("editMessage")
    if isinstance(edit, dict) and isinstance(edit.get("dataMessage"), dict) and edit.get("targetSentTimestamp"):
        edm = edit["dataMessage"]
        conv = _conv(edm, dest)
        if conv is None:
            return []
        return _data_events(edm, conv, me, int(edm.get("timestamp") or envelope_ts or 0), outgoing=True,
                            edit_target=int(edit["targetSentTimestamp"]))
    conv = _conv(sm, dest)
    if conv is None:
        return []
    return _data_events(sm, conv, me, int(sm.get("timestamp") or envelope_ts or 0), outgoing=True)
