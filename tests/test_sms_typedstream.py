"""attributedBody decoding and Messages timestamps (backend/connectors/sms)."""

from datetime import datetime

from backend.connectors.sms.chatdb import APPLE_EPOCH, apple_time, to_apple_ns
from backend.connectors.sms.typedstream import decode_attributed_body

_HEAD = (
    b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00"
    b"\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84"
)
_TAIL = b"\x86\x84\x02iI\x01\x05\x92\x84\x84\x84\x0cNSDictionary\x00\x94\x84\x01i\x01\x92\x84\x96\x96\x1d__kIMMessagePartAttributeName\x86"


def _length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    if n < 0x10000:
        return b"\x81" + n.to_bytes(2, "little")
    return b"\x82" + n.to_bytes(4, "little")


def blob(text: str, mutable: bool = False) -> bytes:
    """An attributedBody as Messages archives it (NSString or NSMutableString)."""
    raw = text.encode("utf-8")
    cls = (b"\x0fNSMutableString\x01\x84\x84\x08NSString\x01\x95" if mutable else b"\x08NSString\x01\x94")
    return _HEAD + cls + b"\x84\x01+" + _length(len(raw)) + raw + _TAIL


def test_decodes_short_text():
    assert decode_attributed_body(blob("Hello")) == "Hello"


def test_decodes_multibyte_and_long_text():
    text = "hi there 👋 " + "ü" * 200
    assert decode_attributed_body(blob(text)) == text
    huge = "x" * 70000
    assert decode_attributed_body(blob(huge)) == huge


def test_decodes_mutable_string():
    assert decode_attributed_body(blob("edited text", mutable=True)) == "edited text"


def test_keeps_attachment_placeholder_for_the_caller():
    assert decode_attributed_body(blob("￼Look")) == "￼Look"


def test_garbage_and_truncated_blobs_give_none():
    assert decode_attributed_body(None) is None
    assert decode_attributed_body(b"") is None
    assert decode_attributed_body(b"streamtyped but no string here") is None
    assert decode_attributed_body(b"NSString\x01\x94\x84\x01+\x81") is None  # length cut off
    assert decode_attributed_body(b"NSString\x01\x94\x84\x01+\x20abc") is None  # fewer bytes than the length
    assert decode_attributed_body(b"NSString\x01\x94\x84\x01+\x85abc") is None  # not an integer tag


def test_apple_time_seconds_and_nanoseconds():
    when = datetime(2024, 3, 5, 12, 30, 15, 250000)
    seconds = int((when - APPLE_EPOCH).total_seconds())
    assert apple_time(seconds) == when.replace(microsecond=0)  # High Sierra and older: seconds
    assert apple_time(to_apple_ns(when)) == when  # current macOS: nanoseconds
    assert apple_time(to_apple_ns(when) + 999) == when  # sub-microsecond part dropped
    assert apple_time(0) is None and apple_time(None) is None
    assert apple_time(1) == datetime(2001, 1, 1, 0, 0, 1)
