from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.database import Base


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String)
    type: Mapped[str | None] = mapped_column(String)  # private, group, channel
    username: Mapped[str | None] = mapped_column(String)
    is_monitored: Mapped[bool] = mapped_column(Boolean, default=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_id: Mapped[int | None] = mapped_column(BigInteger)
    sender_name: Mapped[str | None] = mapped_column(String)
    text: Mapped[str | None] = mapped_column(Text)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    is_outgoing: Mapped[bool] = mapped_column(Boolean, default=False)
    media_type: Mapped[str | None] = mapped_column(String)
    importance_score: Mapped[float | None] = mapped_column(Float)
    importance_reason: Mapped[str | None] = mapped_column(Text)
    user_flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
