"""The briefing pipeline, shared by the manual endpoint, entry regeneration
and the background scheduler.

    load_targets()  → which chats / important contacts to brief
    load_lines()    → newest messages of a target in [since, until]
    build_transcript(lines, char_budget) → newest lines that fit, numbered
    summarize()     → typed SummaryResult (ok | empty | llm_unavailable |
                      model_missing | timeout | llm_error)
    run_briefing(job) → persists the run incrementally, emits SSE events

Infrastructure failures (no model, model missing) abort the run with an
``error`` event; nothing placeholder-like is ever stored as a summary.
"""

import asyncio
import json
import logging
import re
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.llm.base import BaseLLMProvider, ChatMessage, LLMError
from backend.llm.budget import estimate_tokens, prompt_budget_tokens
from backend.llm.briefing_prompts import (
    DEFAULT_BRIEFING_CHAT_PROMPT,
    DEFAULT_IMPORTANT_CONTACT_PROMPT,
    DEFAULT_TRACKED_CHAT_PROMPT,
)
from backend.llm.resolve import error_kind, error_message, get_provider
from backend.org_context import OrgDirectory
from backend.serialization import iso_utc

logger = logging.getLogger(__name__)

BRIEFING_JOB_KEY = "briefing"
LLM_ROLE = "briefing"

MAX_LINE_CHARS = 500
OUTPUT_RESERVE = 1500  # answer tokens reserved in the context window
CHAT_MESSAGE_LIMIT = 400
CONTACT_MESSAGE_LIMIT = 200
ENTRY_DEADLINE_S = 600.0
PRESYNC_TIMEOUT_S = 120.0

NO_MESSAGES = "No messages in this period."
NO_TEXT_MESSAGES = "No text messages in this period."

CITATION_INSTRUCTION = (
    "Each message line starts with a reference number in square brackets, e.g. [12]. "
    "After each point you make, cite the lines it is based on with their reference "
    "numbers, e.g. [3][7]. Only use reference numbers that appear in the messages."
)


# ---------------------------------------------------------------------------
# Settings snapshot (prefs are read once per run)
# ---------------------------------------------------------------------------

@dataclass
class BriefingSettings:
    max_tokens: int = 4000
    prompt_override: Optional[str] = None
    category_prompts: dict = field(default_factory=dict)
    user_orgs: list = field(default_factory=list)
    user_org_ids: Optional[list] = None

    @classmethod
    def load(cls) -> "BriefingSettings":
        from backend.api.settings import (
            get_briefing_max_tokens,
            get_briefing_prompt_override,
            get_briefing_prompt_overrides,
        )
        from backend.org_context import get_user_organization_ids, get_user_organizations
        return cls(
            max_tokens=get_briefing_max_tokens(),
            prompt_override=get_briefing_prompt_override(),
            category_prompts=get_briefing_prompt_overrides(),
            user_orgs=get_user_organizations(),
            user_org_ids=get_user_organization_ids(),
        )

    def system_prompt(self, category: str) -> str:
        if category == "important_contact":
            return DEFAULT_IMPORTANT_CONTACT_PROMPT
        if self.prompt_override:
            return self.prompt_override
        if category == "tracked":
            return self.category_prompts.get("tracked") or DEFAULT_TRACKED_CHAT_PROMPT
        return self.category_prompts.get("briefing") or DEFAULT_BRIEFING_CHAT_PROMPT


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass
class Target:
    category: str  # 'tracked' | 'briefing' | 'important_contact'
    chat_id: int  # chat telegram_id, or the contact's telegram_id for important_contact
    name: str


@dataclass
class Line:
    chat_id: int
    tg_message_id: int
    sender: str
    text: str  # formatted transcript text without the reference prefix


def _contact_name(contact) -> str:
    return (
        f"{contact.first_name or ''} {contact.last_name or ''}".strip()
        or contact.username
        or str(contact.telegram_id)
    )


async def load_targets(db) -> list[Target]:
    tracked = await crud.get_tracked_chat_models(db)
    briefing = await crud.get_briefing_chats(db)
    contacts = await crud.get_important_contacts(db)
    targets: list[Target] = []
    seen: set[int] = set()
    for category, chats in (("tracked", tracked), ("briefing", briefing)):
        for c in chats:
            if c.telegram_id in seen or getattr(c, "is_deactivated", False):
                continue
            seen.add(c.telegram_id)
            targets.append(Target(category, c.telegram_id, c.name or str(c.telegram_id)))
    for contact in contacts:
        targets.append(Target("important_contact", contact.telegram_id, _contact_name(contact)))
    return targets


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


