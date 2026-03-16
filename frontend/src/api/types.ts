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
  unread_count?: number
  last_synced_at: string | null
  last_message?: {
    text: string
    sender_name: string | null
    date: string | null
  } | null
}

export interface Tag {
  id: number
  name: string
  color: string
  category: string | null
  is_default: boolean
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
  telegram_id: number
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
  reply_to_telegram_id?: number | null
  reply_to_preview?: ReplyPreview | null
  my_reaction?: string | null
  topic_id?: number | null
  is_pinned?: boolean
}

export interface ContactPosition {
  id: number
  telegram_id?: number
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

export interface PersonSummary {
  telegram_id: number
  name: string
  username: string | null
  is_important: boolean
  contact_id: number | null
  relationship_status: string | null
  last_interaction_at: string | null
  primary_position: { title: string | null; organization: string } | null
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
  category: string
  summary: string
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
  attention_contacts: DashboardAttentionContact[]
  latest_briefing: DashboardBriefing | null
}

export interface SearchMessageResult {
  telegram_id: number
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

export interface Prefs {
  notif_pref?: string
  briefing_schedule?: string
  briefing_time?: string
  briefing_day?: string
  briefing_max_tokens?: number
  briefing_last_run?: string
  catchup_summary_interval_minutes?: number
  [key: string]: unknown
}

export interface OrgMember {
  contact_id: number
  telegram_id: number
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
  telegram_id: number
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

export interface BriefingRun {
  id: number
  generated_at: string
  days: number
  entries: BriefingHistoryEntry[]
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
  summary: string
  summary_messages: CatchUpContextMessage[]
  recent_messages: CatchUpContextMessage[]
  last_activity_at: string | null
  generated_at: string | null
  new_messages_since_summary: number
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

export type WSEvent =
  | { type: 'new_message'; chat_id: number; message_id: number; chat_name?: string; sender_name?: string | null; text?: string; is_tracked?: boolean; is_outgoing?: boolean }
  | { type: 'importance_update'; message_id: number; score: number; reason: string }
  | { type: 'scan_complete'; scanned: number; flagged: number }
  | { type: 'summary_ready'; chat_id: number }
  | { type: 'message_sent'; chat_id: number; message_id: number }
  | { type: 'message_send_failed'; chat_id: number; error: string }
  | { type: 'research_complete'; entity: 'contact' | 'organization'; id: number }
