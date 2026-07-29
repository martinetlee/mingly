"""Tests for URL sanitization in message HTML generation.

We mock heavy dependencies to test the pure utility functions in isolation.
"""

import sys
from unittest.mock import MagicMock

import pytest

# Mock all heavy transitive dependencies before importing sync.py
for mod_name in [
    "telethon", "telethon.events", "telethon.errors", "telethon.tl", "telethon.tl.types",
    "telethon.sessions", "telethon.extensions", "telethon.extensions.html",
    "cryptography", "cryptography.fernet", "cryptography.hazmat",
    "cryptography.hazmat.bindings", "cryptography.hazmat.bindings._rust",
    "cryptography.exceptions",
    "backend.telegram.client", "backend.secrets",
    "backend.db.database", "backend.db.models", "backend.db.crud",
    "backend.config",
    "backend.ws", "backend.ws.manager",
    "fastapi", "fastapi.websockets",
]:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = MagicMock()

# Now import the functions under test
from backend.telegram.sync import _safe_href, _sanitize_hrefs, _markdown_pass


class TestSafeHref:
    def test_https_allowed(self):
        assert _safe_href("https://example.com") == "https://example.com"

    def test_http_allowed(self):
        assert _safe_href("http://example.com") == "http://example.com"

    def test_mailto_allowed(self):
        assert _safe_href("mailto:user@example.com") == "mailto:user@example.com"

    def test_relative_path_allowed(self):
        assert _safe_href("/profile/123") == "/profile/123"

    def test_javascript_blocked(self):
        assert _safe_href("javascript:alert(1)") == "#"

    def test_javascript_case_insensitive(self):
        assert _safe_href("JavaScript:alert(1)") == "#"

    def test_data_blocked(self):
        assert _safe_href("data:text/html,<script>alert(1)</script>") == "#"

    def test_vbscript_blocked(self):
        assert _safe_href("vbscript:MsgBox") == "#"

    def test_empty_string(self):
        assert _safe_href("") == "#"

    def test_javascript_with_whitespace(self):
        assert _safe_href("  javascript:alert(1)") == "#"


class TestSanitizeHrefs:
    def test_blocks_javascript_href(self):
        html = '<a href="javascript:alert(1)">click</a>'
        result = _sanitize_hrefs(html)
        assert 'href="#"' in result
        assert "javascript:" not in result

    def test_allows_https_href(self):
        html = '<a href="https://example.com">link</a>'
        assert _sanitize_hrefs(html) == html

    def test_allows_profile_href(self):
        html = '<a href="/profile/123">mention</a>'
        assert _sanitize_hrefs(html) == html

    def test_blocks_data_href(self):
        html = '<a href="data:text/html,&lt;script&gt;">click</a>'
        result = _sanitize_hrefs(html)
        assert "data:" not in result
        assert 'href="#"' in result

    def test_multiple_hrefs_mixed(self):
        html = '<a href="https://ok.com">ok</a> <a href="javascript:bad()">bad</a>'
        result = _sanitize_hrefs(html)
        assert "https://ok.com" in result
        assert "javascript:" not in result


class TestMarkdownPass:
    def test_safe_link(self):
        result = _markdown_pass("[click](https://example.com)")
        assert 'href="https://example.com"' in result

    def test_javascript_link_blocked(self):
        result = _markdown_pass("[click](javascript:alert(1))")
        assert "javascript:" not in result
        assert 'href="#"' in result

    def test_data_link_blocked(self):
        result = _markdown_pass("[click](data:text/html,evil)")
        assert "data:" not in result
        assert 'href="#"' in result

    def test_double_bracket_link_safe(self):
        result = _markdown_pass("[[display]](https://example.com)")
        assert 'href="https://example.com"' in result

    def test_double_bracket_link_javascript_blocked(self):
        result = _markdown_pass("[[display]](javascript:alert(1))")
        assert "javascript:" not in result
        assert 'href="#"' in result
