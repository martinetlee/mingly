export interface AuthStatus {
  authenticated: boolean
  username?: string
  first_name?: string
  phone?: string
}

export interface Folder {
  id: number
  title: string
  chat_ids: number[]
  include_contacts: boolean
  include_non_contacts: boolean
  include_groups: boolean
  include_channels: boolean
  include_bots: boolean
}

export interface Chat {
  id: number
  telegram_id: number
  name: string
  type: 'private' | 'group' | 'channel'
  username: string | null
  is_monitored: boolean
  is_hidden: boolean
  is_briefing: boolean
  is_tracked: boolean
  is_forum?: boolean
  slow_mode_seconds?: number
  is_pinned: boolean
  is_archived: boolean
  is_muted: boolean
  muted_until: string | null
  is_marked_unread: boolean
  unread_count?: number
  last_synced_at: string | null
  last_activity_at?: string | null
  last_message?: {
    text: string
    sender_name: string | null
    date: string | null
  } | null
  is_bot?: boolean
  is_deleted_account?: boolean
  /** Every message from the chat's first up to the newest is stored locally */
  history_complete?: boolean
}

export interface Tag {
  id: number
  name: string
  color: string
  category: string | null
  is_default: boolean
  chat_count?: number
}

export interface Workspace {
  id: number
  name: string
  emoji: string | null
  color: string
  match_mode: 'any' | 'all'
  view_mode: 'list' | 'kanban'
  position: number
  tag_ids: number[]
  member_count: number
  unallocated_count: number
  main_board_id?: number | null
}

export interface BoardColumn {
  tag_id: number
  name: string
  color: string
  position: number
  chats: Chat[]
}

export interface WorkspaceBoard {
  id: number
  name: string
  scope_tag_id: number | null
  scope_tag_name: string | null
  position: number
  unallocated_count: number
}

export interface BoardData {
  workspace: { id: number; name: string; emoji: string | null; color: string; view_mode: string }
  boards: WorkspaceBoard[]
  board_id: number | null
  columns: BoardColumn[]
  unallocated: Chat[]
  backlog: { active: Chat[]; cooling: Chat[]; dormant: Chat[] }
  classified_chat_ids: number[]
}

export interface TagSuggestion {
  name: string
  color: string
  reason: string
}

export interface Reaction {
  emoji: string
  count: number
}

export interface ReplyPreview {
  sender_name: string | null
  text: string | null
  media_type: string | null
}

export interface Message {
  id: number
  tg_message_id: number
  chat_id: number
  sender_id: number | null
  sender_name: string | null
  text: string | null
  text_html: string | null
  reactions: Reaction[] | null
  date: string
  is_outgoing: boolean
  media_type: string | null
  media_url: string | null
  importance_score: number | null
  user_flagged: boolean
  reply_to_tg_message_id?: number | null
  reply_to_preview?: ReplyPreview | null
  my_reaction?: string | null
  topic_id?: number | null
  is_pinned?: boolean
  media_filename?: string | null
  media_size?: number | null
  grouped_id?: number | null
  delivery_state?: 'sending' | 'sent' | 'delivered' | 'read'
  is_edited?: boolean
  scam_score?: number | null
  scam_reason?: string | null
  scam_manual_verdict?: ScamManualVerdict
  scam_category?: string | null
  scam_status?: ScamStatus
  scam_error?: string | null
  /** THE verdict (manual wins, else a successful scan > 0.5). Never re-derive it from scam_score. */
  effective_is_scam?: boolean
  /** 'service' = a system line (joined, pinned, call…); `text` holds the readable description */
  kind?: 'message' | 'service'
  /** Structured content of polls, locations, contacts, forwards and service actions */
  meta?: Record<string, unknown> | null
  fwd_from_name?: string | null
}

export type ScamManualVerdict = 'scam' | 'not_scam' | null
/** null = never scanned; pending = queued with no earlier result */
export type ScamStatus = null | 'pending' | 'completed' | 'short_circuited' | 'failed'

