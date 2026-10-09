"""Gmail MIME parsing, HTML sanitizing and outgoing message building."""

import base64
import email
from email import policy

from backend.connectors.gmail import mime


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def part(mime_type, data: bytes = b"", *, part_id="0", filename="", headers=(), attachment_id=None, size=None, parts=()):
    body = {"size": size if size is not None else len(data)}
    if attachment_id:
        body["attachmentId"] = attachment_id
    elif data:
        body["data"] = b64(data)
    return {
        "partId": part_id, "mimeType": mime_type, "filename": filename,
        "headers": [{"name": k, "value": v} for k, v in headers], "body": body, "parts": list(parts),
    }


def test_alternative_prefers_plain_text_and_sanitizes_html():
    payload = part("multipart/alternative", parts=[
        part("text/plain", "Grüße, Ann".encode("iso-8859-1"), part_id="0",
             headers=[("Content-Type", 'text/plain; charset="ISO-8859-1"')]),
        part("text/html", b'<p onclick="x()">Hi<script>alert(1)</script><img src="https://t.example/p.gif"></p>',
             part_id="1", headers=[("Content-Type", "text/html; charset=utf-8")]),
    ])
    body = mime.parse_payload(payload)
    assert body.text == "Grüße, Ann"
    assert "script" not in body.html and "onclick" not in body.html and "alert" not in body.html
    assert 'data-src="https://t.example/p.gif"' in body.html and ' src="https' not in body.html
    assert body.attachments == []


def test_html_only_falls_back_to_html2text_and_mixed_collects_attachments():
    payload = part("multipart/mixed", parts=[
        part("multipart/related", part_id="0", parts=[
            part("text/html", b"<h1>Title</h1><p>Body <b>bold</b></p>", part_id="0.0"),
            part("image/png", part_id="0.1", filename="logo.png", attachment_id="ATT-logo", size=10,
                 headers=[("Content-ID", "<logo@x>"), ("Content-Disposition", "inline")]),
        ]),
        part("application/pdf", part_id="1", filename="deck.pdf", attachment_id="ATT-pdf", size=2048,
             headers=[("Content-Disposition", 'attachment; filename="deck.pdf"')]),
    ])
    body = mime.parse_payload(payload)
    assert "Title" in body.text and "Body bold" in body.text
    assert [(a.filename, a.mime_type, a.external_ref, a.content_id) for a in body.attachments] == [
        ("logo.png", "image/png", "0.1:ATT-logo", "logo@x"),
        ("deck.pdf", "application/pdf", "1:ATT-pdf", None),
    ]
    assert mime.attachment_kind("image/png") == "photo" and mime.attachment_kind("application/pdf") == "document"


def test_sanitizer_strips_dangerous_markup_and_style_urls():
    html = mime.sanitize_html(
        '<style>p{color:red}</style><p style="color:red;background:url(https://x/y);position:fixed">a</p>'
        '<a href="javascript:alert(1)">j</a><a href="https://ok.example">k</a>'
        '<iframe src="https://evil"></iframe><form><input name=x></form>'
        '<img src="data:text/html;base64,PGI+"><img src="data:image/png;base64,AAAA">'
    )
    assert "<style" not in html and "position" not in html and "url(" not in html
    assert "javascript" not in html and "iframe" not in html and "<input" not in html and "<form" not in html
    assert 'href="https://ok.example"' in html and 'rel="noopener noreferrer"' in html
    assert "data:text/html" not in html and 'src="data:image/png;base64,AAAA"' in html


def test_utf8_body_mislabeled_as_latin1_still_decodes():
    assert mime.decode_body("naïve".encode("utf-8"), "iso-8859-1") == "naïve"
    assert mime.decode_body("naïve".encode("iso-8859-1"), "iso-8859-1") == "naïve"
    assert mime.decode_body(b"hi", "x-unknown-charset") == "hi"


def test_build_raw_threads_the_reply_and_attaches_files(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_bytes(b"attached")
    raw = mime.build_raw(
        sender="me@example.com", to=["ann@example.com"], cc=["bob@example.com"], subject="Re: Plans",
        text="Sounds good", in_reply_to="<orig@mail>", references="<root@mail>",
        attachments=[mime.OutgoingAttachment(str(f), "notes.txt", "text/plain")],
    )
    msg = email.message_from_bytes(mime.b64url_decode(raw), policy=policy.default)
    assert msg["To"] == "ann@example.com" and msg["Cc"] == "bob@example.com"
    assert msg["In-Reply-To"] == "<orig@mail>" and msg["References"] == "<root@mail> <orig@mail>"
    assert msg.get_body(("plain",)).get_content().strip() == "Sounds good"
    [att] = list(msg.iter_attachments())
    assert att.get_filename() == "notes.txt" and att.get_content() == "attached"
