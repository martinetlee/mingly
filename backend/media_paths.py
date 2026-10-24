"""Where media files live under DATA_DIR. One definition for every writer
(channel downloads, the generation-1 import); stored paths are relative to
DATA_DIR."""

from pathlib import Path
from typing import Optional

from backend.config import DATA_DIR

MEDIA_DIR = DATA_DIR / "media"
AVATAR_DIR = MEDIA_DIR / "avatars"


def attachment_file(conversation_id: int, attachment_id: int, ext: str, *, media_dir: Optional[Path] = None) -> Path:
    return (media_dir or MEDIA_DIR) / str(conversation_id) / f"{attachment_id}{ext}"


def identity_avatar_file(identity_id: int, *, media_dir: Optional[Path] = None) -> Path:
    return (media_dir or MEDIA_DIR) / "avatars" / "identities" / f"{identity_id}.jpg"


def conversation_avatar_file(conversation_id: int, *, media_dir: Optional[Path] = None) -> Path:
    return (media_dir or MEDIA_DIR) / "avatars" / "conversations" / f"{conversation_id}.jpg"