/** The scam fields every scam API response / WS `messages[]` entry carries (backend scam_fields()). */
export interface ScamFields {
  /** Stored pipeline score (0.5 ± conf/2); null = no successful scan */
  scam_score: number | null
  scam_reason: string | null
  scam_category: string | null
  scam_manual_verdict: ScamManualVerdict
  scam_status: ScamStatus
  /** Last check error; can be set while status is completed (a re-check failed, earlier result kept) */
  scam_error: string | null
  effective_is_scam: boolean
}

/** Stored row state after a check/verdict change, keyed by (chat_id, tg_message_id). */
export type ScamMsgState = ScamFields & { chat_id: number; tg_message_id: number; sender_id: number | null }

export type ScamFlagStatus = 'active' | 'dismissed'
export interface ScamFlagChange { telegram_id: number; status: ScamFlagStatus | null }

export interface ScamVerdictResponse {
  status: string
  scope: 'message' | 'sender'
  verdict: ScamManualVerdict
  count: number
  messages: ScamMsgState[]
  flags: ScamFlagChange[]
}

export type ScamCheckSource = 'manual' | 'rescan' | 'realtime' | 'edit' | 'resume' | 'scan'

export interface ScamFlag {
  telegram_id: number
  sender_name?: string | null
  status: ScamFlagStatus
  source: 'local' | 'cloud' | 'manual'
  confidence: number | null
  reason: string | null
  flagged_at: string
  dismissed_at?: string | null
  last_chat_id?: number | null
  last_chat_name?: string | null
  last_message_id?: number | null
  last_active?: string | null
  last_message?: string | null
  category?: string | null
  flagged_message_text?: string | null
  flagged_message_chat_id?: number | null
  flagged_message_id?: number | null
  synced_to_cloud?: boolean
}

export interface ScannedMessage extends ScamFields {
  tg_message_id: number
  chat_id: number
  chat_name: string
  sender_id: number | null
  sender_name: string | null
  text: string
  date: string | null
  scam_attempts: number
  scam_next_retry_at: string | null
  scam_updated_at: string | null
  llm_prompt: string | null
  llm_response: string | null
  trace_json: string | null
  /** true only if the latest change was uploaded */
  synced_to_cloud: boolean
}

export interface ScamQueueStats { queued: number; running: number; workers: number }

/** POST /api/scam/pipeline/sync result (status: success | partial | failed | not_configured | reloaded | no_pipeline) */
export interface CloudSyncResult {
  status: string
  error?: string | null
  errors?: string[]
  files_updated?: string[]
  reports_submitted?: number
  flags_submitted?: number
  scan_results_submitted?: number
  dismissals_submitted?: number
  analytics_submitted?: number
  version?: string
  /** legacy fields some callers still read */
  pipeline_updated?: boolean
  scenarios_updated?: boolean
  signatures_updated?: boolean
}

/** verdict_filter values accepted by GET /api/scam/scanned-messages (+ export) */
export type ScamVerdictFilter = 'scam' | 'not_scam' | 'failed' | 'pending' | 'manual' | 'unmarked' | 'all'

export interface ScamCategory {
  id: string
  label: string
  description: string
}

export interface ReportContextMessage {
  tg_message_id: number
  sender_id: number | null
  sender_name: string | null
  text: string | null
  date: string | null
  is_outgoing: boolean
  scam_score: number | null
}

export interface ScamReport {
  id: number
  report_id: string
  reported_telegram_id: number
  reported_sender_name: string | null
  reported_in_chat_id: number
  chat_name: string
  chat_type: string | null
  reporter_verdict: string
  scam_category: string | null
  custom_category: string | null
  status: 'submitted' | 'confirmed' | 'false_positive'
  synced_at: string | null
  created_at: string
}

