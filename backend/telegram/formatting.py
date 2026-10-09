"""Message text → safe HTML.

Telegram formatting is carried by ``msg.entities``, whose offsets refer to the
*raw* text (``msg.message`` / ``msg.raw_text``). Never feed ``msg.text`` here:
Telethon builds it by markdown-unparsing the message, so the entity offsets no
longer line up and the HTML comes out garbled.
"""

import re
from html import escape
from typing import Optional
from urllib.parse import urlparse

_SAFE_URL_SCHEMES = frozenset(["http", "https", "ftp", "ftps", "mailto", "tel"])


def safe_href(url: str) -> str:
    """Return the URL only if its scheme is in the allowlist, otherwise '#'.

    Also allows relative paths (starting with '/') for in-app navigation.
    """
    stripped = url.strip()
    if stripped.startswith("/"):
        return url
    try:
        parsed = urlparse(stripped)
        if parsed.scheme.lower() in _SAFE_URL_SCHEMES:
            return url
    except Exception:
        pass
    return "#"


def sanitize_hrefs(html: str) -> str:
    """Replace any href with an unsafe scheme with '#'."""
    return re.sub(r'href="([^"]*)"', lambda m: f'href="{safe_href(m.group(1))}"', html)


def _text_nodes(html: str, fn) -> str:
    """Apply fn to every text node (content between HTML tags)."""
    parts = re.split(r'(<[^>]+>)', html)
    return ''.join(fn(p) if not p.startswith('<') else p for p in parts)


def _newlines_to_br(html: str) -> str:
    return _text_nodes(html, lambda t: t.replace('\n', '<br>'))


def markdown_pass(html: str) -> str:
    """Markdown → HTML for messages that carry no entities.

    Bots that send without a parse mode deliver literal ``**bold**`` /
    ``[label](url)`` markers; render those. Safe to run on escaped HTML.
    """
    def _links(text: str) -> str:
        # [[display]](url) → [display] as link text (matching official Telegram display).
        # \s* allows the bot to put the url on the next line: [[link]]\n(url)
        text = re.sub(
            r'\[\[([^\]]+)\]\]\s*\(([^)]+)\)',
            lambda m: f'<a href="{safe_href(m.group(2))}" target="_blank" rel="noopener noreferrer">[{m.group(1)}]</a>',
            text,
        )
        text = re.sub(
            r'\[([^\]]+)\]\s*\(([^)]+)\)',
            lambda m: f'<a href="{safe_href(m.group(2))}" target="_blank" rel="noopener noreferrer">{m.group(1)}</a>',
            text,
        )
        return text

    html = _text_nodes(html, _links)

    def _inline(text: str) -> str:
        text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
        text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text, flags=re.DOTALL)
        text = re.sub(r'`(.+?)`', r'<code>\1</code>', text, flags=re.DOTALL)
        return text

    html = _text_nodes(html, _inline)
    return _newlines_to_br(html)


def entities_to_html(raw_text: str, entities) -> Optional[str]:
    """Render raw message text + Telegram entities as sanitized HTML.

    Returns None when the message has no formatting (plain text renders as-is).
    """
    if not raw_text:
        return None

    if entities:
        from telethon.extensions import html as thtml
        try:
            result = thtml.unparse(raw_text, entities)
        except Exception:
            result = None
        if result is not None:
            # tg://user?id=X mention links → in-app mention links, resolved
            # to a person by the frontend's /mention/:channel/:externalId route
            result = re.sub(
                r'href="tg://user\?id=(\d+)"',
                r'href="/mention/telegram/\1" data-mention="true"',
                result,
            )
            return sanitize_hrefs(_newlines_to_br(result))

    escaped = escape(raw_text)
    result = markdown_pass(escaped)
    return result if result != escaped else None
