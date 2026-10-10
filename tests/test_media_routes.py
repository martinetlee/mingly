"""A conversation's shared media / files / links, and on-demand attachment
download through the conversation's connector."""

from unittest.mock import AsyncMock, patch

import pytest

from tests import factories as f


@pytest.mark.asyncio
async def test_shared_media_files_and_links(api, db):
    conv = await f.conversation(db, "901", title="Pat")
    await f.message(db, conv, 1, "a photo", attachments=[{"kind": "photo"}])
    await f.message(db, conv, 2, "the doc", attachments=[{"kind": "document", "filename": "a.pdf"}])
    await f.message(db, conv, 3, "see https://example.com")
    await f.message(db, conv, 4, "plain")
    base = f"/api/conversations/{conv.id}/shared-media"

    media = (await api.get(base, params={"media_filter": "media"})).json()
    assert [m["text"] for m in media] == ["a photo"] and media[0]["attachments"][0]["kind"] == "photo"
    files = (await api.get(base, params={"media_filter": "files"})).json()
    assert [m["text"] for m in files] == ["the doc"]
    links = (await api.get(base, params={"media_filter": "links"})).json()
    assert [m["text"] for m in links] == ["see https://example.com"] and links[0]["attachments"] == []
    assert (await api.get(base, params={"media_filter": "nope"})).json() == []
    page = (await api.get(base, params={"media_filter": "media", "limit": 1, "offset": 1})).json()
    assert page == []


@pytest.mark.asyncio
async def test_media_download_goes_through_the_connector(api, db):
    conv = await f.conversation(db, "902", title="Quinn")
    msg = await f.message(db, conv, 1, "", attachments=[{"kind": "video", "size": 10}])
    att = msg.attachments[0]
    with patch("backend.telegram.media.download_attachment", AsyncMock(return_value="media/x/1.mp4")) as dl:
        r = await api.post(f"/api/media-download/{att.id}")
    assert r.status_code == 200 and r.json() == {"url": "/api/media/media/x/1.mp4", "kind": "video"}
    dl.assert_awaited_once_with(att.id)
    assert (await api.post("/api/media-download/999999")).status_code == 404

    from backend.media import MediaUnavailable
    with patch("backend.telegram.media.download_attachment", AsyncMock(side_effect=MediaUnavailable())):
        assert (await api.post(f"/api/media-download/{att.id}")).status_code == 503