async def load_lines(db, target: Target, since: datetime, until: Optional[datetime]) -> list[Line]:
    lines: list[Line] = []
    if target.category == "important_contact":
        rows = await crud.get_messages_from_sender_since(
            db, target.chat_id, since, limit=CONTACT_MESSAGE_LIMIT, until=until
        )
        for msg, chat, reply_sender, reply_text in rows:
            if not msg.text:
                continue
            label = chat.name or str(chat.telegram_id)
            body = _clip(msg.text, MAX_LINE_CHARS)
            if reply_sender and reply_text:
                text = f'({label}) [↩ {reply_sender}: "{_clip(reply_text, 60)}"]: {body}'
            else:
                text = f"({label}) {body}"
            lines.append(Line(chat.telegram_id, msg.tg_message_id, target.name, text))
        return lines

    rows = await crud.get_messages_since(db, target.chat_id, since, limit=CHAT_MESSAGE_LIMIT, until=until)
    for m, reply_sender, reply_text in rows:
        if not m.text:
            continue
        sender = "You" if m.is_outgoing else (m.sender_name or "Unknown")
        body = _clip(m.text, MAX_LINE_CHARS)
        if reply_sender and reply_text:
            text = f'{sender} [↩ {reply_sender}: "{_clip(reply_text, 60)}"]: {body}'
        else:
            text = f"{sender}: {body}"
        lines.append(Line(m.chat_id, m.tg_message_id, sender, text))
    return lines


def transcript_budget(provider: BaseLLMProvider, max_tokens: int, overhead_text: str = "") -> int:
    """Tokens available for the transcript within the provider's context
    window (answer reserved, safety margin, prompt overhead subtracted)."""
    budget = prompt_budget_tokens(getattr(provider, "context_tokens", None), min(max_tokens, OUTPUT_RESERVE))
    return max(500, budget - estimate_tokens(overhead_text))


def build_transcript(lines: list[Line], budget_tokens: int) -> tuple[str, dict[int, Line]]:
    """Keep the NEWEST lines that fit in ``budget_tokens``, oldest-first, each
    prefixed with a reference number. Returns (text, {ref: Line})."""
    kept: list[Line] = []
    used = 0
    for line in reversed(lines):
        cost = estimate_tokens(line.text) + 3  # "[nnn] " + newline
        if kept and used + cost > budget_tokens:
            break
        kept.append(line)
        used += cost
    kept.reverse()
    refs = {i: line for i, line in enumerate(kept, start=1)}
    text = "\n".join(f"[{i}] {line.text}" for i, line in refs.items())
    return text, refs


# ---------------------------------------------------------------------------
# Output post-processing
# ---------------------------------------------------------------------------

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_CITE_GROUP_RE = re.compile(r"\[(?:id:)?\s*(\d+(?:\s*,\s*(?:id:)?\s*\d+)*)\s*\]")


def _unwrap_json(raw: str) -> str:
    """Some models answer {"summary": "..."} despite plain-text instructions."""
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    if s.startswith("{") and s.endswith("}"):
        try:
            obj = json.loads(s)
        except ValueError:
            return raw
        if isinstance(obj, dict):
            val = obj.get("summary") or obj.get("text") or obj.get("content")
            if isinstance(val, list):
                return "\n".join(f"- {v}" for v in val)
            if isinstance(val, str):
                return val
    return raw


