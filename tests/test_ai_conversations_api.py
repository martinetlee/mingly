"""AI chat history routes (/api/ai/conversations): CRUD, visible message
counts (tool traces hidden) and compare-and-set title suggestion."""

from unittest.mock import AsyncMock, patch

import pytest

from backend.db import crud


@pytest.mark.asyncio
async def test_create_append_list_get_rename_delete(api, db):
    r = await api.post("/api/ai/conversations/")
    assert r.status_code == 201
    conv = r.json()
    assert conv["title"] is None and conv["messages"] == []
    cid = conv["id"]

    r = await api.post(f"/api/ai/conversations/{cid}/messages", json={"role": "user", "content": "hello"})
    assert r.status_code == 201 and r.json()["role"] == "user" and r.json()["content"] == "hello"
    await crud.add_ai_conversation_message(db, cid, "tool", "internal trace")
    await api.post(f"/api/ai/conversations/{cid}/messages", json={"role": "assistant", "content": "hi!"})
    assert (await api.post(f"/api/ai/conversations/{cid}/messages",
                           json={"role": "tool", "content": "x"})).status_code == 400

    listed = (await api.get("/api/ai/conversations/")).json()
    assert [(c["id"], c["message_count"]) for c in listed] == [(cid, 2)]

    detail = (await api.get(f"/api/ai/conversations/{cid}")).json()
    assert [(m["role"], m["content"]) for m in detail["messages"]] == [("user", "hello"), ("assistant", "hi!")]

    r = await api.put(f"/api/ai/conversations/{cid}", json={"title": "Plans"})
    assert r.status_code == 200 and r.json()["title"] == "Plans" and len(r.json()["messages"]) == 2

    assert (await api.delete(f"/api/ai/conversations/{cid}")).status_code == 204
    assert (await api.get(f"/api/ai/conversations/{cid}")).status_code == 404
    assert (await api.get("/api/ai/conversations/")).json() == []
    assert await crud.get_ai_conversation_messages(db, cid) == []
    for method, path in (("get", ""), ("put", ""), ("delete", ""), ("post", "/messages"), ("post", "/suggest-title")):
        kw = {"json": {"title": "x", "role": "user", "content": "x"}} if method in ("put", "post") else {}
        assert (await getattr(api, method)(f"/api/ai/conversations/999{path}", **kw)).status_code == 404


@pytest.mark.asyncio
async def test_suggest_title_is_compare_and_set(api, db):
    cid = (await api.post("/api/ai/conversations/")).json()["id"]
    assert (await api.post(f"/api/ai/conversations/{cid}/suggest-title")).json() == {"title": None, "applied": False}
    await api.post(f"/api/ai/conversations/{cid}/messages", json={"role": "user", "content": "trip to Lisbon"})

    with patch("backend.api.ai_conversations.complete_text", AsyncMock(return_value='"Lisbon trip plans"')) as llm:
        r = await api.post(f"/api/ai/conversations/{cid}/suggest-title")
    assert r.json() == {"title": "Lisbon trip plans", "applied": True}
    assert "USER: trip to Lisbon" in llm.call_args.args[0][1].content

    # Titled now: never overwritten, no LLM call
    with patch("backend.api.ai_conversations.complete_text", AsyncMock()) as llm:
        r = await api.post(f"/api/ai/conversations/{cid}/suggest-title")
    assert r.json() == {"title": "Lisbon trip plans", "applied": False} and not llm.called

    # A rename that lands while the LLM runs wins
    other = (await api.post("/api/ai/conversations/")).json()["id"]
    await api.post(f"/api/ai/conversations/{other}/messages", json={"role": "user", "content": "x"})

    async def rename_meanwhile(*a, **k):
        await api.put(f"/api/ai/conversations/{other}", json={"title": "Mine"})
        return "Suggested"

    with patch("backend.api.ai_conversations.complete_text", rename_meanwhile):
        r = await api.post(f"/api/ai/conversations/{other}/suggest-title")
    assert r.json() == {"title": "Mine", "applied": False}
