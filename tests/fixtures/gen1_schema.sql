-- Schema (no data) of the last generation-1 Mingly database (migrations up to 94),
-- the source format of backend/db/legacy_import.py. Used by tests/test_legacy_import.py.

CREATE TABLE admin_action_log (  id INTEGER PRIMARY KEY AUTOINCREMENT,  chat_telegram_id INTEGER NOT NULL,  action TEXT NOT NULL,  target_user_id INTEGER,  details TEXT,  created_at DATETIME NOT NULL);

CREATE TABLE agent_reviews (
	chat_id BIGINT NOT NULL, 
	reviewed_through_tg_id BIGINT NOT NULL, 
	reviewed_message_count INTEGER NOT NULL, 
	reviewed_at DATETIME NOT NULL, 
	agent_label VARCHAR, 
	PRIMARY KEY (chat_id)
);

CREATE TABLE briefing_entries (
	id INTEGER NOT NULL, 
	run_id INTEGER NOT NULL, 
	chat_id BIGINT NOT NULL, 
	chat_name VARCHAR NOT NULL, 
	category VARCHAR NOT NULL, 
	summary TEXT NOT NULL, 
	sources_json TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(run_id) REFERENCES briefing_runs (id) ON DELETE CASCADE
);

CREATE TABLE briefing_runs (
	id INTEGER NOT NULL, 
	generated_at DATETIME NOT NULL, 
	days INTEGER NOT NULL, status VARCHAR NOT NULL DEFAULT 'done', error TEXT, finished_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE TABLE catchup_items (
	id INTEGER NOT NULL, 
	chat_telegram_id BIGINT NOT NULL, 
	contact_id INTEGER, 
	status VARCHAR NOT NULL, 
	note TEXT, 
	snoozed_until DATETIME, 
	handled_at DATETIME, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_catchup_chat UNIQUE (chat_telegram_id)
);

CREATE TABLE catchup_summaries (
	id INTEGER NOT NULL, 
	chat_telegram_id BIGINT NOT NULL, 
	summary TEXT NOT NULL, 
	summary_messages_json TEXT, 
	recent_messages_json TEXT, 
	last_activity_at DATETIME, 
	action_needed BOOLEAN NOT NULL, 
	total_messages_at_generation INTEGER NOT NULL, 
	generated_at DATETIME NOT NULL, is_degraded BOOLEAN NOT NULL DEFAULT 0, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_catchup_summary_chat UNIQUE (chat_telegram_id)
);

CREATE TABLE chat_history_coverage (
	chat_id BIGINT NOT NULL, 
	history_complete BOOLEAN DEFAULT '0' NOT NULL, 
	oldest_tg_id BIGINT, 
	newest_contiguous_tg_id BIGINT, 
	walk_cursor_tg_id BIGINT, 
	last_walk_at DATETIME, 
	last_topup_at DATETIME, 
	error TEXT, 
	attempts INTEGER DEFAULT '0' NOT NULL, 
	PRIMARY KEY (chat_id)
);

CREATE TABLE chat_tags (
	id INTEGER NOT NULL, 
	chat_id BIGINT NOT NULL, 
	tag_id INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_chat_tag UNIQUE (chat_id, tag_id), 
	FOREIGN KEY(tag_id) REFERENCES tags (id)
);

CREATE TABLE chats (
	id INTEGER NOT NULL, 
	telegram_id BIGINT NOT NULL, 
	name VARCHAR, 
	type VARCHAR, 
	username VARCHAR, 
	is_monitored BOOLEAN NOT NULL, 
	is_hidden BOOLEAN NOT NULL, 
	is_briefing BOOLEAN NOT NULL, 
	is_tracked BOOLEAN NOT NULL, 
	is_forum BOOLEAN NOT NULL, 
	slow_mode_seconds INTEGER NOT NULL, 
	is_pinned BOOLEAN NOT NULL, 
	is_archived BOOLEAN NOT NULL, 
	is_muted BOOLEAN NOT NULL, 
	muted_until DATETIME, 
	is_marked_unread BOOLEAN NOT NULL, 
	unread_count INTEGER NOT NULL, 
	members_count INTEGER, 
	last_activity_at DATETIME, 
	last_synced_at DATETIME, 
	last_message_id BIGINT, 
	pinned_message_id BIGINT, 
	last_message_text TEXT, 
	last_message_sender VARCHAR, 
	last_message_date DATETIME, 
	created_at DATETIME NOT NULL, is_deactivated BOOLEAN NOT NULL DEFAULT 0, is_bot BOOLEAN NOT NULL DEFAULT 0, is_deleted_account BOOLEAN NOT NULL DEFAULT 0, is_self BOOLEAN NOT NULL DEFAULT 0, 
	PRIMARY KEY (id), 
	UNIQUE (telegram_id)
);

CREATE TABLE "contact_positions" (  id INTEGER PRIMARY KEY AUTOINCREMENT,  telegram_id BIGINT NOT NULL,  organization_id INTEGER REFERENCES organizations (id),  organization VARCHAR NOT NULL,  title VARCHAR,  is_active INTEGER NOT NULL DEFAULT 1,  ended_at TEXT,  created_at DATETIME NOT NULL DEFAULT (datetime('now')),  CONSTRAINT uq_telegram_org UNIQUE (telegram_id, organization_id));

CREATE TABLE contacts (
	id INTEGER NOT NULL, 
	telegram_id BIGINT NOT NULL, 
	first_name VARCHAR, 
	last_name VARCHAR, 
	username VARCHAR, 
	phone VARCHAR, 
	twitter_handle VARCHAR, 
	linkedin_url VARCHAR, 
	notes TEXT, 
	is_important BOOLEAN NOT NULL, 
	relationship_status VARCHAR, 
	relationship_status_manual BOOLEAN NOT NULL, 
	last_interaction_at DATETIME, 
	last_outgoing_at DATETIME, 
	last_incoming_at DATETIME, 
	synced_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (telegram_id)
);

CREATE TABLE conversation_messages (
	id INTEGER NOT NULL, 
	conversation_id INTEGER NOT NULL, 
	role VARCHAR NOT NULL, 
	content TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(conversation_id) REFERENCES conversations (id) ON DELETE CASCADE
);

CREATE TABLE conversations (
	id INTEGER NOT NULL, 
	title VARCHAR, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE messages (
	id INTEGER NOT NULL, 
	tg_message_id BIGINT NOT NULL, 
	chat_id BIGINT NOT NULL, 
	sender_id BIGINT, 
	sender_name VARCHAR, 
	text TEXT, 
	text_html TEXT, 
	reactions_json TEXT, 
	date DATETIME NOT NULL, 
	is_outgoing BOOLEAN NOT NULL, 
	media_type VARCHAR, 
	media_path VARCHAR, 
	importance_score FLOAT, 
	importance_reason TEXT, 
	user_flagged BOOLEAN NOT NULL, 
	reply_to_tg_message_id BIGINT, 
	my_reaction VARCHAR, 
	topic_id INTEGER, 
	is_pinned BOOLEAN NOT NULL, 
	media_filename VARCHAR, 
	media_size INTEGER, 
	grouped_id BIGINT, 
	delivery_state VARCHAR, 
	is_edited BOOLEAN NOT NULL, 
	scam_score FLOAT, 
	scam_reason TEXT, 
	scam_llm_prompt TEXT, 
	scam_llm_response TEXT, 
	scam_category TEXT, 
	scam_trace_json TEXT, 
	scam_synced_to_cloud BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, reply_to_telegram_id INTEGER, scam_manual_verdict TEXT, kind VARCHAR NOT NULL DEFAULT 'message', meta_json TEXT, fwd_from_name VARCHAR, scam_status VARCHAR, scam_error TEXT, scam_attempts INTEGER NOT NULL DEFAULT 0, scam_next_retry_at DATETIME, scam_updated_at DATETIME, scam_synced_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_message_tgmsgid_chat UNIQUE (tg_message_id, chat_id)
);

CREATE TABLE organizations (
	id INTEGER NOT NULL, 
	name VARCHAR NOT NULL, 
	context TEXT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

CREATE TABLE person_notes (
	id INTEGER NOT NULL, 
	telegram_id BIGINT NOT NULL, 
	kind VARCHAR NOT NULL, 
	title VARCHAR NOT NULL, 
	body TEXT NOT NULL, 
	citations_json TEXT, 
	author VARCHAR NOT NULL, 
	agent_label VARCHAR, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE scam_analytics (
	id INTEGER NOT NULL, 
	period_start DATETIME NOT NULL, 
	period_end DATETIME, 
	synced_at DATETIME, 
	total_scanned INTEGER NOT NULL, 
	scam_detected INTEGER NOT NULL, 
	safe_detected INTEGER NOT NULL, 
	short_circuited_ml INTEGER NOT NULL, 
	short_circuited_signatures INTEGER NOT NULL, 
	pipeline_errors INTEGER NOT NULL, 
	total_pipeline_ms INTEGER NOT NULL, 
	categories_json TEXT, 
	ml_safe INTEGER NOT NULL, 
	ml_borderline INTEGER NOT NULL, 
	ml_auto_scam INTEGER NOT NULL, 
	ml_unavailable INTEGER NOT NULL, 
	tool_calls_json TEXT, 
	scenarios_matched_json TEXT, 
	safe_patterns_matched_json TEXT, 
	flags_dismissed INTEGER NOT NULL, 
	messages_unflagged INTEGER NOT NULL, 
	reports_submitted INTEGER NOT NULL, 
	manual_checks INTEGER NOT NULL, 
	rescans INTEGER NOT NULL, 
	scans_private INTEGER NOT NULL, 
	scans_group INTEGER NOT NULL, 
	scans_channel INTEGER NOT NULL, 
	system_info_json TEXT, 
	PRIMARY KEY (id)
);

CREATE TABLE scam_check_requests (
	id INTEGER NOT NULL, 
	request_id VARCHAR NOT NULL, 
	request_type VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	payload_json TEXT NOT NULL, 
	response_json TEXT, 
	related_chat_id BIGINT, 
	related_message_ids_json TEXT, 
	related_telegram_ids_json TEXT, 
	followup_request_json TEXT, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	completed_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (request_id)
);

CREATE TABLE scam_dismissals (
	id INTEGER NOT NULL, 
	dismissal_type VARCHAR NOT NULL, 
	payload_json TEXT NOT NULL, 
	synced_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE scam_feedback (
	id INTEGER NOT NULL, 
	feedback_type VARCHAR NOT NULL, 
	message TEXT NOT NULL, 
	related_chat_id BIGINT, 
	related_message_id BIGINT, 
	related_flag_telegram_id BIGINT, 
	status VARCHAR NOT NULL, 
	synced_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE scam_flags (
	id INTEGER NOT NULL, 
	telegram_id BIGINT NOT NULL, 
	source VARCHAR NOT NULL, 
	confidence FLOAT, 
	reason TEXT, 
	category VARCHAR, 
	request_id VARCHAR, 
	flagged_message_text TEXT, 
	flagged_message_chat_id BIGINT, 
	flagged_message_id BIGINT, 
	flagged_at DATETIME NOT NULL, 
	synced_to_cloud BOOLEAN NOT NULL, status VARCHAR NOT NULL DEFAULT 'active', dismissed_at DATETIME, updated_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (telegram_id)
);

CREATE TABLE scam_reports (
	id INTEGER NOT NULL, 
	report_id VARCHAR NOT NULL, 
	reported_telegram_id BIGINT NOT NULL, 
	reported_in_chat_id BIGINT NOT NULL, 
	chat_type VARCHAR, 
	reporter_verdict VARCHAR NOT NULL, 
	scam_category VARCHAR, 
	custom_category VARCHAR, 
	messages_json TEXT NOT NULL, 
	local_detection_json TEXT, 
	status VARCHAR NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	synced_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (report_id)
);

CREATE TABLE schema_migrations (  version INTEGER PRIMARY KEY,  description TEXT NOT NULL,  applied_at DATETIME NOT NULL);

CREATE TABLE sync_text_repair (chat_id BIGINT PRIMARY KEY);

CREATE TABLE tags (
	id INTEGER NOT NULL, 
	name VARCHAR NOT NULL, 
	color VARCHAR NOT NULL, 
	category VARCHAR, 
	is_default BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

CREATE TABLE workspace_boards (
	id INTEGER NOT NULL, 
	workspace_id INTEGER NOT NULL, 
	name VARCHAR NOT NULL, 
	scope_tag_id INTEGER, 
	position INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_workspace_board_name UNIQUE (workspace_id, name)
);

CREATE TABLE workspace_chat_overrides (
	id INTEGER NOT NULL, 
	workspace_id INTEGER NOT NULL, 
	chat_id BIGINT NOT NULL, 
	needs_triage_since DATETIME, 
	triaged_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_workspace_chat_override UNIQUE (workspace_id, chat_id)
);

CREATE TABLE workspace_columns (
	id INTEGER NOT NULL, 
	workspace_id INTEGER NOT NULL, 
	tag_id INTEGER NOT NULL, 
	position INTEGER NOT NULL, 
	color VARCHAR, board_id INTEGER, label TEXT, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_workspace_column UNIQUE (workspace_id, tag_id)
);

CREATE TABLE workspace_tags (
	id INTEGER NOT NULL, 
	workspace_id INTEGER NOT NULL, 
	tag_id INTEGER NOT NULL, 
	is_exclusion BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_workspace_tag UNIQUE (workspace_id, tag_id)
);

CREATE TABLE workspaces (
	id INTEGER NOT NULL, 
	name VARCHAR NOT NULL, 
	emoji VARCHAR, 
	color VARCHAR NOT NULL, 
	match_mode VARCHAR NOT NULL, 
	view_mode VARCHAR NOT NULL, 
	position INTEGER NOT NULL, 
	board_watermark DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_admin_action_log_chat ON admin_action_log (chat_telegram_id, created_at);

CREATE INDEX ix_briefing_entries_run ON briefing_entries (run_id);

CREATE INDEX ix_contact_positions_organization_id ON contact_positions (organization_id);

CREATE INDEX ix_contact_positions_telegram_id ON contact_positions (telegram_id);

CREATE INDEX ix_conversation_messages_conv ON conversation_messages (conversation_id, created_at);

CREATE INDEX ix_messages_chat_date ON messages (chat_id, date DESC);

CREATE INDEX ix_messages_chat_date_tgid ON messages (chat_id, date, tg_message_id);

CREATE INDEX ix_messages_chat_tgmsgid ON messages (chat_id, tg_message_id);

CREATE INDEX ix_messages_date_id ON messages (date DESC, id DESC);

CREATE INDEX ix_messages_scam_status ON messages (scam_status);

CREATE INDEX ix_messages_sender_id ON messages (sender_id);

CREATE INDEX ix_person_notes_telegram_id ON person_notes (telegram_id);

CREATE INDEX ix_workspace_boards_workspace ON workspace_boards (workspace_id);

CREATE INDEX ix_workspace_columns_workspace ON workspace_columns (workspace_id);

CREATE INDEX ix_workspace_overrides_workspace ON workspace_chat_overrides (workspace_id);

CREATE INDEX ix_workspace_tags_workspace ON workspace_tags (workspace_id);

CREATE UNIQUE INDEX uq_message_tgmsgid_chat ON messages (tg_message_id, chat_id);

CREATE UNIQUE INDEX uq_organizations_name_ci ON organizations (lower(trim(name)));

CREATE UNIQUE INDEX uq_scam_analytics_period ON scam_analytics (period_start);

CREATE UNIQUE INDEX uq_tags_name_ci ON tags (lower(trim(name)));

