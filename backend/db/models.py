from datetime import datetime
from typing import Optional
from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
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
    is_forum: Mapped[bool] = mapped_column(Boolean, default=False)
    slow_mode_seconds: Mapped[int] = mapped_column(Integer, default=0)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    is_muted: Mapped[bool] = mapped_column(Boolean, default=False)
    muted_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_marked_unread: Mapped[bool] = mapped_column(Boolean, default=False)
    unread_count: Mapped[int] = mapped_column(Integer, default=0)
    members_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    pinned_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    last_message_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_message_sender: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_message_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("tg_message_id", "chat_id", name="uq_message_tgmsgid_chat"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
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
    reply_to_tg_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    my_reaction: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    topic_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    media_filename: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    media_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    grouped_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    delivery_state: Mapped[Optional[str]] = mapped_column(String, default="sent")  # sending, sent, delivered, read
    is_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    scam_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    scam_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scam_llm_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scam_llm_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scam_category: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    color: Mapped[str] = mapped_column(String, default="slate")
    category: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ChatTag(Base):
    __tablename__ = "chat_tags"
    __table_args__ = (UniqueConstraint("chat_id", "tag_id", name="uq_chat_tag"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # telegram_id
    tag_id: Mapped[int] = mapped_column(Integer, ForeignKey("tags.id"), nullable=False)


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    first_name: Mapped[Optional[str]] = mapped_column(String)
    last_name: Mapped[Optional[str]] = mapped_column(String)
    username: Mapped[Optional[str]] = mapped_column(String)
    phone: Mapped[Optional[str]] = mapped_column(String)
    twitter_handle: Mapped[Optional[str]] = mapped_column(String)
    linkedin_url: Mapped[Optional[str]] = mapped_column(String)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    is_important: Mapped[bool] = mapped_column(Boolean, default=False)
    relationship_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    relationship_status_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    last_interaction_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_outgoing_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_incoming_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ContactPosition(Base):
    __tablename__ = "contact_positions"
    __table_args__ = (UniqueConstraint("telegram_id", "organization", name="uq_telegram_org"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    organization: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)  # True = current, False = former
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # when they left
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    context: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # null = untitled/pending auto-suggest
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)  # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CatchUpItem(Base):
    __tablename__ = "catchup_items"
    __table_args__ = (UniqueConstraint("chat_telegram_id", name="uq_catchup_chat"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    contact_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending, handled, snoozed
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    snoozed_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    handled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CatchUpSummary(Base):
    __tablename__ = "catchup_summaries"
    __table_args__ = (UniqueConstraint("chat_telegram_id", name="uq_catchup_summary_chat"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    summary_messages_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    recent_messages_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    action_needed: Mapped[bool] = mapped_column(Boolean, default=False)
    total_messages_at_generation: Mapped[int] = mapped_column(Integer, default=0)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BriefingRun(Base):
    __tablename__ = "briefing_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    days: Mapped[int] = mapped_column(Integer, default=1)


class ScamCheckRequest(Base):
    __tablename__ = "scam_check_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    request_type: Mapped[str] = mapped_column(String, nullable=False)  # are_flagged_scams, is_this_scam
    status: Mapped[str] = mapped_column(String, default="pending")  # pending, awaiting_followup, completed, failed, expired
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    response_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    related_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    related_message_ids_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    related_telegram_ids_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    followup_request_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ScamFlag(Base):
    __tablename__ = "scam_flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)  # local, cloud, manual
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    request_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    flagged_message_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    flagged_message_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    flagged_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    flagged_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ScamReport(Base):
    __tablename__ = "scam_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    reported_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reported_in_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    reporter_verdict: Mapped[str] = mapped_column(String, nullable=False)
    scam_category: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    custom_category: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    messages_json: Mapped[str] = mapped_column(Text, nullable=False)
    local_detection_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="submitted")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BriefingEntry(Base):
    __tablename__ = "briefing_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, ForeignKey("briefing_runs.id", ondelete="CASCADE"), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_name: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)  # 'tracked' | 'briefing' | 'important_contact'
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    sources_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON: [{chat_id, telegram_id}]