def finalize_summary(raw: str, refs: dict[int, Line]) -> tuple[str, list[dict]]:
    """Strip hidden reasoning, map [n] citations to source messages and
    renumber them 1..k in order of first appearance. Markdown is preserved
    (the frontend renders it without raw HTML)."""
    text = _unwrap_json(_THINK_RE.sub("", raw)).strip()
    by_tg = {line.tg_message_id: line for line in refs.values()}
    order: dict[tuple[int, int], int] = {}
    sources: list[dict] = []

    def resolve(n: int) -> Optional[Line]:
        return refs.get(n) or by_tg.get(n)

    def repl(m: re.Match) -> str:
        out = []
        for part in re.split(r"\s*,\s*", m.group(1)):
            part = part.replace("id:", "").strip()
            if not part.isdigit():
                continue
            line = resolve(int(part))
            if line is None:
                continue
            key = (line.chat_id, line.tg_message_id)
            if key not in order:
                order[key] = len(order) + 1
                sources.append({"chat_id": line.chat_id, "telegram_id": line.tg_message_id})
            out.append(f"[{order[key]}]")
        return "".join(dict.fromkeys(out))

    text = _CITE_GROUP_RE.sub(repl, text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip(), sources


# ---------------------------------------------------------------------------
# Summarize
# ---------------------------------------------------------------------------

@dataclass
class SummaryResult:
    kind: str  # ok | empty | llm_unavailable | model_missing | timeout | llm_error
    summary: str = ""
    sources: list = field(default_factory=list)
    error: Optional[str] = None

    @property
    def persistable(self) -> bool:
        return self.kind in ("ok", "empty")

    @property
    def aborts_run(self) -> bool:
        return self.kind in ("llm_unavailable", "model_missing")


async def summarize(
    provider: BaseLLMProvider,
    target: Target,
    lines: list[Line],
    settings: BriefingSettings,
    org_context: Optional[str] = None,
    on_delta: Optional[Callable[[str, dict], None]] = None,
) -> SummaryResult:
    if not lines:
        return SummaryResult("empty", NO_TEXT_MESSAGES)
    header = f"Person: {target.name}" if target.category == "important_contact" else f"Chat: {target.name}"
    preamble = header + "\n\n"
    if org_context:
        preamble += org_context + "\n\n"
    preamble += CITATION_INSTRUCTION + "\n\nMessages:\n"
    system = settings.system_prompt(target.category)
    budget = transcript_budget(provider, settings.max_tokens, system + preamble)
    transcript, refs = build_transcript(lines, budget)
    user_msg = preamble + transcript
    messages = [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=user_msg),
    ]

    content = ""
    chunks = 0
    deadline = _time.monotonic() + ENTRY_DEADLINE_S
    try:
        async for delta in provider.stream(messages, max_tokens=settings.max_tokens, temperature=0.3):
            if delta.reasoning and on_delta:
                on_delta("thinking", {"chat_name": target.name, "reasoning": delta.reasoning})
            if delta.content:
                content += delta.content
                chunks += 1
                if on_delta and chunks % 10 == 0:
                    on_delta("thinking", {"chat_name": target.name, "tokens": chunks})
            if _time.monotonic() > deadline:
                return SummaryResult("timeout", error="The AI model took too long for this chat.")
    except LLMError as exc:
        return SummaryResult(error_kind(exc), error=error_message(exc))
    if on_delta and chunks:
        on_delta("thinking", {"chat_name": target.name, "tokens": chunks})

    summary, sources = finalize_summary(content, refs)
    if not summary:
        return SummaryResult("llm_error", error="The AI model returned an empty summary.")
    return SummaryResult("ok", summary, sources)


async def summarize_target(
    provider: BaseLLMProvider,
    target: Target,
    since: datetime,
    until: Optional[datetime],
    settings: BriefingSettings,
    orgs: Optional[OrgDirectory] = None,
    on_delta: Optional[Callable[[str, dict], None]] = None,
) -> SummaryResult:
    """Load a target's messages (short-lived session) and summarize them."""
    async with AsyncSessionLocal() as db:
        lines = await load_lines(db, target, since, until)
        if orgs is None:
            orgs = await OrgDirectory.load(db, settings.user_orgs, settings.user_org_ids)
    # Session closed: no SQLite read transaction is held across the LLM call.
    if not lines:
        has_any = await _has_messages(target, since, until)
        return SummaryResult("empty", NO_TEXT_MESSAGES if has_any else NO_MESSAGES)
    if target.category == "important_contact":
        org_ctx = orgs.for_contact(target.name)
    else:
        org_ctx = orgs.for_senders([ln.sender for ln in lines if ln.sender not in ("You", "Unknown")])
    return await summarize(provider, target, lines, settings, org_ctx, on_delta)


async def _has_messages(target: Target, since: datetime, until: Optional[datetime]) -> bool:
    from sqlalchemy import func, select
    from backend.db.models import Message
    col = Message.sender_id if target.category == "important_contact" else Message.chat_id
    q = select(func.count()).select_from(Message).where(col == target.chat_id, Message.date >= since)
    if until is not None:
        q = q.where(Message.date <= until)
    async with AsyncSessionLocal() as db:
        return bool((await db.execute(q)).scalar())


# ---------------------------------------------------------------------------
# Full run
# ---------------------------------------------------------------------------

@dataclass
class BriefingOutcome:
    status: str  # done | partial | empty | failed | cancelled
    run_id: Optional[int] = None
    ok: int = 0
    failed: int = 0
    error: Optional[str] = None
    error_kind: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.status in ("done", "partial", "empty")