export interface SyncHistoryStats {
  last_synced_at: string | null
  last_sync_status: 'success' | 'partial' | 'failed' | null
  last_sync_error: string | null
  last_sync_details: {
    files_updated: string[]
    reports_submitted: number
    flags_submitted: number
    scan_results_submitted: number
    dismissals_submitted?: number
  } | null
  flags: { total: number; synced: number; pending: number }
  reports: { total: number; synced: number; pending: number }
  scans: { total: number; synced: number; pending: number; total_scanned: number; scam_detected: number; failed: number; queued?: number }
  dismissals: { total: number; synced: number; pending: number }
  /** A cloud sync is in flight (single-flight on the server) */
  running?: boolean
  analytics: {
    total: number
    synced: number
    pending: number
    latest: {
      period_start: string
      total_scanned: number
      scam_detected: number
      safe_detected: number
      pipeline_errors: number
      avg_pipeline_ms: number
    } | null
  }
}

export interface ScamFeedback {
  id: number
  feedback_type: string
  message: string
  related_chat_id: number | null
  related_message_id: number | null
  related_flag_telegram_id: number | null
  status: string
  synced_at: string | null
  created_at: string | null
}

export interface PinnedMessage {
  tg_message_id: number
  sender_name: string | null
  text: string | null
  media_type: string | null
  media_url: string | null
  date: string
}

export interface ContactPosition {
  id: number
  telegram_id?: number
  /** null = detached (org deleted with mode=detach; the name is kept as history) */
  organization_id: number | null
  organization: string
  title: string | null
  is_active: boolean
  ended_at: string | null
}

export type RelationshipStatus = 'new' | 'active' | 'nurturing' | 'stalled' | 'cold' | 'needs_reply'

export interface Contact {
  id: number
  telegram_id: number
  first_name: string | null
  last_name: string | null
  name: string
  username: string | null
  phone: string | null
  twitter_handle: string | null
  linkedin_url: string | null
  notes: string | null
  is_important: boolean
  relationship_status: RelationshipStatus | null
  relationship_status_manual: boolean
  last_interaction_at: string | null
  last_outgoing_at: string | null
  last_incoming_at: string | null
  synced_at: string | null
  positions: ContactPosition[]
  status_explanation?: string
  recommended_action?: string
  status_source?: 'auto' | 'manual'
  attention_reason?: string
  days_since_interaction?: number | null
  days_since_outgoing?: number | null
  days_since_incoming?: number | null
}

/** GET /api/contacts — server-side filtered page */
export interface ContactsPage {
  items: Contact[]
  total: number
  limit: number
  offset: number
  /** Matches of `search` alone per status ("none" = no status) */
  status_counts: Record<string, number>
  /** Important contacts matching `search` (Important tab badge) */
  important_count?: number
}

/** GET /api/people/{telegram_id} — local data only, works offline */
export interface PersonDetail {
  telegram_id: number
  name: string
  contact: Contact | null
  positions: ContactPosition[]
}

export interface PersonSummary {
  telegram_id: number
  name: string
  username: string | null
  is_important: boolean
  contact_id: number | null
  relationship_status: string | null
  last_interaction_at: string | null
  primary_position: { title: string | null; organization: string; organization_id?: number | null } | null
  phone: string | null
  twitter_handle: string | null
  linkedin_url: string | null
  has_notes: boolean
}

export interface DashboardStats {
  unread_today: number
}

export interface DashboardNeedsReply {
  chat: Chat
  last_message: {
    sender_name: string | null
    text: string | null
    date: string
  }
  /** Triage record (null = never triaged). Handled/snoozed filtering is done server-side. */
  triage: CatchUpItem | null
  /** Inline catch-up context (summary null = not generated yet) */
  context: CatchUpContext | null
}

export interface DashboardAttentionContact {
  id: number
  telegram_id: number
  name: string
  username: string | null
  is_important: boolean
  relationship_status: string | null
  relationship_status_manual?: boolean
  last_interaction_at: string | null
  primary_position: { title: string | null; organization: string } | null
  status_explanation?: string
  recommended_action?: string
  status_source?: 'auto' | 'manual'
  attention_reason?: string
  days_since_interaction?: number | null
}

