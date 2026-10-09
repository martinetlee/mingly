"""Intelligence chat (agent loop through the provider, single-writer turn
persistence, windowed history) and batched mentions."""

import json
from unittest.mock import patch

import pytest

from backend.api import intelligence
from backend.db import crud
from backend.db.models import AIConversation
from backend.llm.base import CompletionResponse, LLMTimeout, ToolCall
from tests import factories
from tests.llm_fakes import FakeProvider, seed_chat, seed_msg


def _resp(content="", tool_calls=None):
    return CompletionResponse(content=content, model="m", provider="fake", instance_name="fake",
                              tool_calls=tool_calls)


def _parse(chunks):
    out = []
    for c in chunks:
        lines = c.strip().split("\n")
        out.append((lines[0][7:], json.loads(lines[1][6:])))
    return out


async def _ai_conversation(db):
    conv = AIConversation(title=None)
    db.add(conv)
    await db.commit()
    return conv.id


@pytest.mark.asyncio
async def test_agent_loop_uses_provider_tools_and_persists_turn_once(db):
    group = await seed_chat(db, -1001, "Roadmap Group")
    await seed_msg(db, group, 1, "the quarterly roadmap is ready")
    conv_id = await _ai_conversation(db)

    provider = FakeProvider(responses=[
        _resp(tool_calls=[ToolCall(id="c1", name="search_messages", arguments={"keyword": "roadmap"})]),
        _resp(content="The roadmap is ready."),
    ])
    # A client that still appends the user message itself must not cause a duplicate.
    await crud.add_ai_conversation_message(db, conv_id, "user", "what about the roadmap?")
    uid = await intelligence._persist_user_message(conv_id, "what about the roadmap?")
    history = intelligence._window_history(await crud.get_ai_conversation_messages(db, conv_id))

    with patch.object(intelligence, "get_provider", return_value=provider):
        events = _parse([c async for c in intelligence._run_agent(history, conv_id, uid)])

    names = [e for e, _ in events]
    assert names[0] == "tool_call" and names[1] == "tool_result" and names[-1] == "done"
    assert "roadmap" in events[1][1]["content"]
    text = "".join(d["content"] for e, d in events if e == "text")
    assert text == "The roadmap is ready."
    # second LLM call saw the assistant tool-call turn and the tool result
    second = provider.complete_calls[1]["messages"]
    assert second[-2].role == "assistant" and second[-2].tool_calls[0]["id"] == "c1"
    assert second[-1].role == "tool" and second[-1].tool_call_id == "c1"
    assert provider.complete_calls[0]["tools"] is intelligence.TOOLS

    rows = await crud.get_ai_conversation_messages(db, conv_id)
    assert [r.role for r in rows] == ["user", "tool", "assistant"]
    assert rows[-1].content == "The roadmap is ready."
    done = events[-1][1]
    assert done["assistant_message_id"] == rows[-1].id and done["user_message_id"] == rows[0].id


@pytest.mark.asyncio
async def test_agent_llm_failure_ends_with_typed_error_and_persists_nothing(db):
    conv_id = await _ai_conversation(db)
    provider = FakeProvider(fail_with=LLMTimeout("slow"))
    with patch.object(intelligence, "get_provider", return_value=provider):
        events = _parse([c async for c in intelligence._run_agent(
            [intelligence.ChatMessage(role="user", content="hi")], conv_id)])
    assert events == [("error", {"message": "The AI model timed out.", "kind": "timeout"})]
    assert await crud.get_ai_conversation_messages(db, conv_id) == []


def test_history_window_is_budgeted_and_starts_with_user():
    class Row:
        def __init__(self, role, content):
            self.role, self.content = role, content

    rows = [Row("user", "u" * 10), Row("assistant", "a" * 30000)] + \
        [Row("user", "q1"), Row("tool", "t"), Row("assistant", "a1"), Row("user", "q2")]
    out = intelligence._window_history(rows)
    assert out[0].role == "user" and out[0].content == "q1"
    assert out[1].role == "assistant" and out[1].content.startswith("[Tool results")
    assert out[-1].content == "q2"