async def presync(chat_ids: list[int], since: datetime) -> None:
    """Pull fresh history for briefing chats first (they are excluded from the
    automatic syncs). Best-effort: no Telegram connection is not an error."""
    if not chat_ids:
        return
    try:
        from backend.telegram.sync import sync_chats_since  # provided by the sync engine
    except (ImportError, AttributeError):
        return
    try:
        await asyncio.wait_for(sync_chats_since(chat_ids, since), timeout=PRESYNC_TIMEOUT_S)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # not connected, FloodWait, timeout...
        logger.info("Briefing pre-sync skipped: %s", exc)


def _mark_last_run() -> None:
    from backend.api.settings import _load_prefs, _save_prefs
    prefs = _load_prefs()
    prefs["briefing_last_run"] = datetime.now().isoformat()
    _save_prefs(prefs)


async def run_briefing(job, days: int, *, sync_first: bool = True) -> BriefingOutcome:
    """Generate, stream (via ``job.emit``) and persist a briefing."""
    emit = job.emit
    until = datetime.utcnow()
    since = until - timedelta(days=days)
    settings = BriefingSettings.load()

    async with AsyncSessionLocal() as db:
        targets = await load_targets(db)
        orgs = await OrgDirectory.load(db, settings.user_orgs, settings.user_org_ids)

    if not targets:
        # Nothing to summarize — completes without needing an LLM.
        emit("start", {"total": 0, "days": days, "run_id": None})
        emit("done", {"run_id": None, "status": "empty", "entries": 0, "failed": 0})
        _mark_last_run()
        return BriefingOutcome("empty")

    provider = get_provider(LLM_ROLE)  # LLMUnavailable → error event via the job runner

    if sync_first:
        emit("progress", {"current": 0, "total": len(targets), "chat_name": "", "stage": "syncing"})
        await presync([t.chat_id for t in targets if t.category != "important_contact"], since)

    async with AsyncSessionLocal() as db:
        run = await crud.create_briefing_run(db, days=days, status="running")
        run_id = run.id
    emit("start", {"total": len(targets), "days": days, "run_id": run_id})

    ok = failed = persisted = 0
    abort: Optional[SummaryResult] = None
    try:
        for idx, target in enumerate(targets):
            emit("progress", {"current": idx + 1, "total": len(targets), "chat_name": target.name})
            result = await summarize_target(provider, target, since, until, settings, orgs, emit)
            if result.persistable:
                async with AsyncSessionLocal() as db:
                    entry = await crud.add_briefing_entry(
                        db, run_id=run_id, chat_id=target.chat_id, chat_name=target.name,
                        category=target.category, summary=result.summary,
                        sources_json=json.dumps(result.sources) if result.sources else None,
                    )
                persisted += 1
                ok += result.kind == "ok"
                emit("chat_done", {
                    "entry_id": entry.id, "run_id": run_id, "chat_id": target.chat_id,
                    "chat_name": target.name, "category": target.category,
                    "summary": result.summary, "sources": result.sources,
                })
                continue
            failed += 1
            emit("chat_error", {
                "chat_id": target.chat_id, "chat_name": target.name, "category": target.category,
                "kind": result.kind, "message": result.error,
            })
            if result.aborts_run:
                abort = result
                break
    except asyncio.CancelledError:
        await _close_run(run_id, persisted, "partial", "Cancelled")
        raise

    if abort is not None or (failed and ok == 0):
        reason = abort or SummaryResult("llm_error", error="Every summary failed.")
        kept = await _close_run(run_id, persisted if ok else 0, "partial", reason.error)
        emit("error", {"message": reason.error, "kind": reason.kind, "run_id": kept})
        return BriefingOutcome("failed", kept, ok, failed, reason.error, reason.kind)

    status = "partial" if failed else "done"
    async with AsyncSessionLocal() as db:
        await crud.finish_briefing_run(db, run_id, status)
    _mark_last_run()
    emit("done", {"run_id": run_id, "status": status, "entries": persisted, "failed": failed})
    return BriefingOutcome(status, run_id, ok, failed)


async def _close_run(run_id: int, keep_entries: int, status: str, error: Optional[str]) -> Optional[int]:
    """Keep a run only if it holds at least one real summary; otherwise delete it."""
    async with AsyncSessionLocal() as db:
        if keep_entries and await crud.count_briefing_entries(db, run_id):
            await crud.finish_briefing_run(db, run_id, status, error)
            return run_id
        await crud.delete_briefing_run(db, run_id)
        return None


def start_briefing_job(days: int, *, trigger: str = "manual"):
    """Start (or join) the single briefing job. Returns (EventJob, started)."""
    from backend.briefing.events import start_event_job
    return start_event_job(
        BRIEFING_JOB_KEY,
        "briefing",
        lambda job: run_briefing(job, days),
        meta={"days": days, "trigger": trigger},
    )


