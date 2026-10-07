"""Test doubles for the LLM layer (no network)."""

from contextlib import ExitStack
from datetime import datetime, timedelta
from typing import Optional
from unittest.mock import patch

from backend.llm.base import BaseLLMProvider, CompletionResponse, StreamDelta


class FakeProvider(BaseLLMProvider):
    provider_name = "fake"
    supports_tools = True

    def __init__(self, summary="- Point one [1]", responses=None, fail_with: Optional[Exception] = None,
                 fail_on_call: int = 0, context_tokens=None):
        self.instance_name = "fake"
        self.model = "fake-model"
        self.summary = summary
        self.responses = list(responses or [])
        self.fail_with = fail_with
        self.fail_on_call = fail_on_call
        self.context_tokens = context_tokens
        self.stream_calls: list = []
        self.complete_calls: list = []

    async def complete(self, messages, *, max_tokens=1024, temperature=0.3, tools=None):
        self.complete_calls.append({"messages": list(messages), "tools": tools})
        if self.fail_with is not None and len(self.complete_calls) > self.fail_on_call:
            raise self.fail_with
        if self.responses:
            return self.responses.pop(0)
        return CompletionResponse(content=self.summary, model=self.model, provider="fake", instance_name="fake")

    async def stream(self, messages, *, max_tokens=1024, temperature=0.3):
        self.stream_calls.append(list(messages))
        if self.fail_with is not None and len(self.stream_calls) > self.fail_on_call:
            raise self.fail_with
        yield StreamDelta(reasoning="thinking...")
        for i in range(0, len(self.summary), 5):
            yield StreamDelta(content=self.summary[i:i + 5])


def patch_briefing_env(provider, *, prefs: Optional[dict] = None) -> ExitStack:
    """Point the briefing pipeline at ``provider`` and keep prefs in memory
    (never touch the real prefs.json)."""
    from backend.briefing.pipeline import BriefingSettings

    store = dict(prefs or {})
    stack = ExitStack()
    stack.enter_context(patch("backend.briefing.pipeline.get_provider", return_value=provider))
    # A registered provider means "configured" for the scheduler's pre-check,
    # even if that provider then fails (e.g. Ollama down → backoff).
    stack.enter_context(patch("backend.briefing_scheduler.llm_configured", return_value=True))
    stack.enter_context(patch.object(BriefingSettings, "load", classmethod(lambda cls: BriefingSettings())))
    stack.enter_context(patch("backend.api.settings._load_prefs", side_effect=lambda: dict(store)))
    stack.enter_context(patch("backend.api.settings._save_prefs", side_effect=lambda d: (store.clear(), store.update(d))))
    stack.enter_context(patch("backend.briefing.pipeline.presync", side_effect=_no_presync))
    stack.prefs = store
    return stack


async def _no_presync(chat_ids, since):
    return None


async def seed_chat(db, telegram_id, name, **flags):
    from backend.db.models import Chat
    chat = Chat(telegram_id=telegram_id, name=name, type=flags.pop("type", "group"), **flags)
    db.add(chat)
    await db.commit()
    return chat


async def seed_msg(db, chat_id, tg_id, text, minutes_ago=5, **kw):
    from backend.db.models import Message
    msg = Message(
        tg_message_id=tg_id, chat_id=chat_id, sender_name=kw.pop("sender_name", "Alice"), text=text,
        date=datetime.utcnow() - timedelta(minutes=minutes_ago), **kw,
    )
    db.add(msg)
    await db.commit()
    return msg