export interface DashboardBriefingEntry {
  id: number
  chat_id: number
  chat_name: string
  category: 'tracked' | 'briefing' | 'important_contact'
  summary: string
  /** Citation targets for `[n]` markers (sources[n-1]) */
  sources?: BriefingSource[]
}

export interface DashboardBriefing {
  id: number
  generated_at: string
  days: number
  entries: DashboardBriefingEntry[]
}

export interface DashboardData {
  stats: DashboardStats
  needs_reply: DashboardNeedsReply[]
  /** Handled items still unanswered (≤10), newest handled first */
  recently_handled?: DashboardNeedsReply[]
  attention_contacts: DashboardAttentionContact[]
  latest_briefing: DashboardBriefing | null
}

export interface SearchMessageResult {
  tg_message_id: number
  chat_id: number
  chat_name: string
  sender_name: string | null
  text: string
  date: string
}

export interface SearchOrgResult {
  id: number
  name: string
  context: string | null
}

export interface GlobalSearchResults {
  messages: SearchMessageResult[]
  chats: Chat[]
  contacts: Contact[]
  organizations: SearchOrgResult[]
}

export interface TelegramSearchResult {
  telegram_id: number
  name: string
  type: 'private' | 'group' | 'channel'
  username: string | null
}

/**
 * GET /api/settings/prefs — the backend returns only these whitelisted keys,
 * each present only if set. API keys live in the vault and are never returned.
 */
export interface Prefs {
  notif_pref?: string
  briefing_schedule?: 'daily' | 'weekly'
  briefing_time?: string            // "HH:MM" (24h)
  briefing_day?: string             // 'monday' … 'sunday'
  briefing_max_tokens?: number
  briefing_last_run?: string
  catchup_summary_interval_minutes?: number
  translation_language?: string
  session_idle_timeout_minutes?: number
}

/** GET /api/llm/keys — whether a provider API key is stored in the vault */
export interface LlmKeyStatus {
  anthropic: boolean
  openai: boolean
}

export type LlmKeyProvider = keyof LlmKeyStatus

export interface OrgMember {
  contact_id: number | null
  telegram_id: number
  position_id: number
  name: string
  username: string | null
  title: string | null
  ended_at: string | null
}

export interface OrgRelatedChat {
  chat_id: number
  chat_name: string
  chat_type: string
  last_message: {
    text: string | null
    sender_name: string | null
    date: string | null
  } | null
}

export interface OrgSummary {
  id: number
  name: string
  context: string | null
  member_count: number
  former_member_count: number
}

export interface OrgDetail {
  id: number
  name: string
  context: string | null
  members: OrgMember[]
  former_members: OrgMember[]
  related_chats: OrgRelatedChat[]
}

export interface SharedMediaItem {
  id: number
  telegram_id: number
  date: string | null
  sender_name: string | null
  media_type: string | null
  media_url: string | null
  media_filename: string | null
  media_size: number | null
  text: string | null
}

export interface ChatMember {
  id: number
  name: string
  username: string | null
  is_bot: boolean
  role: 'creator' | 'admin' | 'member'
}

export interface TelegramProfile {
  id: number
  type: 'user' | 'group' | 'channel' | 'unknown'
  first_name?: string
  last_name?: string
  title?: string
  username?: string | null
  phone?: string | null
  is_bot?: boolean
  is_self?: boolean
  is_verified?: boolean
  is_premium?: boolean
  status?: string
  bio?: string | null
  about?: string | null
  members_count?: number | null
  photo_url?: string | null
  common_chats?: { id: number; title: string }[]
  /** Served from the backend cache because Telegram is unreachable */
  stale?: boolean
  /** Epoch seconds of the underlying Telegram fetch */
  fetched_at?: number
}

