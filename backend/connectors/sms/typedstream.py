"""Text of a Messages ``attributedBody`` blob.

Since macOS Ventura many rows of ``message`` leave ``text`` NULL and keep the
body only in ``attributedBody``: an ``NSAttributedString`` archived with
NeXTSTEP's *typedstream* format (``streamtyped`` header). A full decoder is
not needed — the string's characters are archived right after the
``NSString`` class (directly, or as the superclass of ``NSMutableString``):

    … NSString \\x01 \\x94 \\x84 \\x01 + <length> <UTF-8 bytes> …

``\\x84 \\x01 +`` is a one-character type encoding ("+" = raw bytes) and
``<length>`` a typedstream integer: one byte below 0x80, or 0x81 followed by
an int16 / 0x82 followed by an int32, little-endian.
"""

from typing import Optional

_CLASS = b"NSString"
_BYTES_TYPE = b"\x01+"
_TYPE_WINDOW = 8  # bytes between the class name and the type tag (version + class refs)
_INT16, _INT32 = 0x81, 0x82


def _read_length(data: bytes, pos: int) -> tuple[Optional[int], int]:
    if pos >= len(data):
        return None, pos
    tag = data[pos]
    if tag < 0x80:
        return tag, pos + 1
    width = {_INT16: 2, _INT32: 4}.get(tag)
    if width is None or pos + 1 + width > len(data):
        return None, pos
    return int.from_bytes(data[pos + 1:pos + 1 + width], "little"), pos + 1 + width


def _string_after(data: bytes, pos: int) -> Optional[str]:
    tag = data.find(_BYTES_TYPE, pos, pos + _TYPE_WINDOW + len(_BYTES_TYPE))
    if tag < 0:
        return None
    length, start = _read_length(data, tag + len(_BYTES_TYPE))
    if length is None or start + length > len(data):
        return None
    return data[start:start + length].decode("utf-8", errors="replace")


def decode_attributed_body(blob: Optional[bytes]) -> Optional[str]:
    """The plain text archived in ``blob``, or None when there is none."""
    if not blob:
        return None
    data = bytes(blob)
    start = 0
    while True:
        at = data.find(_CLASS, start)
        if at < 0:
            return None
        text = _string_after(data, at + len(_CLASS))
        if text is not None:
            return text
        start = at + 1
