from datetime import datetime
from typing import Optional
from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.database import Base


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String)
    type: Mapped[Optional[str]] = mapped_column(String)  # private, group, channel
    username: Mapped[Optional[str]] = mapped_column(String)
    is_monitored: Mapped[bool] = mapped_column(Boolean, default=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    is_briefing: Mapped[bool] = mapped_column(Boolean, default=False)
    is_tracked: Mapped[bool] = mapped_column(Boolean, default=False)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("telegram_id", "chat_id", name="uq_message_telegram_chat"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    sender_name: Mapped[Optional[str]] = mapped_column(String)
    text: Mapped[Optional[str]] = mapped_column(Text)
    text_html: Mapped[Optional[str]] = mapped_column(Text)
    reactions_json: Mapped[Optional[str]] = mapped_column(Text)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    is_outgoing: Mapped[bool] = mapped_column(Boolean, default=False)
    media_type: Mapped[Optional[str]] = mapped_column(String)
    media_path: Mapped[Optional[str]] = mapped_column(String)
    importance_score: Mapped[Optional[float]] = mapped_column(Float)
    importance_reason: Mapped[Optional[str]] = mapped_column(Text)
    user_flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