export interface ConversationSummary {
  id: number
  title: string | null
  created_at: string
  updated_at: string
  message_count: number
}

export interface ConversationMessageItem {
  id: number
  role: string
  content: string
  created_at: string
}

export interface ConversationDetail {
  id: number
  title: string | null
  created_at: string
  updated_at: string
  messages: ConversationMessageItem[]
}

export interface MentionMessage {
  id: number
  tg_message_id: number
  chat_id: number
  sender_id: number | null
  sender_name: string | null
  text: string | null
  date: string
  is_outgoing: boolean
  media_type: string | null
  media_url: string | null
  my_reaction?: string | null
}

export interface MentionGroup {
  mention_msg: MentionMessage
  context_before: MentionMessage[]
  context_after: MentionMessage[]
  replied: boolean
  user_reply_msg: MentionMessage | null
  reply_has_gap: boolean
  chat: {
    id: number
    telegram_id: number
    name: string
    type: string
    is_tracked: boolean
    tags: { id: number; name: string; color: string }[]
  }
}

export interface BriefingSource {
  chat_id: number
  telegram_id: number
}

export interface BriefingSummary {
  chat_id: number
  chat_name: string
  category: 'tracked' | 'briefing' | 'important_contact'
  summary: string
  sources?: BriefingSource[]
}

export interface BriefingHistoryEntry {
  id: number
  chat_id: number
  chat_name: string
  category: 'tracked' | 'briefing' | 'important_contact'
  summary: string
  sources?: BriefingSource[]
}

export type BriefingRunStatus = 'running' | 'done' | 'partial' | 'failed'

export interface BriefingRun {
  id: number
  generated_at: string
  finished_at?: string | null
  days: number
  status?: BriefingRunStatus
  error?: string | null
  entries: BriefingHistoryEntry[]
}

/** GET /api/intelligence/briefing/status */
export type BriefingJobStatus =
  | { running: false }
  | {
      running: true
      job_id: string
      kind: 'briefing'
      started_at: number
      days: number
      trigger: 'manual' | 'scheduled'
      progress: { total?: number; current?: number; chat_name?: string; run_id?: number | null; days?: number }
    }

/** POST/GET /api/catchup/generate-summaries[/status] */
export interface CatchUpSummaryJob {
  job_id?: string
  status: 'idle' | 'running' | 'done' | 'error'
  started?: boolean
  chat_ids?: number[] | null
  total?: number
  done?: number
  summaries_generated?: number
  started_at?: string | number | null
  finished_at?: string | number | null
  error?: string | null
  llm_error?: string | null
}

export interface CatchUpItem {
  id: number
  chat_telegram_id: number
  contact_id: number | null
  status: 'pending' | 'handled' | 'snoozed'
  note: string | null
  snoozed_until: string | null
  handled_at: string | null
  created_at: string | null
  updated_at: string | null
}

export interface CatchUpContextMessage {
  sender_name: string | null
  text: string | null
  date: string | null
  is_outgoing: boolean
  media_type: string | null
}

export interface CatchUpContext {
  /** null when no summary has been generated yet */
  summary: string | null
  summary_messages: CatchUpContextMessage[]
  recent_messages: CatchUpContextMessage[]
  last_activity_at: string | null
  generated_at: string | null
  new_messages_since_summary: number
  /** True when the summary is the non-LLM fallback (AI was unavailable) */
  degraded?: boolean
}

export interface ForumTopic {
  id: number
  title: string
  icon_color: number | null
  icon_emoji_id: string | null
  date: string | null
  is_closed: boolean
  is_pinned: boolean
}

export interface ChannelPostStats {
  telegram_id: number
  text: string
  date: string | null
  views: number
  forwards: number
  replies_count: number
}