_replace_lock: Optional[asyncio.Lock] = None


async def replace_briefing_job(days: int):
    """Atomically cancel any running briefing, wait for it to finish its
    cleanup, and start a ``days`` run. Serialized by a lock so concurrent
    replaces can't interleave. Returns (EventJob, replaced_job_id | None)."""
    global _replace_lock
    from backend.briefing.events import get_event_job
    if _replace_lock is None:
        _replace_lock = asyncio.Lock()
    async with _replace_lock:
        old = get_event_job(BRIEFING_JOB_KEY)
        replaced = None
        if old is not None and old.task is not None:
            replaced = old.id
            old.task.cancel()
            await asyncio.wait([old.task])
        job, _ = start_briefing_job(days, trigger="manual")
        return job, replaced


async def cleanup_stale_runs() -> None:
    """Runs left 'running' by a crash: keep those with entries as 'partial'."""
    from sqlalchemy import select
    from backend.db.models import BriefingRun
    async with AsyncSessionLocal() as db:
        ids = list((await db.execute(
            select(BriefingRun.id).where(BriefingRun.status == "running")
        )).scalars().all())
    for run_id in ids:
        await _close_run(run_id, 1, "partial", "Interrupted")


# ---------------------------------------------------------------------------
# Regenerate one entry of a past run
# ---------------------------------------------------------------------------

async def regenerate_entry(job, run_id: int, entry_id: int) -> Optional[dict]:
    from sqlalchemy import select
    from backend.db.models import BriefingEntry, BriefingRun

    async with AsyncSessionLocal() as db:
        run = (await db.execute(select(BriefingRun).where(BriefingRun.id == run_id))).scalar_one_or_none()
        entry = (await db.execute(
            select(BriefingEntry).where(BriefingEntry.id == entry_id, BriefingEntry.run_id == run_id)
        )).scalar_one_or_none()
    if run is None or entry is None:
        job.emit("error", {"message": "Briefing entry not found", "kind": "not_found"})
        return None
    if entry.category not in ("tracked", "briefing", "important_contact"):
        job.emit("error", {"message": f"Unsupported category: {entry.category}", "kind": "invalid"})
        return None

    provider = get_provider(LLM_ROLE)
    settings = BriefingSettings.load()
    # The run's own window, not "now": regenerating last week's entry must
    # summarize last week.
    until = run.generated_at
    since = until - timedelta(days=run.days or 1)
    target = Target(entry.category, entry.chat_id, entry.chat_name)
    job.emit("start", {"entry_id": entry.id, "run_id": run_id, "chat_name": entry.chat_name, "category": entry.category})

    def on_delta(event: str, data: dict) -> None:
        job.emit(event, {**data, "entry_id": entry.id})

    result = await summarize_target(provider, target, since, until, settings, None, on_delta)
    if not result.persistable:
        job.emit("error", {"message": result.error, "kind": result.kind, "entry_id": entry.id})
        return None

    async with AsyncSessionLocal() as db:
        fresh = (await db.execute(select(BriefingEntry).where(BriefingEntry.id == entry_id))).scalar_one_or_none()
        if fresh is not None:
            fresh.summary = result.summary
            fresh.sources_json = json.dumps(result.sources) if result.sources else None
            await db.commit()
    payload = {
        "id": entry.id, "run_id": run_id, "chat_id": entry.chat_id, "chat_name": entry.chat_name,
        "category": entry.category, "summary": result.summary, "sources": result.sources,
    }
    job.emit("done", {"entry": payload})
    return payload


def start_regenerate_job(run_id: int, entry_id: int):
    from backend.briefing.events import start_event_job
    return start_event_job(
        f"briefing-entry:{entry_id}",
        "briefing_entry",
        lambda job: regenerate_entry(job, run_id, entry_id),
        meta={"run_id": run_id, "entry_id": entry_id},
        cancel_when_orphaned=True,
    )


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def entry_dict(e) -> dict:
    return {
        "id": e.id,
        "chat_id": e.chat_id,
        "chat_name": e.chat_name,
        "category": e.category,
        "summary": e.summary,
        "sources": json.loads(e.sources_json) if e.sources_json else [],
    }


def run_dict(run, entries: list) -> dict:
    return {
        "id": run.id,
        "generated_at": iso_utc(run.generated_at),
        "finished_at": iso_utc(getattr(run, "finished_at", None)),
        "days": run.days,
        "status": getattr(run, "status", "done") or "done",
        "error": getattr(run, "error", None),
        "entries": [entry_dict(e) for e in entries],
    }