@pytest.mark.asyncio
async def test_chat_endpoint_persists_user_message(api, db):
    conv_id = await _ai_conversation(db)
    provider = FakeProvider(responses=[_resp(content="hello!")])
    with patch.object(intelligence, "get_provider", return_value=provider):
        resp = await api.post("/api/intelligence/chat", json={"ai_conversation_id": conv_id, "message": "hey"})
    assert resp.status_code == 200
    assert "event: done" in resp.text
    rows = await crud.get_ai_conversation_messages(db, conv_id)
    assert [(r.role, r.content) for r in rows] == [("user", "hey"), ("assistant", "hello!")]


@pytest.mark.asyncio
async def test_get_chat_messages_tool_is_truncated(db):
    big = await seed_chat(db, -1001, "Big")
    for i in range(1, 61):
        await seed_msg(db, big, i, "x" * 2000, minutes_ago=100 - i)
    out = await intelligence.execute_tool("get_chat_messages", {"chat_name": "Big", "limit": 100}, db)
    assert len(out) <= intelligence.TOOL_RESULT_CHARS + 200
    assert "…" in out


@pytest.mark.asyncio
async def test_batched_mention_contexts_match_per_mention_queries(db):
    group = await seed_chat(db, -1001, "G")
    other = await seed_chat(db, -1002, "H")
    msgs = {}
    for i in range(1, 11):
        msgs[i] = await seed_msg(db, group, i, f"m{i}", minutes_ago=100 - i, is_outgoing=(i in (6, 9)))
        await seed_msg(db, other, i, f"o{i}", minutes_ago=100 - i, is_outgoing=(i == 4))
    anchors = [msgs[3], msgs[8], msgs[10]]
    batched = await crud.get_mention_contexts(db, anchors, before=2, after=2)
    for anchor in anchors:
        around = await crud.get_messages_around(db, anchor, before=2, after=2)
        reply = await crud.get_first_reply_after(db, anchor)
        got = batched[anchor.id]
        assert [m.id for m in got["before"]] == [m.id for m in around["before"]]
        assert [m.id for m in got["after"]] == [m.id for m in around["after"]]
        assert (got["reply"].id if got["reply"] else None) == (reply.id if reply else None)
        assert all(m.conversation_id == group.id for m in got["before"] + got["after"])


@pytest.mark.asyncio
async def test_mentions_endpoint_reply_gap(api, db):
    me = await factories.identity(db, 7, display_name="Me", username="me", is_self=True)
    account = await factories.account(db)
    await crud.set_account_owner(db, account.id, external_id="7", label="@me", self_identity_id=me.id)
    group = await seed_chat(db, -1001, "G")
    await seed_msg(db, group, 1, "hey @me look", minutes_ago=50)
    after = await seed_msg(db, group, 2, "context after", minutes_ago=40)
    await seed_msg(db, group, 3, "unrelated", minutes_ago=30)
    reply = await seed_msg(db, group, 4, "my reply", minutes_ago=20, is_outgoing=True)
    # An inline mention link (no @username in the text) counts too.
    await seed_msg(db, group, 5, "thanks Me", minutes_ago=10,
                   text_html='thanks <a href="/mention/telegram/7" data-mention="true">Me</a>')
    resp = await api.get("/api/intelligence/mentions")
    assert resp.status_code == 200, resp.text
    second, g = resp.json()
    assert second["mention_msg"]["text"] == "thanks Me" and not second["replied"]
    assert [m["id"] for m in g["context_after"]] == [after.id]
    assert g["replied"] and g["user_reply_msg"]["id"] == reply.id
    assert g["reply_has_gap"] is True
    assert g["mention_msg"]["date"].endswith("Z")
    assert g["conversation"]["id"] == group.id and g["conversation"]["tags"] == []
    # another channel's filter excludes them
    assert (await api.get("/api/intelligence/mentions", params={"channel": "gmail"})).json() == []