export interface AdminPermissions {
  change_info: boolean
  post_messages: boolean
  edit_messages: boolean
  delete_messages: boolean
  ban_users: boolean
  invite_users: boolean
  pin_messages: boolean
  manage_call: boolean
  anonymous: boolean
  add_admins: boolean
}

export interface InviteLink {
  link: string
  title: string | null
  expire_date: string | null
  usage_limit: number | null
  usage: number
  request_needed: boolean
  revoked: boolean
}

export interface GroupSettings {
  title: string
  about: string
}

export interface AdminActionLog {
  id: number
  chat_telegram_id: number
  action: string
  target_user_id: number | null
  details: string | null
  created_at: string | null
}

export interface UserPresence {
  status: 'online' | 'offline' | 'recently' | 'last_week' | 'last_month' | 'unknown'
  last_seen?: string | null
  error?: string
}

export type TelegramRuntimeState = 'starting' | 'syncing' | 'ready' | 'reconnecting' | 'unauthorized' | 'error' | 'stopped'

/** GET /api/chats/{id}/messages/window — keyset window (oldest → newest) */
export interface MessageWindow {
  messages: Message[]
  has_older: boolean
  has_newer: boolean
  older_cursor: string | null
  newer_cursor: string | null
  anchor_found: boolean
  /** Why anchor_found is false (the server already asked Telegram) */
  anchor_reason?: AnchorReason | null
  syncing: boolean
}

export type AnchorReason = 'not_found' | 'unavailable' | 'rate_limited' | 'error'

/** POST /api/compose/send-files — per-file outcome, same order as uploaded */
export interface SendFileResult {
  filename: string
  status: 'sent' | 'failed'
  message_id?: number
  error?: string
}

export interface SendFilesResponse {
  /** Every file sent */
  success: boolean
  /** Some sent, some failed (HTTP 207) */
  partial?: boolean
  message_ids: number[]
  messages?: Message[]
  files?: SendFileResult[]
}

/** POST /api/chats/{id}/backfill */
export interface BackfillResult {
  added: number
  exhausted: boolean
  status: 'changed' | 'unchanged'
}

/** POST /api/chats/{id}/sync — typed sync result */
export interface ChatSyncResult {
  status: 'changed' | 'unchanged'
  chat_id: number
  fetched: number
  added: number
  updated: number
  deleted: number
  exhausted: boolean
  flood_wait: number | null
  error: string | null
}

