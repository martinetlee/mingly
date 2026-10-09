"""Schema v2: channel-neutral, person-centric (docs/multichannel/SCHEMA_V2.md).

Every row is keyed by an internal integer id. A channel's own ids live in
``external_id`` text columns and are only interpreted by that channel's
connector code.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
    and_, case, exists, func, select, text,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, column_property, mapped_column, relationship

from backend.db.database import Base

SCHEMA_GENERATION = "2"

SCAM_SCORE_THRESHOLD = 0.5
SCAM_SUCCESS_STATUSES = ("completed", "short_circuited")

CONVERSATION_KINDS = ("direct", "group", "channel", "email_thread")
ATTACHMENT_KINDS = ("photo", "video", "video_note", "voice", "audio", "gif", "document", "sticker")


class SchemaMeta(Base):
    __tablename__ = "schema_meta"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)


# ── Accounts & people ────────────────────────────────────────────────────────

class Account(Base):
    """One signed-in account on one channel."""
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("channel", "external_id", name="uq_account_channel_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String, nullable=False)  # telegram | gmail | signal | sms
    external_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    self_identity_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("identities.id", ondelete="SET NULL", use_alter=True), nullable=True
    )
    # Connector-owned incremental sync state (e.g. Gmail historyId), JSON
    sync_state_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Identity(Base):
    """One handle of a person on one channel (a Telegram user, an email
    address, a Signal account)."""
    __tablename__ = "identities"
    __table_args__ = (UniqueConstraint("channel", "external_id", name="uq_identity_channel_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("people.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String)
    first_name: Mapped[Optional[str]] = mapped_column(String)
    last_name: Mapped[Optional[str]] = mapped_column(String)
    username: Mapped[Optional[str]] = mapped_column(String)
    phone: Mapped[Optional[str]] = mapped_column(String)
    bio: Mapped[Optional[str]] = mapped_column(Text)
    avatar_path: Mapped[Optional[str]] = mapped_column(String)  # relative to DATA_DIR
    is_contact: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    is_self: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ContactPoint(Base):
    """A normalized phone number or email address an identity is reachable
    at; the key for linking one person's identities across channels."""
    __tablename__ = "contact_points"
    __table_args__ = (
        UniqueConstraint("identity_id", "kind", "value", name="uq_contact_point"),
        Index("ix_contact_points_kind_value", "kind", "value"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identity_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("identities.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)  # phone | email
    value: Mapped[str] = mapped_column(String, nullable=False)  # E.164 / lowercased email


class Person(Base):
    """A human, across every channel they use. The CRM record."""
    __tablename__ = "people"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # user override
    twitter_handle: Mapped[Optional[str]] = mapped_column(String)
    linkedin_url: Mapped[Optional[str]] = mapped_column(String)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    is_important: Mapped[bool] = mapped_column(Boolean, default=False)
    is_self: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    relationship_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    relationship_status_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    last_interaction_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_outgoing_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_incoming_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Derived, read-only: in any channel's address book; the best display name
    is_contact: Mapped[bool] = column_property(
        exists().where(Identity.person_id == id, Identity.is_contact == True)  # noqa: E712
        .correlate_except(Identity)
    )
    name: Mapped[Optional[str]] = column_property(
        func.coalesce(
            display_name,
            select(func.coalesce(Identity.display_name, Identity.username, Identity.phone))
            .where(Identity.person_id == id)
            .order_by(Identity.is_contact.desc(), Identity.id)
            .limit(1)
            .correlate_except(Identity)
            .scalar_subquery(),
        )
    )


# ── Conversations & messages ─────────────────────────────────────────────────

class HistoryCoverage(Base):
    """How much of a conversation's history is stored (backend.telegram.archive).

    Invariant: every message with ``walk_cursor_seq <= seq <=
    newest_contiguous_seq`` is stored; with ``history_complete`` the range
    starts at the conversation's first message. Absent row = nothing known.
    """
    __tablename__ = "history_coverage"

    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True, autoincrement=False
    )
    history_complete: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    oldest_seq: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    newest_contiguous_seq: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # Lowest seq the in-progress full walk has stored; resume point after a restart
    walk_cursor_seq: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    last_walk_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_topup_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)


class Conversation(Base):
    """A chat, group, channel or email thread on one account."""
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("account_id", "external_id", name="uq_conversation_account_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # direct | group | channel | email_thread
    title: Mapped[Optional[str]] = mapped_column(String)
    username: Mapped[Optional[str]] = mapped_column(String)
    avatar_path: Mapped[Optional[str]] = mapped_column(String)  # relative to DATA_DIR
    peer_identity_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("identities.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_monitored: Mapped[bool] = mapped_column(Boolean, default=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    is_briefing: Mapped[bool] = mapped_column(Boolean, default=False)
    is_tracked: Mapped[bool] = mapped_column(Boolean, default=False)
    is_forum: Mapped[bool] = mapped_column(Boolean, default=False)
    slow_mode_seconds: Mapped[int] = mapped_column(Integer, default=0)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    # No longer usable (e.g. a Telegram group migrated to a supergroup) — hidden from all lists
    is_deactivated: Mapped[bool] = mapped_column(Boolean, default=False)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    is_deleted_account: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    is_self: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")  # notes to self
    is_muted: Mapped[bool] = mapped_column(Boolean, default=False)
    muted_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_marked_unread: Mapped[bool] = mapped_column(Boolean, default=False)
    unread_count: Mapped[int] = mapped_column(Integer, default=0)
    members_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pinned_message_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("messages.id", ondelete="SET NULL", use_alter=True), nullable=True
    )
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_message_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_message_sender: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # Channel folders / labels (Gmail labels), JSON list of strings
    labels_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Eager (selectin): every serializer needs the channel and the other party.
    account: Mapped["Account"] = relationship(lazy="selectin", viewonly=True)
    peer_identity: Mapped[Optional["Identity"]] = relationship(
        foreign_keys=[peer_identity_id], lazy="selectin", viewonly=True,
    )
    # Derived, read-only: False without a coverage row
    history_complete: Mapped[bool] = column_property(
        func.coalesce(
            select(HistoryCoverage.history_complete)
            .where(HistoryCoverage.conversation_id == id)
            .correlate_except(HistoryCoverage)
            .scalar_subquery(),
            False,
        )
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "external_id", name="uq_message_conversation_external"),
        Index("ix_messages_conversation_seq", "conversation_id", "seq"),
        Index("ix_messages_conversation_date", "conversation_id", "date", "id"),
        Index("ix_messages_date_id", "date", "id"),
        Index("ix_messages_scam_status", "scam_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    # Channel-native ordering within the conversation (Telegram message id;
    # epoch milliseconds for channels without one)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_identity_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("identities.id", ondelete="SET NULL"), nullable=True, index=True
    )
    sender_name: Mapped[Optional[str]] = mapped_column(String)
    subject: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    text: Mapped[Optional[str]] = mapped_column(Text)
    text_html: Mapped[Optional[str]] = mapped_column(Text)
    reactions_json: Mapped[Optional[str]] = mapped_column(Text)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    is_outgoing: Mapped[bool] = mapped_column(Boolean, default=False)
    importance_score: Mapped[Optional[float]] = mapped_column(Float)
    importance_reason: Mapped[Optional[str]] = mapped_column(Text)
    user_flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    reply_to_external_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    my_reaction: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    topic_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    grouped_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    delivery_state: Mapped[Optional[str]] = mapped_column(String, default="sent")  # sending, sent, delivered, read
    is_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    scam_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    scam_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scam_llm_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scam_llm_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scam_category: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scam_trace_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scam_synced_to_cloud: Mapped[bool] = mapped_column(Boolean, default=False)
    scam_manual_verdict: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # 'scam', 'not_scam', or NULL
    # NULL = never scanned; pending | completed | failed | short_circuited.
    # A failed scan never touches scam_score/scam_reason (error goes to scam_error).
    scam_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    scam_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scam_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    scam_next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Cloud change tracking: re-upload when scam_updated_at > scam_synced_at
    scam_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    scam_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 'message' | 'service' (joins, pins, calls…: text holds a readable description)
    kind: Mapped[str] = mapped_column(String, default="message", server_default="message", nullable=False)
    # Structured content for polls, locations, contacts, forwards, service actions (JSON)
    meta_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    fwd_from_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Eager (selectin): every serializer needs a message's files and sender.
    attachments: Mapped[list["MessageAttachment"]] = relationship(
        order_by="MessageAttachment.position", lazy="selectin",
        cascade="all, delete-orphan", passive_deletes=True,
    )
    sender_identity: Mapped[Optional["Identity"]] = relationship(lazy="selectin", viewonly=True)

    # Transient, not mapped: set by crud.save_message to "created" | "updated" | None
    _change = None  # type: Optional[str]

    @hybrid_property
    def pipeline_is_scam(self) -> Optional[bool]:
        """The automated verdict alone: None without a successful scan."""
        if self.scam_status not in SCAM_SUCCESS_STATUSES or self.scam_score is None:
            return None
        return self.scam_score > SCAM_SCORE_THRESHOLD

    @pipeline_is_scam.inplace.expression
    @classmethod
    def _pipeline_is_scam_expression(cls):
        # False (never NULL) when there is no successful scan
        return and_(
            func.coalesce(cls.scam_status, "").in_(SCAM_SUCCESS_STATUSES),
            func.coalesce(cls.scam_score, 0.0) > SCAM_SCORE_THRESHOLD,
        )

    @hybrid_property
    def effective_is_scam(self) -> bool:
        """The one verdict every consumer uses: a manual verdict wins, else a
        successful scan scoring above the threshold."""
        if self.scam_manual_verdict is not None:
            return self.scam_manual_verdict == "scam"
        return bool(self.pipeline_is_scam)

    @effective_is_scam.inplace.expression
    @classmethod
    def _effective_is_scam_expression(cls):
        # Never NULL, so NOT effective_is_scam is also correct in SQL
        return case(
            (cls.scam_manual_verdict.isnot(None), cls.scam_manual_verdict == "scam"),
            else_=cls.pipeline_is_scam,
        )


class MessageAttachment(Base):
    """A file attached to a message. Telegram has at most one per message
    (albums are separate messages); email can have many."""
    __tablename__ = "message_attachments"
    __table_args__ = (UniqueConstraint("message_id", "position", name="uq_attachment_message_position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # see ATTACHMENT_KINDS
    filename: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    mime_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    local_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # relative to DATA_DIR; NULL until downloaded
    external_ref: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # channel handle to fetch it
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MessageParticipant(Base):
    """Addressees of a message beyond its sender (email from/to/cc/bcc)."""
    __tablename__ = "message_participants"
    __table_args__ = (
        UniqueConstraint("message_id", "identity_id", "role", name="uq_message_participant"),
        Index("ix_message_participants_identity", "identity_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    identity_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("identities.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String, nullable=False)  # from | to | cc | bcc


class PersonMergeSuggestion(Base):
    """Two people who may be the same human (never merged automatically)."""
    __tablename__ = "person_merge_suggestions"
    __table_args__ = (UniqueConstraint("person_a_id", "person_b_id", name="uq_merge_suggestion_pair"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # person_a_id < person_b_id
    person_a_id: Mapped[int] = mapped_column(Integer, ForeignKey("people.id", ondelete="CASCADE"), nullable=False)
    person_b_id: Mapped[int] = mapped_column(Integer, ForeignKey("people.id", ondelete="CASCADE"), nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)  # same_name_shared_group | username_matches_email | ...
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")  # pending | dismissed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PersonMerge(Base):
    """Undo log of a merge: which identities (and CRM rows) moved from a
    removed person into the surviving one."""
    __tablename__ = "person_merges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    into_person_id: Mapped[int] = mapped_column(Integer, ForeignKey("people.id", ondelete="CASCADE"), nullable=False)
    from_person_id: Mapped[int] = mapped_column(Integer, nullable=False)  # the removed person's former id
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)  # removed person's fields + moved row ids
    automatic: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    undone_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class CalendarEvent(Base):
    """A calendar event on an account (Google Calendar)."""
    __tablename__ = "calendar_events"
    __table_args__ = (
        UniqueConstraint("account_id", "external_id", name="uq_calendar_event_account_external"),
        Index("ix_calendar_events_start", "start_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    calendar_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    location: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    start_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # naive UTC
    end_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # confirmed | tentative | cancelled
    organizer_identity_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("identities.id", ondelete="SET NULL"), nullable=True
    )
    html_link: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CalendarAttendee(Base):
    __tablename__ = "calendar_attendees"
    __table_args__ = (UniqueConstraint("event_id", "identity_id", name="uq_calendar_attendee"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("calendar_events.id", ondelete="CASCADE"), nullable=False
    )
    identity_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("identities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    response_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # accepted | declined | tentative | needsAction
    is_organizer: Mapped[bool] = mapped_column(Boolean, default=False)


# ── Tags, workspaces, organizations ──────────────────────────────────────────

class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (Index("uq_tags_name_ci", text("lower(trim(name))"), unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    color: Mapped[str] = mapped_column(String, default="slate")
    category: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ConversationTag(Base):
    __tablename__ = "conversation_tags"
    __table_args__ = (UniqueConstraint("conversation_id", "tag_id", name="uq_conversation_tag"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    tag_id: Mapped[int] = mapped_column(Integer, ForeignKey("tags.id", ondelete="CASCADE"), nullable=False, index=True)


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    emoji: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    color: Mapped[str] = mapped_column(String, default="indigo")
    match_mode: Mapped[str] = mapped_column(String, default="any")  # 'any' | 'all'
    view_mode: Mapped[str] = mapped_column(String, default="list")  # 'list' | 'kanban'
    position: Mapped[int] = mapped_column(Integer, default=0)
    # Unallocated-column cutoff: only incoming activity after this surfaces for triage
    board_watermark: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WorkspaceTag(Base):
    __tablename__ = "workspace_tags"
    __table_args__ = (UniqueConstraint("workspace_id", "tag_id", name="uq_workspace_tag"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tag_id: Mapped[int] = mapped_column(Integer, ForeignKey("tags.id", ondelete="CASCADE"), nullable=False)
    is_exclusion: Mapped[bool] = mapped_column(Boolean, default=False)


class WorkspaceBoard(Base):
    __tablename__ = "workspace_boards"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_workspace_board_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    # Stream tag scoping this board; NULL = unscoped (all workspace members)
    scope_tag_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("tags.id", ondelete="SET NULL"), nullable=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)


class WorkspaceColumn(Base):
    __tablename__ = "workspace_columns"
    __table_args__ = (UniqueConstraint("workspace_id", "tag_id", name="uq_workspace_column"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tag_id: Mapped[int] = mapped_column(Integer, ForeignKey("tags.id", ondelete="CASCADE"), nullable=False)  # stage tag
    board_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("workspace_boards.id", ondelete="CASCADE"), nullable=True
    )
    # Display name; may differ from the tag name so boards can share labels
    # ("Done" on two boards) while keeping independent pipeline state
    label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    color: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class WorkspaceConversationOverride(Base):
    __tablename__ = "workspace_conversation_overrides"
    __table_args__ = (
        UniqueConstraint("workspace_id", "conversation_id", name="uq_workspace_conversation_override"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    needs_triage_since: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    triaged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (Index("uq_organizations_name_ci", text("lower(trim(name))"), unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    context: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PersonPosition(Base):
    __tablename__ = "person_positions"
    __table_args__ = (UniqueConstraint("person_id", "organization_id", name="uq_person_org"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("people.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Source of truth for membership. NULL = detached (the org was deleted with
    # mode=detach); the position keeps its history under `organization`.
    organization_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Display name: mirrors organizations.name while attached, frozen once detached
    organization: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)  # True = current, False = former
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # when they left
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PersonNote(Base):
    """Context / due-diligence note about a person. Written by the user or an
    MCP agent."""
    __tablename__ = "person_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("people.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)  # context|due_diligence|risk|relationship|todo
    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")  # markdown
    citations_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # [{conversation_id, message_id}]
    author: Mapped[str] = mapped_column(String, nullable=False, default="user")  # user | agent
    agent_label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ── Intelligence ─────────────────────────────────────────────────────────────

class AIConversation(Base):
    """A conversation with the built-in LLM assistant (Intelligence page)."""
    __tablename__ = "ai_conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # null = untitled/pending auto-suggest
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AIConversationMessage(Base):
    __tablename__ = "ai_conversation_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ai_conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ai_conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String, nullable=False)  # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CatchUpItem(Base):
    __tablename__ = "catchup_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    person_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("people.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, default="pending")  # pending, handled, snoozed
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    snoozed_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    handled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CatchUpSummary(Base):
    __tablename__ = "catchup_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    summary_messages_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    recent_messages_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    action_needed: Mapped[bool] = mapped_column(Boolean, default=False)
    total_messages_at_generation: Mapped[int] = mapped_column(Integer, default=0)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Rule-based fallback written because the LLM failed; regenerated once it is back.
    is_degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")


class BriefingRun(Base):
    __tablename__ = "briefing_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    days: Mapped[int] = mapped_column(Integer, default=1)
    # running | done | partial (some entries failed / run aborted after ≥1 entry) | failed
    status: Mapped[str] = mapped_column(String, nullable=False, default="done", server_default="done")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class BriefingEntry(Base):
    __tablename__ = "briefing_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("briefing_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    # The person an 'important_contact' entry is about (they may span several
    # conversations, so conversation_id is NULL for those entries)
    person_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("people.id", ondelete="SET NULL"), nullable=True
    )
    conversation_title: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)  # 'tracked' | 'briefing' | 'important_contact'
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    sources_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # [{conversation_id, message_id}]


class AgentReview(Base):
    """How far an MCP agent has reviewed a conversation. It counts as
    reviewed only while its history is complete, nothing newer than
    reviewed_through_seq is stored, and no older message appeared since
    (count of stored seqs <= reviewed_through_seq still <= reviewed_message_count)."""
    __tablename__ = "agent_reviews"

    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True, autoincrement=False
    )
    reviewed_through_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reviewed_message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    agent_label: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class AdminActionLog(Base):
    """Audit trail of admin/moderation actions taken from the app."""
    __tablename__ = "admin_action_log"
    __table_args__ = (Index("ix_admin_action_log_conversation", "conversation_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String, nullable=False)
    target_identity_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("identities.id", ondelete="SET NULL"), nullable=True
    )
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


# ── Shield (scam detection) ──────────────────────────────────────────────────

class ScamCheckRequest(Base):
    __tablename__ = "scam_check_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    request_type: Mapped[str] = mapped_column(String, nullable=False)  # are_flagged_scams, is_this_scam
    status: Mapped[str] = mapped_column(String, default="pending")  # pending, awaiting_followup, completed, failed, expired
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    response_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    related_conversation_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    related_message_ids_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # internal message ids
    related_identity_ids_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    followup_request_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ScamFlag(Base):
    """Per-sender verdict record."""
    __tablename__ = "scam_flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identity_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("identities.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    source: Mapped[str] = mapped_column(String, nullable=False)  # local, cloud, manual
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    request_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    flagged_message_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    flagged_conversation_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    flagged_message_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    flagged_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    synced_to_cloud: Mapped[bool] = mapped_column(Boolean, default=False)
    # 'active' = flagged scammer, 'dismissed' = the user cleared this sender;
    # automated flagging never re-activates it.
    status: Mapped[str] = mapped_column(String, default="active", server_default="active")
    dismissed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ScamReport(Base):
    __tablename__ = "scam_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    reported_identity_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("identities.id", ondelete="SET NULL"), nullable=True
    )
    reported_in_conversation_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    chat_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    reporter_verdict: Mapped[str] = mapped_column(String, nullable=False)
    scam_category: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    custom_category: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    messages_json: Mapped[str] = mapped_column(Text, nullable=False)
    local_detection_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="submitted")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ScamDismissal(Base):
    __tablename__ = "scam_dismissals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dismissal_type: Mapped[str] = mapped_column(String, nullable=False)  # scammer, scan
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)  # full captured data as JSON
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ScamFeedback(Base):
    __tablename__ = "scam_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feedback_type: Mapped[str] = mapped_column(String, nullable=False)  # general, false_positive, missed_scam, feature_request, bug
    message: Mapped[str] = mapped_column(Text, nullable=False)
    related_conversation_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    related_message_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    related_flag_identity_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("identities.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, default="pending")  # pending, synced
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ScamAnalytics(Base):
    __tablename__ = "scam_analytics"
    __table_args__ = (Index("uq_scam_analytics_period", "period_start", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    period_end: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Detection counters
    total_scanned: Mapped[int] = mapped_column(Integer, default=0)
    scam_detected: Mapped[int] = mapped_column(Integer, default=0)
    safe_detected: Mapped[int] = mapped_column(Integer, default=0)
    short_circuited_ml: Mapped[int] = mapped_column(Integer, default=0)
    short_circuited_signatures: Mapped[int] = mapped_column(Integer, default=0)
    pipeline_errors: Mapped[int] = mapped_column(Integer, default=0)
    total_pipeline_ms: Mapped[int] = mapped_column(Integer, default=0)  # sum, divide by total_scanned for avg

    # Category breakdown (JSON: {"phishing": 5, ...})
    categories_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ML prefilter breakdown
    ml_safe: Mapped[int] = mapped_column(Integer, default=0)
    ml_borderline: Mapped[int] = mapped_column(Integer, default=0)
    ml_auto_scam: Mapped[int] = mapped_column(Integer, default=0)
    ml_unavailable: Mapped[int] = mapped_column(Integer, default=0)

    # Tool usage
    tool_calls_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # {"search_scam_scenarios": 50, ...}
    scenarios_matched_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # {"fake_reconnection": 3, ...}
    safe_patterns_matched_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # {"professional_title": 8, ...}

    # User actions
    flags_dismissed: Mapped[int] = mapped_column(Integer, default=0)
    messages_unflagged: Mapped[int] = mapped_column(Integer, default=0)
    reports_submitted: Mapped[int] = mapped_column(Integer, default=0)
    manual_checks: Mapped[int] = mapped_column(Integer, default=0)
    rescans: Mapped[int] = mapped_column(Integer, default=0)

    # Conversation kind breakdown
    scans_direct: Mapped[int] = mapped_column(Integer, default=0)
    scans_group: Mapped[int] = mapped_column(Integer, default=0)
    scans_channel: Mapped[int] = mapped_column(Integer, default=0)

    # System info (JSON)
    system_info_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