@pytest.mark.asyncio
async def test_tag_tools_never_use_board_stage_tags(db):
    from sqlalchemy import select
    from backend.db.models import Tag
    conv = await seed_chat(db, -1001, "Alpha Group")
    stage = Tag(name="Done", color="slate", category=crud.STAGE_CATEGORY)
    db.add(stage)
    await db.commit()

    out = await intelligence.execute_tool("tag_chat", {"chat_name": "Alpha", "tag_name": "done"}, db)
    assert out.startswith("Tagged"), out
    tags = await crud.get_tags_for_conversation(db, conv.id)
    assert [t.category for t in tags] != [crud.STAGE_CATEGORY] and stage.id not in [t.id for t in tags]

    out = await intelligence.execute_tool("tag_chats_matching", {"keyword": "Alpha", "tag_name": "  "}, db)
    assert out.startswith("Tool error"), out
    user_tags = (await db.execute(select(Tag).where(Tag.category.is_(None)))).scalars().all()
    assert all(t.name.strip() for t in user_tags)


@pytest.mark.asyncio
async def test_agent_prompt_fits_provider_context_window(db):
    from backend.llm.budget import estimate_tokens
    big = await seed_chat(db, -1001, "Big")
    for i in range(1, 101):
        await seed_msg(db, big, i, "長訊息 " * 150, minutes_ago=200 - i)  # CJK-heavy
    history = []
    for i in range(30):
        history.append(intelligence.ChatMessage(role="user", content=f"old question {i} " + "x" * 3000))
        history.append(intelligence.ChatMessage(role="assistant", content="old answer " + "y" * 3000))
    history.append(intelligence.ChatMessage(role="user", content="what is in Big?"))

    provider = FakeProvider(context_tokens=8192, responses=[
        _resp(tool_calls=[ToolCall(id="c1", name="get_chat_messages", arguments={"chat_name": "Big", "limit": 100})]),
        _resp(content="done"),
    ])
    with patch.object(intelligence, "get_provider", return_value=provider):
        events = _parse([c async for c in intelligence._run_agent(history, None)])
    assert events[-1][0] == "done"
    for call in provider.complete_calls:
        msgs = call["messages"]
        prompt = sum(intelligence._message_tokens(m) for m in msgs) + estimate_tokens(json.dumps(call["tools"]))
        assert prompt + intelligence.AGENT_MAX_TOKENS <= 8192, prompt
        assert msgs[0].role == "system" and msgs[1].role == "user"
        assert any(m.content == "what is in Big?" for m in msgs)
    # the tool result survived (clipped), paired with its tool call
    second = provider.complete_calls[1]["messages"]
    assert second[-1].role == "tool" and second[-1].tool_call_id == "c1" and "Big" in second[-1].content


@pytest.mark.asyncio
async def test_telegram_tools_report_not_connected(db):
    from backend.telegram.runtime import runtime

    async def no_client(require=True):
        return None

    with patch.object(runtime, "client", side_effect=no_client):
        out = await intelligence.execute_tool("sync_tagged_chats", {"tag_name": "X"}, db)
        assert out.startswith("Telegram not connected")


@pytest.mark.asyncio
async def test_suggest_title_never_overwrites_manual_rename(api, db):
    from sqlalchemy import select
    conv_id = await _ai_conversation(db)
    await crud.add_ai_conversation_message(db, conv_id, "user", "hello")

    async def rename_meanwhile(messages, **kw):
        await api.put(f"/api/ai/conversations/{conv_id}", json={"title": "Mine"})
        return "Auto"

    with patch("backend.api.ai_conversations.complete_text", rename_meanwhile):
        r = await api.post(f"/api/ai/conversations/{conv_id}/suggest-title")
    assert r.json() == {"title": "Mine", "applied": False}
    title = (await db.execute(
        select(AIConversation.title).where(AIConversation.id == conv_id).execution_options(populate_existing=True)
    )).scalar()
    assert title == "Mine"

    # Untitled: the suggestion is applied
    other = await _ai_conversation(db)
    await crud.add_ai_conversation_message(db, other, "user", "plans for the offsite")

    async def suggest(messages, **kw):
        return '"Offsite plans"'

    with patch("backend.api.ai_conversations.complete_text", suggest):
        r = await api.post(f"/api/ai/conversations/{other}/suggest-title")
    assert r.json() == {"title": "Offsite plans", "applied": True}
