"""Tests for /api/media path-traversal containment.

These tests verify the containment logic used in serve_media()
without importing the full application (which has heavy dependencies).
"""

from pathlib import Path

import pytest


def is_contained(file_path: str, data_dir: Path) -> bool:
    """Reproduce the containment check from serve_media().

    Returns True if `file_path` resolves to somewhere inside `data_dir`.
    """
    full_path = (data_dir / file_path).resolve()
    return full_path.is_relative_to(data_dir.resolve())


def is_contained_old(file_path: str, data_dir: Path) -> bool:
    """The OLD (vulnerable) containment check using startswith."""
    full_path = (data_dir / file_path).resolve()
    return str(full_path).startswith(str(data_dir.resolve()))


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Create a fake DATA_DIR with a sample file and a sibling dir."""
    d = tmp_path / "data"
    d.mkdir()
    (d / "photo.jpg").write_bytes(b"\xff\xd8fake-jpeg")

    # Create a sibling whose name starts with the same prefix
    sibling = tmp_path / "data2"
    sibling.mkdir()
    (sibling / "evil.txt").write_text("secret")
    return d


def test_valid_file_allowed(data_dir):
    """A file inside DATA_DIR should be allowed."""
    assert is_contained("photo.jpg", data_dir)


def test_dot_dot_traversal_blocked(data_dir):
    """Classic ../ traversal must be rejected."""
    assert not is_contained("../../etc/passwd", data_dir)


def test_sibling_directory_bypass_blocked(data_dir):
    """A sibling path like 'data2' sharing a string prefix must be rejected.

    The old startswith check would wrongly allow this because
    '/tmp/.../data2/evil.txt' starts with '/tmp/.../data'.
    """
    # This path resolves to data_dir/../data2/evil.txt
    assert not is_contained("../data2/evil.txt", data_dir)


def test_sibling_bypass_passes_old_check(data_dir):
    """Demonstrate that the OLD check was actually vulnerable."""
    # The old startswith check WRONGLY allows the sibling path
    assert is_contained_old("../data2/evil.txt", data_dir), (
        "Old startswith check should (incorrectly) allow sibling paths"
    )


def test_nonexistent_but_inside_dir_allowed(data_dir):
    """A non-existent file inside DATA_DIR passes containment."""
    assert is_contained("nonexistent.png", data_dir)


def test_absolute_path_escape_blocked(data_dir):
    """Joining an absolute path should not escape DATA_DIR on resolve."""
    # Pathlib / with an absolute path replaces the left side,
    # but resolve() still yields a path outside data_dir.
    result = is_contained("/etc/passwd", data_dir)
    # This should be blocked because /etc/passwd is not under data_dir
    assert not result