export type WSEvent =
  | { type: 'new_message'; chat_id: number; message_id: number; chat_name?: string; sender_name?: string | null; text?: string; is_tracked?: boolean; is_outgoing?: boolean; message?: Message }
  | { type: 'importance_update'; message_id: number; score: number; reason: string }
  | { type: 'scan_complete'; scanned: number; flagged: number }
  | { type: 'summary_ready'; chat_id: number }
  | { type: 'message_sent'; chat_id: number; message_id: number }
  | { type: 'message_send_failed'; chat_id: number; error: string }
  | { type: 'research_complete'; entity: 'contact' | 'organization'; id: number }
  | { type: 'user_typing'; user_id: number; chat_id: number; user_name: string }
  | { type: 'user_status'; user_id: number; status: string; last_seen?: string | null }
  /** Exactly one of scam_detected / scam_check_safe / scam_check_error per checked message (terminal). */
  | { type: 'scam_detected'; request_id: string; source: ScamCheckSource; chat_id: number; message_ids: number[]; sender_ids: number[]; messages: ScamMsgState[]; pipeline_is_scam: boolean; /** raw pipeline confidence, display only */ confidence: number; reason: string; category: string | null }
  | { type: 'scam_check_safe'; request_id: string; source: ScamCheckSource; chat_id: number; message_ids: number[]; sender_ids: number[]; messages: ScamMsgState[]; pipeline_is_scam: boolean; confidence: number; reason: string; category: string | null }
  | { type: 'scam_check_error'; request_id: string | null; source: ScamCheckSource; chat_id: number; message_ids: number[]; sender_ids: number[]; messages: ScamMsgState[]; error: string }
  | { type: 'scam_users_flagged'; telegram_ids: number[] }
  | { type: 'scam_verdict_changed'; scope: 'message' | 'sender'; verdict: ScamManualVerdict; count: number; messages: ScamMsgState[]; flags: ScamFlagChange[] }
  | { type: 'messages_deleted'; chat_id: number | null; message_ids: number[] }
  | { type: 'navigate_to_chat'; chat_id: number }
  | { type: 'chat_synced'; chat_id: number }
  | { type: 'chat_sync_empty'; chat_id: number }
  | { type: 'bulk_sync_complete'; synced: number }
  | { type: 'chats_synced'; chat_ids: number[] }
  | { type: 'message_edited'; chat_id: number; tg_message_id: number; message: Message }
  | { type: 'messages_pinned'; chat_id: number; message_ids: number[]; pinned: boolean }
  | { type: 'media_ready'; chat_id: number; tg_message_id: number; media_type: string; media_url: string }
  | { type: 'chat_sync_failed'; chat_id: number; status: 'unavailable' | 'error' | 'skipped'; error: string | null }
  | { type: 'telegram_status'; state: TelegramRuntimeState; error: string | null }
  /** Channel-neutral runtime status; telegram_status is the Telegram-only alias */
  | { type: 'connector_status'; channel: string; account_key: string; state: string; error: string | null }
  | { type: 'catchup_summaries_updated'; job_id: string }
  | ({ type: 'archive_progress' } & ArchiveStatus)
  | { type: 'person_notes_changed'; telegram_id: number }
  /** An MCP agent changed a person's contact fields or positions */
  | { type: 'person_changed'; telegram_id: number }
  /** Client-side only: emitted by WebSocketProvider after a reconnect — events may have been missed, refetch */
  | { type: 'resync' }

export interface OllamaStatus {
  running: boolean
  /** Orchestrator model name when the server is up but the model is not installed */
  missing_model: string | null
}

export interface McpSettings {
  enabled: boolean
  key_set: boolean
  url: string
  vault_unlocked: boolean
  /** Opt-in: MCP clients may write person notes, fields, positions, tags and review marks */
  write_enabled?: boolean
}

// ── DM archive ──

export type ArchiveState = 'idle' | 'running' | 'paused' | 'waiting_flood' | 'stopped'

/** GET /api/archive/status (also the `archive_progress` WS payload) */
export interface ArchiveStatus {
  state: ArchiveState
  /** While waiting on a Telegram FloodWait: when the archive resumes */
  flood_wait_until: string | null
  dms: { total: number; complete: number; pending: number; failed: number; bots_skipped: number }
  current_chat: { telegram_id: number; name: string } | null
  started_at: string | null
  last_topup_at: string | null
  groups_queued: number
}

export interface HistoryCoverage {
  history_complete: boolean
  message_count: number
  oldest_tg_id: number | null
  newest_contiguous_tg_id: number | null
}

/** POST /api/chats/{telegram_id}/archive */
export interface ChatArchiveResponse {
  queued: boolean
  coverage: HistoryCoverage
}

// ── Person notes ──

export type PersonNoteKind = 'context' | 'due_diligence' | 'risk' | 'relationship' | 'todo'

export interface PersonNoteCitation {
  chat_id: number
  tg_message_id: number
}

export interface PersonNote {
  id: number
  telegram_id: number
  kind: PersonNoteKind
  title: string
  /** Markdown */
  body: string
  citations: PersonNoteCitation[]
  author: 'agent' | 'user'
  agent_label: string | null
  created_at: string
  updated_at: string
}

export interface PersonNoteInput {
  kind: PersonNoteKind
  title: string
  body: string
  citations?: PersonNoteCitation[]
}

/** GET /api/features — backend feature switches the UI follows */
export interface Features {
  /** Shield (scam detection). Off while its backend is retired. */
  shield: boolean
}
