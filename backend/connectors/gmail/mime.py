"""Gmail message payloads ↔ Mingly shapes.

``parse_message`` walks a ``format=full`` payload (multipart/alternative,
related, mixed; base64url bodies; declared charsets) into body text, sanitized
HTML and attachment descriptors. HTML goes through nh3 with a strict
allowlist — no scripts, ``<style>`` blocks or event handlers — and every
remote ``<img src>`` becomes ``data-src`` so nothing loads until the user
allows remote images for the sender. ``build_raw`` writes an outgoing
message as RFC 822 for ``messages.send``.
"""

from __future__ import annotations

import base64
import mimetypes
import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, parseaddr
from pathlib import Path
from typing import Iterable, Optional

import html2text
import nh3

# -- small helpers --------------------------------------------------------------


def b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def headers_of(part: dict) -> dict[str, str]:
    """Header name (lowercased) → value; the first occurrence wins."""
    out: dict[str, str] = {}
    for h in part.get("headers") or []:
        out.setdefault(h.get("name", "").lower(), h.get("value", ""))
    return out


def _param(header: str, name: str) -> Optional[str]:
    m = re.search(rf'{name}\s*=\s*"?([^";]+)"?', header or "", re.I)
    return m.group(1).strip() if m else None


def decode_body(data: bytes, charset: Optional[str]) -> str:
    """Decode a text part. Gmail usually hands bodies over in their declared
    charset, but some arrive already UTF-8 despite the label; non-ASCII bytes
    that are valid UTF-8 win over a legacy single-byte charset."""
    cs = (charset or "utf-8").strip().lower()
    if cs in ("utf-8", "utf8", "us-ascii", "ascii"):
        return data.decode("utf-8", errors="replace")
    if not data.isascii():
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            pass
    try:
        return data.decode(cs, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def parse_addresses(*values: Optional[str]) -> list[tuple[str, str]]:
    """``[(display name, lowercased email)]`` from address headers."""
    out = []
    seen = set()
    for name, addr in getaddresses([v for v in values if v]):
        addr = addr.strip().lower()
        if "@" not in addr or addr in seen:
            continue
        seen.add(addr)
        out.append((name.strip().strip('"') or "", addr))
    return out


def parse_address(value: Optional[str]) -> Optional[tuple[str, str]]:
    name, addr = parseaddr(value or "")
    addr = addr.strip().lower()
    return (name.strip().strip('"'), addr) if "@" in addr else None


# -- HTML sanitizing --------------------------------------------------------------

_TAGS = {
    "a", "abbr", "address", "b", "bdi", "bdo", "big", "blockquote", "br", "caption", "center", "cite", "code",
    "col", "colgroup", "dd", "del", "dfn", "div", "dl", "dt", "em", "font", "h1", "h2", "h3", "h4", "h5", "h6",
    "hr", "i", "img", "ins", "kbd", "li", "mark", "ol", "p", "pre", "q", "s", "samp", "small", "span", "strike",
    "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "tt", "u", "ul", "var",
}
_COMMON_ATTRS = {
    "align", "valign", "width", "height", "style", "dir", "title", "lang", "bgcolor", "color", "border",
    "cellpadding", "cellspacing", "colspan", "rowspan", "nowrap",
}
_ATTRS = {
    "*": _COMMON_ATTRS,
    "a": {"href", "name"},
    "img": {"src", "alt"},
    "font": {"face", "size"},
    "ol": {"start", "type"},
    "ul": {"type"},
    "blockquote": {"cite"},
}
# Layout and typography only: nothing that can load a URL (background,
# background-image, list-style-image, content, …) or escape the frame.
_STYLE_PROPERTIES = {
    "color", "background-color", "font", "font-family", "font-size", "font-style", "font-weight",
    "font-variant", "line-height", "letter-spacing", "word-spacing", "text-align", "text-decoration",
    "text-transform", "text-indent", "vertical-align", "white-space", "word-break", "word-wrap",
    "overflow-wrap", "direction", "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left", "border", "border-top",
    "border-right", "border-bottom", "border-left", "border-color", "border-style", "border-width",
    "border-collapse", "border-spacing", "border-radius", "width", "min-width", "max-width", "height",
    "min-height", "max-height", "display", "table-layout", "list-style-type", "opacity",
}
_URL_SCHEMES = {"http", "https", "mailto", "tel", "cid", "data"}
_REMOTE_IMG = re.compile(r'(<img\b[^>]*?)\ssrc="((?:https?:)?//[^"]*)"', re.I)
_DATA_URL = re.compile(r'\b(src|href)="data:(?!image/(?:png|jpe?g|gif|webp);)[^"]*"', re.I)


def sanitize_html(html: str) -> str:
    """Strict allowlist; remote images are blocked behind ``data-src``."""
    cleaned = nh3.clean(
        html,
        tags=_TAGS,
        clean_content_tags={"script", "style", "title", "head", "noscript", "template", "iframe", "object"},
        attributes=_ATTRS,
        filter_style_properties=_STYLE_PROPERTIES,
        url_schemes=_URL_SCHEMES,
        link_rel="noopener noreferrer",
        set_tag_attribute_values={"a": {"target": "_blank"}},
        strip_comments=True,
    )
    cleaned = _DATA_URL.sub("", cleaned)  # data: only for inline raster images
    return _REMOTE_IMG.sub(r'\1 data-src="\2"', cleaned)


def html_to_text(html: str) -> str:
    h = html2text.HTML2Text()
    h.ignore_images = True
    h.body_width = 0
    h.ignore_emphasis = True
    return h.handle(html).strip()


# -- payload walking ----------------------------------------------------------------

_PHOTO_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def attachment_kind(mime: str) -> str:
    mime = (mime or "").lower()
    if mime in _PHOTO_MIMES:
        return "photo"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "document"


@dataclass
class PartAttachment:
    part_id: str
    filename: str
    mime_type: str
    size: Optional[int]
    attachment_id: Optional[str]
    content_id: Optional[str] = None

    @property
    def external_ref(self) -> str:
        """``<partId>:<attachmentId>``: Gmail re-issues attachment ids, the
        part id is what stays stable."""
        return f"{self.part_id}:{self.attachment_id or ''}"


@dataclass
class ParsedBody:
    text: Optional[str] = None
    html: Optional[str] = None
    attachments: list[PartAttachment] = field(default_factory=list)


def _walk(part: dict) -> Iterable[dict]:
    yield part
    for child in part.get("parts") or []:
        yield from _walk(child)


def parse_payload(payload: dict) -> ParsedBody:
    out = ParsedBody()
    plain: list[str] = []
    html: list[str] = []
    for part in _walk(payload):
        mime = (part.get("mimeType") or "").lower()
        if mime.startswith("multipart/"):
            continue
        hdrs = headers_of(part)
        body = part.get("body") or {}
        disposition = (hdrs.get("content-disposition") or "").lower()
        filename = part.get("filename") or ""
        is_attachment = bool(filename) or disposition.startswith("attachment") or (
            body.get("attachmentId") and not mime.startswith("text/")
        )
        if is_attachment:
            cid = hdrs.get("content-id")
            out.attachments.append(PartAttachment(
                part_id=str(part.get("partId") or ""),
                filename=filename or "attachment",
                mime_type=mime or "application/octet-stream",
                size=body.get("size"),
                attachment_id=body.get("attachmentId"),
                content_id=cid.strip("<>") if cid else None,
            ))
            continue
        if mime not in ("text/plain", "text/html") or not body.get("data"):
            continue
        text = decode_body(b64url_decode(body["data"]), _param(hdrs.get("content-type", ""), "charset"))
        (plain if mime == "text/plain" else html).append(text)
    if html:
        out.html = sanitize_html("\n".join(html))
    if plain:
        out.text = "\n".join(plain).strip()
    elif html:
        out.text = html_to_text(out.html or "")
    return out


def find_part(payload: dict, part_id: str) -> Optional[dict]:
    return next((p for p in _walk(payload) if str(p.get("partId") or "") == part_id), None)


# -- outgoing ---------------------------------------------------------------------------

@dataclass
class OutgoingAttachment:
    path: str
    filename: str
    mime_type: Optional[str] = None


def build_raw(
    *,
    sender: str,
    to: list[str],
    cc: list[str],
    subject: str,
    text: str,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    attachments: Iterable[OutgoingAttachment] = (),
) -> str:
    """RFC 822 message, base64url-encoded for ``messages.send``."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to
    msg.set_content(text or "")
    for att in attachments:
        mime = att.mime_type or mimetypes.guess_type(att.filename)[0] or "application/octet-stream"
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(
            Path(att.path).read_bytes(), maintype=maintype, subtype=subtype or "octet-stream", filename=att.filename,
        )
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def format_address(name: Optional[str], email: str) -> str:
    return formataddr((name or "", email))
