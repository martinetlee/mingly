import type { PeoplePage, Features, Person } from './types'
import type { BackfillResult, ConversationSyncResult, MessageWindow, SendFilesResponse } from './types'
import type { AdminActionLog, AdminPermissions, AiConversationDetail, AiConversationSummary, AuthStatus, BoardColumn, BoardData, BriefingJobStatus, BriefingRun, CatchUpItem, CatchUpSummaryJob, ChannelPostStats, ChatMember, ContactPosition, Conversation, ConversationKind, DashboardData, Folder, ForumTopic, GlobalSearchResults, GroupSettings, InviteLink, MentionGroup, Message, OrgDetail, OrgSummary, Prefs, ScamFlag, ScannedMessage, ScamCategory, ScamManualVerdict, ScamVerdictResponse, ScamVerdictFilter, ScamQueueStats, CloudSyncResult, ScamReport, ReportContextMessage, SharedMediaItem, Tag, TagSuggestion, TelegramDirectoryResult, TelegramProfile, UserPresence, Workspace } from './types'
import { setSessionToken, clearSessionToken, notifySessionExpired } from './session'
import { setCachedNotifPref } from '../lib/notifications'

import { request, stream, upload, requestBlob, ApiError, isAbortError, isSessionExpiredError, formatErrorDetail } from '../lib/http'
import type { SSEEvent, StreamOptions, UploadOptions, UploadHandle, RequestOptions } from '../lib/http'

// The transport lives in lib/http.ts; re-exported so callers have one import site
export { request, stream, upload, requestBlob, ApiError, isAbortError, isSessionExpiredError, formatErrorDetail }
export type { SSEEvent, StreamOptions, UploadOptions, UploadHandle, RequestOptions }

// True when an error came from a Telegram rate limit surfaced by the backend
// (429 detail: "Telegram rate limit — retry in Ns")
export const isRateLimitError = (e: unknown): boolean =>
  e instanceof Error && e.message.includes('rate limit')

/**
 * Method-first JSON helper (formerly duplicated in Settings/LlmSetup).
 * Same semantics as request(): token header, GET retries, 401 → session expiry.
 */
export const apiFetch = <T>(method: string, path: string, body?: object, signal?: AbortSignal): Promise<T> =>
  request<T>(path, {
    method,
    body: body ? JSON.stringify(body) : undefined,
    signal,
  })

/**
 * Follow a background LLM job (/api/llm/jobs/{id}: Ollama install or model
 * pull) to completion. Throws on `status: 'error'`, on any inline `error`
 * field (Ollama reports pull failures with HTTP 200), and when the stream
 * ends without `status: 'done'`, so a failed pull can never look like success.
 */
export async function followLlmJob(
  jobId: number,
  onProgress: (ev: Record<string, unknown>) => void,
  signal?: AbortSignal,
  failureLabel = 'Job failed',
): Promise<void> {
  for await (const ev of streamSSE(`/api/llm/jobs/${jobId}`, signal)) {
    if (ev.status === 'error' || ev.error) {
      throw new Error(typeof ev.error === 'string' && ev.error ? `${failureLabel}: ${ev.error}` : failureLabel)
    }
    if (ev.status === 'done') return
    onProgress(ev)
  }
  throw new Error(`${failureLabel}: the progress stream ended unexpectedly`)
}

/** SSE helper yielding only the JSON `data` payloads (formerly duplicated in Settings/LlmSetup). */
export async function* streamSSE(path: string, signal?: AbortSignal): AsyncGenerator<Record<string, unknown>> {
  for await (const ev of stream<Record<string, unknown>>(path, { signal })) {
    yield ev.data
  }
}

// --- Vault ---

export const getVaultStatus = () =>
  request<{ status: 'unset' | 'locked' | 'unlocked' }>('/api/auth/vault-status')

export type SessionValidation = {
  valid: boolean
  vault_status: 'unset' | 'locked' | 'unlocked'
  idle_timeout_minutes?: number
}

/**
 * Ask the backend whether this tab's session token is still valid (no token
 * required; also refreshes the idle timer). Resolves to null only when the
 * endpoint doesn't exist (pre-contract backend) so callers can fall back to
 * vault status. Never fires the global session-expired handler. Network
 * errors and 5xx throw — callers must never read those as "no vault".
 */
export const validateSession = async (signal?: AbortSignal): Promise<SessionValidation | null> => {
  try {
    return await request<SessionValidation>('/api/session/validate', { signal, skipSessionExpiry: true, attempts: 1 })
  } catch (e) {
    if (e instanceof ApiError && (e.status === 404 || e.status === 405 || e.code === 'html_response')) return null
    throw e
  }
}

export const initVault = async (password: string) => {
  const result = await request<{ success: boolean; session_token: string }>('/api/auth/vault/init', {
    method: 'POST',
    body: JSON.stringify({ password }),
  })
  if (result.session_token) {
    setSessionToken(result.session_token)
  }
  return result
}

export const unlockVault = async (password: string) => {
  const result = await request<{ success: boolean; session_token: string }>('/api/auth/vault/unlock', {
    method: 'POST',
    body: JSON.stringify({ password }),
  })
  if (result.session_token) {
    setSessionToken(result.session_token)
  }
  return result
}

/**
 * Destroy the vault. The backend requires the user's typed confirmation
 * (`"RESET"`); anything else is rejected with 400 — collect it in the UI.
 */
export const resetVault = async (confirm?: string) => {
  const result = await request<{ success: boolean }>('/api/auth/vault/reset', {
    method: 'POST',
    body: JSON.stringify({ confirm: confirm ?? '' }),
  })
  clearSessionToken()
  return result
}

// --- Auth ---

export const getAuthStatus = () =>
  request<AuthStatus>('/api/auth/status')

export const hasCredentials = () =>
  request<{ has_credentials: boolean }>('/api/auth/has-credentials')

export const setupCredentials = (api_id: string, api_hash: string) =>
  request<{ success: boolean }>('/api/auth/setup', {
    method: 'POST',
    body: JSON.stringify({ api_id, api_hash }),
  })

export const resolve1Password = (reference: string) =>
  request<{ value: string }>('/api/auth/resolve-1password', {
    method: 'POST',
    body: JSON.stringify({ reference }),
  })

export const sendCode = (phone: string) =>
  request<{ phone_code_hash: string }>('/api/auth/send-code', {
    method: 'POST',
    body: JSON.stringify({ phone }),
  })

export const verifyCode = (phone: string, code: string, phone_code_hash: string) =>
  request<{ success: boolean; requires_2fa: boolean; first_name?: string }>('/api/auth/verify-code', {
    method: 'POST',
    body: JSON.stringify({ phone, code, phone_code_hash }),
  })

export const verify2FA = (password: string) =>
  request<{ success: boolean; first_name?: string }>('/api/auth/verify-2fa', {
    method: 'POST',
    body: JSON.stringify({ password }),
  })

export const logout = () =>
  request<{ success: boolean }>('/api/auth/logout', { method: 'POST' })

/**
 * Lock the vault: the backend revokes every session, stops Telegram and
 * closes all WebSockets. Then end this tab's session through the same path
 * as expiry (token cleared once, App → Unlock, socket stopped), so in-flight
 * requests that 401 afterwards hit the stampede guard instead of a storm.
 * A 401 means the session was already gone — still lock locally.
 */
export const lockVault = async (): Promise<void> => {
  try {
    await request<{ success: boolean }>('/api/auth/lock', { method: 'POST' })
  } catch (e) {
    if (!isSessionExpiredError(e)) throw e
  }
  notifySessionExpired()
}


// --- Conversations ---

export const getConversations = (limit = 50, offset = 0, sortBy = 'recent', tagId?: number, workspaceId?: number, signal?: AbortSignal) =>
  request<Conversation[]>(`/api/conversations?limit=${limit}&offset=${offset}&sort_by=${sortBy}${tagId != null ? `&tag_id=${tagId}` : ''}${workspaceId != null ? `&workspace_id=${workspaceId}` : ''}`, { signal })

// --- Workspaces ---

export const getWorkspaces = () => request<Workspace[]>('/api/workspaces')

export const createWorkspace = (data: { name: string; emoji?: string | null; color?: string; tag_ids: number[]; view_mode?: string }) =>
  request<Workspace>('/api/workspaces', { method: 'POST', body: JSON.stringify(data) })

export const updateWorkspace = (id: number, data: { name?: string; emoji?: string | null; color?: string; tag_ids?: number[]; view_mode?: string }) =>
  request<{ status: string }>(`/api/workspaces/${id}`, { method: 'PUT', body: JSON.stringify(data) })

export const deleteWorkspace = (id: number) =>
  request<void>(`/api/workspaces/${id}`, { method: 'DELETE' })

export const getBoard = (workspaceId: number, boardId?: number) =>
  request<BoardData>(`/api/workspaces/${workspaceId}/board${boardId != null ? `?board_id=${boardId}` : ''}`)

export const addWorkspaceBoard = (workspaceId: number, name: string, scopeTagName?: string) =>
  request<{ id: number; name: string }>(`/api/workspaces/${workspaceId}/boards`, {
    method: 'POST', body: JSON.stringify({ name, scope_tag_name: scopeTagName ?? null }),
  })

export const updateWorkspaceBoard = (
  workspaceId: number, boardId: number,
  data: { name?: string; scope_tag_name?: string; clear_scope?: boolean; position?: number },
) =>
  request<{ status: string }>(`/api/workspaces/${workspaceId}/boards/${boardId}`, { method: 'PUT', body: JSON.stringify(data) })

// Cards on the board's columns always return to backlog (the server deletes
// the columns' stage tags); membership and stream tags are never stripped.
export const deleteWorkspaceBoard = (workspaceId: number, boardId: number) =>
  request<{ status: string }>(`/api/workspaces/${workspaceId}/boards/${boardId}`, { method: 'DELETE' })

export const addBoardColumn = (workspaceId: number, boardId: number, name: string, color?: string) =>
  request<BoardColumn>(`/api/workspaces/${workspaceId}/boards/${boardId}/columns`, { method: 'POST', body: JSON.stringify({ name, color }) })

export const updateBoardColumn = (workspaceId: number, boardId: number, tagId: number, data: { name?: string; color?: string; position?: number }) =>
  request<{ status: string }>(`/api/workspaces/${workspaceId}/boards/${boardId}/columns/${tagId}`, { method: 'PUT', body: JSON.stringify(data) })

export const deleteBoardColumn = (workspaceId: number, boardId: number, tagId: number) =>
  request<{ status: string }>(`/api/workspaces/${workspaceId}/boards/${boardId}/columns/${tagId}`, { method: 'DELETE' })

/** Atomic left-to-right column order (every column of the board); returns the updated board */
export const reorderBoardColumns = (workspaceId: number, boardId: number, tagIds: number[]) =>
  request<BoardData>(`/api/workspaces/${workspaceId}/boards/${boardId}/columns/order`, { method: 'PUT', body: JSON.stringify({ tag_ids: tagIds }) })

/** Returns the committed board for `boardId` — the newest snapshot */
export const moveBoardConversation = (workspaceId: number, boardId: number, conversationId: number, toTagId: number | null) =>
  request<BoardData>(`/api/workspaces/${workspaceId}/move`, { method: 'POST', body: JSON.stringify({ board_id: boardId, conversation_id: conversationId, to_tag_id: toTagId }) })

export const getConversation = (conversationId: number, signal?: AbortSignal) =>
  request<Conversation>(`/api/conversations/${conversationId}`, { signal })

export const getConversationsByIds = (ids: number[]) =>
  request<Conversation[]>('/api/conversations/by-ids', {
    method: 'POST',
    body: JSON.stringify({ ids }),
  })

export const searchConversationsLocal = (q: string) =>
  request<{ local: Conversation[] }>(`/api/conversations/search?q=${encodeURIComponent(q)}`)

/** Telegram global message search: the conversation is stored server-side, its messages may not be yet (open by external_id) */
export interface MessageSearchResult {
  conversation_id: number
  conversation_title: string
  conversation_kind: ConversationKind
  messages: { external_id: string; text: string; sender_name: string | null; date: string | null }[]
}

export const searchMessagesGlobal = (q: string, signal?: AbortSignal) =>
  request<MessageSearchResult[]>(`/api/conversations/search/messages?q=${encodeURIComponent(q)}`, { signal })

export const bulkSync = () =>
  request<{ synced: number }>('/api/conversations/bulk-sync', { method: 'POST' })

export interface MessageWindowParams {
  limit?: number
  /** Message id: the `limit` messages immediately older than it */
  before?: number | null
  /** Message id: the `limit` messages immediately newer than it */
  after?: number | null
  /** Message id to centre the window on */
  anchor?: number | null
  /** The channel's id of a message that may not be stored yet (e.g. a reply target): fetched from the channel */
  aroundExternalId?: string | null
  /** ISO datetime: centre on the first message at/after it */
  date?: string | null
  /** Forum topic scope (1 = General, includes untagged rows) */
  topicId?: number | null
}

/** Keyset message window (oldest → newest). Use at most one of before/after/anchor/aroundExternalId/date. */
export const getMessageWindow = (conversationId: number, p: MessageWindowParams = {}, signal?: AbortSignal) => {
  const params = new URLSearchParams()
  if (p.limit != null) params.set('limit', String(p.limit))
  if (p.before != null) params.set('before', String(p.before))
  if (p.after != null) params.set('after', String(p.after))
  if (p.anchor != null) params.set('anchor', String(p.anchor))
  if (p.aroundExternalId) params.set('around_external_id', p.aroundExternalId)
  if (p.date) params.set('around_date', p.date)
  if (p.topicId != null) params.set('topic_id', String(p.topicId))
  return request<MessageWindow>(`/api/conversations/${conversationId}/messages/window?${params}`, { signal })
}

/** Fetch history older than the oldest stored message from the channel. */
export const backfillConversation = (conversationId: number, opts: { limit?: number; topicId?: number | null } = {}, signal?: AbortSignal) => {
  const params = new URLSearchParams({ limit: String(opts.limit ?? 100) })
  if (opts.topicId != null) params.set('topic_id', String(opts.topicId))
  return request<BackfillResult>(`/api/conversations/${conversationId}/backfill?${params}`, { method: 'POST', signal })
}

export const searchConversationMessages = (conversationId: number, query: string, limit = 50, signal?: AbortSignal) =>
  request<Message[]>(`/api/conversations/${conversationId}/messages/search?q=${encodeURIComponent(query)}&limit=${limit}`, { signal })

export const syncConversation = (conversationId: number, limit?: number) =>
  request<ConversationSyncResult>(`/api/conversations/${conversationId}/sync${limit != null ? `?limit=${limit}` : ''}`, { method: 'POST' })

export const getConversationMembers = (conversationId: number, limit = 100) =>
  request<ChatMember[]>(`/api/conversations/${conversationId}/members?limit=${limit}`)

export const getSharedMedia = (conversationId: number, mediaFilter: 'media' | 'files' | 'links' = 'media', limit = 50, offset = 0, signal?: AbortSignal) =>
  request<SharedMediaItem[]>(`/api/conversations/${conversationId}/shared-media?media_filter=${mediaFilter}&limit=${limit}&offset=${offset}`, { signal })

export const markConversationRead = (conversationId: number) =>
  request<{ success: boolean }>(`/api/conversations/${conversationId}/mark-read`, { method: 'POST' })

export const getPinnedMessages = (conversationId: number, signal?: AbortSignal) =>
  request<Message[]>(`/api/conversations/${conversationId}/pinned`, { signal })

export const getPinnedMessage = async (conversationId: number, signal?: AbortSignal): Promise<Message | null> => {
  const msgs = await getPinnedMessages(conversationId, signal)
  return msgs && msgs.length > 0 ? msgs[0] : null
}

export const getHiddenConversations = () =>
  request<Conversation[]>('/api/conversations/hidden')

export const getArchivedConversations = () =>
  request<Conversation[]>('/api/conversations/archived')

export const setPinned = (conversationId: number, pinned: boolean) =>
  request<{ conversation_id: number; is_pinned: boolean }>(`/api/conversations/${conversationId}/pin`, {
    method: 'POST',
    body: JSON.stringify({ pinned }),
  })

export const setArchived = (conversationId: number, archived: boolean) =>
  request<{ conversation_id: number; is_archived: boolean }>(`/api/conversations/${conversationId}/archive`, {
    method: 'POST',
    body: JSON.stringify({ archived }),
  })

export const setMuted = (conversationId: number, muted: boolean, duration?: string) =>
  request<{ conversation_id: number; is_muted: boolean; muted_until: string | null }>(`/api/conversations/${conversationId}/mute`, {
    method: 'POST',
    body: JSON.stringify({ muted, duration }),
  })

export const setMarkedUnread = (conversationId: number, marked_unread: boolean) =>
  request<{ conversation_id: number; is_marked_unread: boolean }>(`/api/conversations/${conversationId}/mark-unread`, {
    method: 'POST',
    body: JSON.stringify({ marked_unread }),
  })

export const setHidden = (conversationId: number, hidden: boolean) =>
  request<{ conversation_id: number; is_hidden: boolean }>(`/api/conversations/${conversationId}/hide`, {
    method: 'POST',
    body: JSON.stringify({ hidden }),
  })

export const getTrackedConversations = () =>
  request<Conversation[]>('/api/conversations/tracked')

export const setTracked = (conversationId: number, tracked: boolean) =>
  request<{ conversation_id: number; is_tracked: boolean }>(`/api/conversations/${conversationId}/track`, {
    method: 'POST',
    body: JSON.stringify({ tracked }),
  })

export const getBriefingConversations = () =>
  request<Conversation[]>('/api/conversations/briefing')

export const setBriefing = (conversationId: number, briefing: boolean) =>
  request<{ conversation_id: number; is_briefing: boolean }>(`/api/conversations/${conversationId}/briefing`, {
    method: 'POST',
    body: JSON.stringify({ briefing }),
  })

export const reactToMessage = (messageId: number, emoji: string | null) =>
  request<{ success: boolean; my_reaction: string | null }>(`/api/messages/${messageId}/react`, {
    method: 'POST',
    body: JSON.stringify({ emoji }),
  })

/** Presence of a direct conversation's peer (capability 'presence') */
export const getPeerPresence = (conversationId: number, signal?: AbortSignal) =>
  request<UserPresence>(`/api/conversations/${conversationId}/presence`, { signal })

export const sendTypingAction = (conversationId: number) =>
  request<{ success: boolean }>(`/api/conversations/${conversationId}/typing`, { method: 'POST' })

// --- Forum Topics ---

export const getForumTopics = (conversationId: number) =>
  request<ForumTopic[]>(`/api/conversations/${conversationId}/topics`)

export const createForumTopic = (conversationId: number, title: string) =>
  request<{ success: boolean; topic_id: number | null }>(`/api/conversations/${conversationId}/topics`, {
    method: 'POST',
    body: JSON.stringify({ title }),
  })

export const setTopicState = (conversationId: number, topic_id: number, closed: boolean) =>
  request<{ success: boolean; topic_id: number; closed: boolean }>(`/api/conversations/${conversationId}/topics/${topic_id}/state`, {
    method: 'POST',
    body: JSON.stringify({ closed }),
  })

// --- Slow Mode ---

export const setSlowMode = (conversationId: number, seconds: number) =>
  request<{ success: boolean; slow_mode_seconds: number }>(`/api/conversations/${conversationId}/slow-mode`, {
    method: 'POST',
    body: JSON.stringify({ seconds }),
  })

// --- Pin/Unpin Messages ---

export const pinMessage = (messageId: number, pinned: boolean, notify = false) =>
  request<{ success: boolean; is_pinned: boolean }>(`/api/messages/${messageId}/pin`, {
    method: 'POST',
    body: JSON.stringify({ pinned, notify }),
  })

// --- Channel Post Stats ---

export const getPostStats = (conversationId: number, limit = 20) =>
  request<ChannelPostStats[]>(`/api/conversations/${conversationId}/post-stats?limit=${limit}`)

// --- Tags ---

export const getTags = () =>
  request<Tag[]>('/api/tags')

export const createTag = (name: string, color: string = 'slate', category?: string) =>
  request<Tag>('/api/tags', {
    method: 'POST',
    body: JSON.stringify({ name, color, category: category || null }),
  })

export const updateTag = (tag_id: number, data: { name?: string; color?: string; category?: string | null }) =>
  request<Tag>(`/api/tags/${tag_id}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  })

export const deleteTag = (tag_id: number) =>
  request<{ success: boolean }>(`/api/tags/${tag_id}`, { method: 'DELETE' })

export const getConversationTags = (conversationId: number) =>
  request<Tag[]>(`/api/conversations/${conversationId}/tags`)

export const addTagToConversation = (conversationId: number, tag_id: number) =>
  request<{ success: boolean }>(`/api/conversations/${conversationId}/tags`, {
    method: 'POST',
    body: JSON.stringify({ tag_id }),
  })

export const removeTagFromConversation = (conversationId: number, tag_id: number) =>
  request<{ success: boolean }>(`/api/conversations/${conversationId}/tags/${tag_id}`, { method: 'DELETE' })

export const suggestTags = (conversationId: number) =>
  request<TagSuggestion[]>(`/api/conversations/${conversationId}/tags/suggest`, { method: 'POST' })

// --- Organizations ---

export const getOrganizations = (search = '', signal?: AbortSignal) =>
  request<OrgSummary[]>(`/api/organizations?search=${encodeURIComponent(search)}`, { signal })

export const createOrganization = (name: string) =>
  request<OrgDetail>('/api/organizations', { method: 'POST', body: JSON.stringify({ name }) })

export const getOrganization = (orgId: number, signal?: AbortSignal) =>
  request<OrgDetail>(`/api/organizations/${orgId}`, { signal })

/** With `merge: true` a rename onto an existing name merges INTO that org — the result has the other org's id */
export const updateOrganization = (orgId: number, data: { name?: string; context?: string; merge?: boolean }) =>
  request<OrgDetail>(`/api/organizations/${orgId}`, { method: 'PUT', body: JSON.stringify(data) })

export const mergeOrganization = (orgId: number, intoId: number) =>
  request<OrgDetail & { moved_positions: number; merged_duplicate_positions: number }>(`/api/organizations/${orgId}/merge`, {
    method: 'POST', body: JSON.stringify({ into_id: intoId }),
  })

/** detach (default): members keep the position as history; delete: their positions here are removed */
export const deleteOrganization = (orgId: number, mode: 'detach' | 'delete' = 'detach') =>
  request<{ success: boolean; mode: string; positions_detached: number; positions_deleted: number }>(
    `/api/organizations/${orgId}?mode=${mode}`, { method: 'DELETE' },
  )

export const autoDiscoverOrgs = () =>
  request<{ created: number; total: number }>('/api/organizations/auto-discover', { method: 'POST' })

export const generateOrgContext = (orgId: number) =>
  request<OrgDetail>(`/api/organizations/${orgId}/generate-context`, { method: 'POST' })

// --- People (the CRM entity; every identity on every channel belongs to one) ---

/** Address-book sync of every connector that has one */
export const syncPeople = () =>
  request<{ imported: number; total: number }>('/api/people/sync', { method: 'POST' })

export const getPeoplePage = (
  params: { search?: string; status?: string; important?: boolean; contactsOnly?: boolean; limit?: number; offset?: number } = {},
  signal?: AbortSignal,
) => {
  const qs = new URLSearchParams({
    search: params.search ?? '',
    limit: String(params.limit ?? 100),
    offset: String(params.offset ?? 0),
  })
  if (params.status && params.status !== 'all') qs.set('status', params.status)
  if (params.important) qs.set('important', 'true')
  if (params.contactsOnly) qs.set('contacts_only', 'true')
  return request<PeoplePage>(`/api/people?${qs}`, { signal })
}

/** First page of matches only — use getPeoplePage for lists */
export const getPeople = (search = '', limit = 100, offset = 0, contactsOnly = false) =>
  getPeoplePage({ search, limit, offset, contactsOnly }).then((page) => page.items)

/** Local data only (works offline) */
export const getPerson = (personId: number, signal?: AbortSignal) =>
  request<Person>(`/api/people/${personId}`, { signal })

/** The person's direct conversations on every channel, most recent first */
export const getPersonConversations = (personId: number, signal?: AbortSignal) =>
  request<Conversation[]>(`/api/people/${personId}/conversations`, { signal })

export const updatePerson = (personId: number, data: { display_name?: string | null; twitter_handle?: string; linkedin_url?: string; notes?: string; is_important?: boolean }) =>
  request<Person>(`/api/people/${personId}`, { method: 'PUT', body: JSON.stringify(data) })

export const setPersonImportant = (personId: number, is_important: boolean) =>
  request<Person>(`/api/people/${personId}/important`, {
    method: 'POST',
    body: JSON.stringify({ is_important }),
  })

export const addPosition = (
  personId: number,
  organization: string,
  title?: string,
  is_active = true,
  ended_at?: string,
) =>
  request<ContactPosition>(`/api/people/${personId}/positions`, {
    method: 'POST',
    body: JSON.stringify({ organization, title: title || null, is_active, ended_at: ended_at || null }),
  })

/** `ended_at: ""` clears the end date */
export const updatePosition = (
  personId: number,
  positionId: number,
  data: { title?: string; is_active?: boolean; ended_at?: string | null },
) =>
  request<ContactPosition>(`/api/people/${personId}/positions/${positionId}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  })

export const removePosition = (personId: number, positionId: number) =>
  request<{ success: boolean }>(`/api/people/${personId}/positions/${positionId}`, { method: 'DELETE' })

export const getPositions = (personId: number, signal?: AbortSignal) =>
  request<ContactPosition[]>(`/api/people/${personId}/positions`, { signal })

export const setRelationshipStatus = (personId: number, status: string) =>
  request<Person>(`/api/people/${personId}/relationship-status`, { method: 'POST', body: JSON.stringify({ status }) })

export const resetRelationshipStatus = (personId: number) =>
  request<Person>(`/api/people/${personId}/relationship-status/auto`, { method: 'POST' })

export const refreshRelationshipStatuses = () =>
  request<{ refreshed: number }>('/api/people/refresh-statuses', { method: 'POST' })

// --- Telegram-only operations (409 `unsupported_channel` for other channels) ---

/** Telegram directory search (people, groups, channels not necessarily stored) */
export const searchTelegramDirectory = (q: string, signal?: AbortSignal) =>
  request<TelegramDirectoryResult[]>(`/api/telegram/search?q=${encodeURIComponent(q)}`, { signal })

/** Store (if needed) and return the conversation for a Telegram peer */
export const ensureTelegramConversation = (peerId: number) =>
  request<Conversation>('/api/telegram/conversations/ensure', { method: 'POST', body: JSON.stringify({ peer_id: peerId }) })

export const getTelegramFolders = () =>
  request<Folder[]>('/api/telegram/folders')

// Single attempt: a 503 means "Telegram offline, no cached copy" — retrying
// only delays showing the local data
export const getTelegramProfile = (identityId: number, signal?: AbortSignal, opts: { refresh?: boolean } = {}) =>
  request<TelegramProfile>(`/api/telegram/identities/${identityId}/profile${opts.refresh ? '?refresh=true' : ''}`, { signal, attempts: 1 })

/** A public @username → the stored person and/or conversation it names */
export const resolveTelegramUsername = (username: string, signal?: AbortSignal) =>
  request<{ person_id: number | null; identity_id: number | null; conversation_id: number | null }>(
    `/api/telegram/resolve?username=${encodeURIComponent(username.replace(/^@/, ''))}`, { signal, attempts: 1 },
  )

/** A Telegram user id (`tg://user?id=` link) → its person */
export const getPersonForTelegramUser = (userId: number | string, signal?: AbortSignal) =>
  request<{ person_id: number; identity_id: number }>(`/api/telegram/users/${encodeURIComponent(String(userId))}/person`, { signal, attempts: 1 })

/** Start a Telegram direct conversation with a person (through their Telegram identity) */
export const startPrivateChat = (personId: number) =>
  request<Conversation>('/api/telegram/create/private', {
    method: 'POST',
    body: JSON.stringify({ person_id: personId }),
  })

export const createGroup = (name: string, memberPersonIds: number[]) =>
  request<Conversation>('/api/telegram/create/group', {
    method: 'POST',
    body: JSON.stringify({ name, person_ids: memberPersonIds }),
  })

export const createChannel = (name: string, about: string = '', isPublic: boolean = false) =>
  request<Conversation>('/api/telegram/create/channel', {
    method: 'POST',
    body: JSON.stringify({ name, about, is_public: isPublic }),
  })

export const joinViaInviteLink = (link: string) =>
  request<Conversation>('/api/telegram/join', {
    method: 'POST',
    body: JSON.stringify({ link }),
  })

// --- LLM provider API keys (vault-stored, never returned) ---

export const getLlmKeys = () =>
  request<import('./types').LlmKeyStatus>('/api/llm/keys')

export const setLlmKey = (provider: import('./types').LlmKeyProvider, apiKey: string) =>
  request<{ provider: string; key_set: boolean }>(`/api/llm/keys/${provider}`, {
    method: 'PUT',
    body: JSON.stringify({ api_key: apiKey }),
  })

export const deleteLlmKey = (provider: import('./types').LlmKeyProvider) =>
  request<{ provider: string; key_set: boolean }>(`/api/llm/keys/${provider}`, { method: 'DELETE' })

// --- Settings / Prefs ---

export const getSessionTimeout = () =>
  request<{ minutes: number }>('/api/settings/prefs/session-timeout')

/** Idle minutes before a session token expires: 0 (never) or 5..10080 */
export const setSessionTimeout = (minutes: number) =>
  request<{ success: boolean; minutes: number }>('/api/settings/prefs/session-timeout', {
    method: 'POST',
    body: JSON.stringify({ minutes }),
  })

export const getPrefs = () =>
  request<Prefs>('/api/settings/prefs')

export const setNotifPref = async (pref: 'all' | 'tracked' | 'off') => {
  const result = await request<{ success: boolean; pref: string }>('/api/settings/prefs/notifications', {
    method: 'POST',
    body: JSON.stringify({ pref }),
  })
  // Keep the global NotificationBridge in step with the saved value
  setCachedNotifPref(result?.pref ?? pref)
  return result
}

export const setBriefingSchedule = (schedule: 'daily' | 'weekly') =>
  request<{ success: boolean; schedule: string }>('/api/settings/prefs/briefing-schedule', {
    method: 'POST',
    body: JSON.stringify({ schedule }),
  })

export const setBriefingTime = (time: string) =>
  request<{ success: boolean; time: string }>('/api/settings/prefs/briefing-time', {
    method: 'POST',
    body: JSON.stringify({ time }),
  })

export const setBriefingDay = (day: string) =>
  request<{ success: boolean; day: string }>('/api/settings/prefs/briefing-day', {
    method: 'POST',
    body: JSON.stringify({ day }),
  })

export const setBriefingLastRun = () =>
  request<{ success: boolean; briefing_last_run: string }>('/api/settings/prefs/briefing-last-run', {
    method: 'POST',
  })

export const setBriefingMaxTokens = (max_tokens: number) =>
  request<{ success: boolean; max_tokens: number }>('/api/settings/prefs/briefing-max-tokens', {
    method: 'POST',
    body: JSON.stringify({ max_tokens }),
  })

export const getBriefingPrompts = () =>
  request<{
    tracked: { default: string; custom: string; effective: string; is_custom: boolean }
    briefing: { default: string; custom: string; effective: string; is_custom: boolean }
  }>('/api/settings/prefs/briefing-prompts')

export const setBriefingPrompts = (tracked_prompt: string, briefing_prompt: string) =>
  request<{ success: boolean; tracked_is_custom: boolean; briefing_is_custom: boolean }>('/api/settings/prefs/briefing-prompts', {
    method: 'POST',
    body: JSON.stringify({ tracked_prompt, briefing_prompt }),
  })

export const getBriefingHistory = (signal?: AbortSignal) =>
  request<BriefingRun[]>('/api/intelligence/briefing/history', { signal })

/** Whether a briefing job (manual or scheduled) is running right now */
export const getBriefingStatus = () =>
  request<BriefingJobStatus>('/api/intelligence/briefing/status')

/** Stop the running briefing (closing its stream does not cancel it) */
export const cancelBriefing = () =>
  request<{ cancelled: boolean; job_id?: string }>('/api/intelligence/briefing/cancel', { method: 'POST' })

export const deleteBriefingRun = (run_id: number) =>
  request<{ success: boolean }>(`/api/intelligence/briefing/history/${run_id}`, {
    method: 'DELETE',
  })

// --- Compose ---

export interface SendOptions {
  /** Forum topic to post into (1/omitted = General) */
  topicId?: number | null
  /** One per user send; a retry with the same nonce returns the first result instead of sending twice */
  clientNonce?: string
}

export const sendMessage = (conversationId: number, text: string, replyToMessageId?: number, opts: SendOptions = {}) =>
  request<{ success: boolean; message_id: number; message?: Message }>('/api/compose/send', {
    method: 'POST',
    body: JSON.stringify({
      conversation_id: conversationId, text, reply_to_message_id: replyToMessageId,
      topic_id: opts.topicId ?? undefined,
      client_nonce: opts.clientNonce,
    }),
  })

export const editMessage = (messageId: number, text: string) =>
  request<{ success: boolean; message?: Message }>('/api/compose/edit', {
    method: 'POST',
    body: JSON.stringify({ message_id: messageId, text }),
  })

export const deleteMessages = (conversationId: number, messageIds: number[], revoke = false) =>
  request<{ success: boolean }>('/api/compose/delete', {
    method: 'POST',
    body: JSON.stringify({ conversation_id: conversationId, message_ids: messageIds, revoke }),
  })

export const forwardMessages = (fromConversationId: number, messageIds: number[], toConversationId: number) =>
  request<{ success: boolean }>('/api/compose/forward', {
    method: 'POST',
    body: JSON.stringify({ from_conversation_id: fromConversationId, message_ids: messageIds, to_conversation_id: toConversationId }),
  })

export function sendFiles(
  conversationId: number,
  files: File[],
  text?: string,
  replyToMessageId?: number,
  onProgress?: (fraction: number) => void,
  opts: SendOptions & { signal?: AbortSignal } = {},
): { promise: Promise<SendFilesResponse>; abort: () => void } {
  const formData = new FormData()
  formData.append('conversation_id', String(conversationId))
  if (text) formData.append('text', text)
  if (replyToMessageId != null) formData.append('reply_to_message_id', String(replyToMessageId))
  if (opts.topicId != null) formData.append('topic_id', String(opts.topicId))
  if (opts.clientNonce) formData.append('client_nonce', opts.clientNonce)
  for (const file of files) {
    formData.append('files', file)
  }
  // 200 = all sent, 207 = partial (see `files`); 400 (none sent) rejects with ApiError, body.files has per-file errors
  return upload<SendFilesResponse>('/api/compose/send-files', formData, { onProgress, signal: opts.signal })
}

// --- AI conversations (LLM chat history) ---

export const listAiConversations = () =>
  request<AiConversationSummary[]>('/api/ai/conversations/')

export const createAiConversation = () =>
  request<AiConversationDetail>('/api/ai/conversations/', { method: 'POST' })

export const getAiConversation = (id: number, signal?: AbortSignal) =>
  request<AiConversationDetail>(`/api/ai/conversations/${id}`, { signal })

export const updateAiConversationTitle = (id: number, title: string) =>
  request<AiConversationDetail>(`/api/ai/conversations/${id}`, {
    method: 'PUT',
    body: JSON.stringify({ title }),
  })

export const deleteAiConversation = (id: number) =>
  request<{ success: boolean }>(`/api/ai/conversations/${id}`, { method: 'DELETE' })

// Note: POST /api/intelligence/chat persists both the user message and the
// assistant reply itself — never append turn messages from the client.

/** Compare-and-set on the server: applied=false when the conversation already had a title */
export const suggestAiConversationTitle = (id: number) =>
  request<{ title: string | null; applied?: boolean }>(`/api/ai/conversations/${id}/suggest-title`, { method: 'POST' })

// --- Mentions ---

export const getMentions = (params?: { days?: number; groupName?: string; tagName?: string; channel?: string }, signal?: AbortSignal) => {
  const qs = new URLSearchParams()
  if (params?.channel) qs.set('channel', params.channel)
  if (params?.days) qs.set('days', String(params.days))
  if (params?.groupName) qs.set('group_name', params.groupName)
  if (params?.tagName) qs.set('tag_name', params.tagName)
  const q = qs.toString()
  return request<MentionGroup[]>(`/api/intelligence/mentions${q ? `?${q}` : ''}`, { signal })
}

// --- Catch Up ---

export const getCatchUpItems = () =>
  request<CatchUpItem[]>('/api/catchup/items')

export const snoozeCatchUpItem = (conversationId: number, hours = 24) =>
  request<CatchUpItem>(`/api/catchup/${conversationId}/snooze`, {
    method: 'POST',
    body: JSON.stringify({ hours }),
  })

export const markCatchUpHandled = (conversationId: number) =>
  request<CatchUpItem>(`/api/catchup/${conversationId}/handled`, {
    method: 'POST',
    body: JSON.stringify({}),
  })

export const reopenCatchUpItem = (conversationId: number) =>
  request<CatchUpItem>(`/api/catchup/${conversationId}/reopen`, {
    method: 'POST',
    body: JSON.stringify({}),
  })

/** Set the note. A blank note is a no-op on the server — use clearCatchUpNote to remove it. */
export const addCatchUpNote = (conversationId: number, note: string) =>
  request<CatchUpItem>(`/api/catchup/${conversationId}/note`, {
    method: 'POST',
    body: JSON.stringify({ note }),
  })

export const clearCatchUpNote = (conversationId: number) =>
  request<CatchUpItem>(`/api/catchup/${conversationId}/note`, {
    method: 'POST',
    body: JSON.stringify({ clear: true }),
  })

export const updateCatchUpRelationship = (conversationId: number, status: string) =>
  request<{ person_id: number; relationship_status: string }>(`/api/catchup/${conversationId}/relationship-status`, {
    method: 'POST',
    body: JSON.stringify({ status }),
  })

/** Starts (or joins) the background summary job; returns its status immediately (202). */
export const triggerCatchUpSummaries = (conversationIds?: number[]) =>
  request<CatchUpSummaryJob>('/api/catchup/generate-summaries', {
    method: 'POST',
    body: JSON.stringify(conversationIds ? { conversation_ids: conversationIds } : {}),
  })

export const getCatchUpSummaryStatus = () =>
  request<CatchUpSummaryJob>('/api/catchup/generate-summaries/status')

export const setCatchUpSummaryInterval = (interval_minutes: number) =>
  request<{ success: boolean; interval_minutes: number }>('/api/settings/prefs/catchup-summary-interval', {
    method: 'POST',
    body: JSON.stringify({ interval_minutes }),
  })

// --- User Organizations ---

export const getUserOrganizations = () =>
  request<{ organizations: string[] }>('/api/settings/prefs/user-organizations')

export const setUserOrganizations = (organizations: string[]) =>
  request<{ success: boolean; organizations: string[] }>('/api/settings/prefs/user-organizations', {
    method: 'PUT',
    body: JSON.stringify({ organizations }),
  })

// --- Dashboard ---

export const getDashboard = (signal?: AbortSignal) =>
  request<DashboardData>('/api/dashboard', { signal })

// --- Global Search ---

export const globalSearch = (query: string, limit = 20, signal?: AbortSignal) =>
  request<GlobalSearchResults>(`/api/search?q=${encodeURIComponent(query)}&limit=${limit}`, { signal })

// --- Media Download ---

/** Fetch one attachment from its channel; resolves to the served URL */
export const downloadAttachment = (attachmentId: number, signal?: AbortSignal) =>
  request<{ url: string; kind: string }>(`/api/media-download/${attachmentId}`, {
    method: 'POST',
    signal,
  })

// --- Admin (capability 'admin_moderation'; target users are identities) ---

export const promoteAdmin = (conversationId: number, identityId: number, permissions: AdminPermissions) =>
  request<{ success: boolean }>(`/api/admin/${conversationId}/promote`, {
    method: 'POST',
    body: JSON.stringify({ identity_id: identityId, permissions }),
  })

export const demoteAdmin = (conversationId: number, identityId: number) =>
  request<{ success: boolean }>(`/api/admin/${conversationId}/demote`, {
    method: 'POST',
    body: JSON.stringify({ identity_id: identityId }),
  })

export const banMember = (conversationId: number, identityId: number) =>
  request<{ success: boolean }>(`/api/admin/${conversationId}/ban`, {
    method: 'POST',
    body: JSON.stringify({ identity_id: identityId }),
  })

export const restrictMember = (conversationId: number, identityId: number, restrictions: Record<string, boolean | number | null>) =>
  request<{ success: boolean }>(`/api/admin/${conversationId}/restrict`, {
    method: 'POST',
    body: JSON.stringify({ identity_id: identityId, ...restrictions }),
  })

export const approveMember = (conversationId: number, identityId: number) =>
  request<{ success: boolean }>(`/api/admin/${conversationId}/approve`, {
    method: 'POST',
    body: JSON.stringify({ identity_id: identityId }),
  })

export const getInviteLinks = (conversationId: number) =>
  request<InviteLink[]>(`/api/admin/${conversationId}/invite-links`)

export const createInviteLink = (conversationId: number, options?: { expire_date?: number; usage_limit?: number; title?: string; request_needed?: boolean }) =>
  request<InviteLink>(`/api/admin/${conversationId}/invite-links`, {
    method: 'POST',
    body: JSON.stringify(options ?? {}),
  })

export const revokeInviteLink = (conversationId: number, link: string) =>
  request<{ success: boolean }>(`/api/admin/${conversationId}/invite-links/revoke`, {
    method: 'POST',
    body: JSON.stringify({ link }),
  })

export const getGroupSettings = (conversationId: number) =>
  request<GroupSettings>(`/api/admin/${conversationId}/settings`)

export const updateGroupSettings = (conversationId: number, settings: { title?: string; about?: string }) =>
  request<{ success: boolean; updated: string[] }>(`/api/admin/${conversationId}/settings`, {
    method: 'PUT',
    body: JSON.stringify(settings),
  })

export const getAdminActionLog = (conversationId: number, limit = 50) =>
  request<AdminActionLog[]>(`/api/admin/${conversationId}/action-log?limit=${limit}`)

// ── Scam Detection ──

export const getScamDetectionSettings = () =>
  request<{ mode: string; api_url: string; api_key: string; api_key_set: boolean; max_tokens: number }>('/api/settings/prefs/scam-detection')

export const setScamDetectionSettings = (mode: string, apiUrl: string, apiKey: string, maxTokens: number = 200) =>
  request<{ success: boolean; mode: string }>('/api/settings/prefs/scam-detection', {
    method: 'POST',
    body: JSON.stringify({ mode, api_url: apiUrl, api_key: apiKey, max_tokens: maxTokens }),
  })

export const getScamFlags = (includeDismissed = false) =>
  request<{ flags: ScamFlag[]; flagged_identity_ids: number[]; flagged_person_ids: number[]; flagged_conversation_ids: number[] }>(
    `/api/scam/flags${includeDismissed ? '?include_dismissed=true' : ''}`,
  )

/** Sender-scope verdict: 'scam' flags, 'not_scam' dismisses (+ marks unmarked flagged messages), null resets. */
export const setSenderVerdict = (identityId: number, verdict: ScamManualVerdict) =>
  request<ScamVerdictResponse>(`/api/scam/senders/${identityId}/verdict`, {
    method: 'PUT',
    body: JSON.stringify({ verdict }),
  })

export const getConversationScamStatus = (conversationId: number) =>
  request<{ has_scam_flags: boolean; flagged_message_count: number; flagged_message_ids: number[]; flagged_users: { identity_id: number; person_id: number | null; confidence: number | null; reason: string | null; source: string }[] }>(`/api/scam/status/${conversationId}`)

export const checkMessagesForScam = (conversationId: number, messageIds: number[]) =>
  request<{ status: 'queued'; message_count: number; queued_ids: number[]; already_running_ids: number[]; missing_ids: number[] }>('/api/scam/check-messages', {
    method: 'POST',
    body: JSON.stringify({ conversation_id: conversationId, message_ids: messageIds }),
  })

export interface ScannedMessageFilters {
  sender_identity_id?: number
  conversation_id?: number
  min_members?: number
  verdict_filter?: ScamVerdictFilter
}

function scannedFilterParams(filters?: ScannedMessageFilters): URLSearchParams {
  const params = new URLSearchParams()
  if (filters?.sender_identity_id) params.set('sender_identity_id', String(filters.sender_identity_id))
  if (filters?.conversation_id) params.set('conversation_id', String(filters.conversation_id))
  if (filters?.min_members) params.set('min_members', String(filters.min_members))
  if (filters?.verdict_filter && filters.verdict_filter !== 'all') params.set('verdict_filter', filters.verdict_filter)
  return params
}

/** Keyset-paginated (newest first). Pass the previous page's next_cursor to load more. */
export const getScannedMessages = (
  opts: { cursor?: string | null; pageSize?: number; filters?: ScannedMessageFilters; signal?: AbortSignal } = {},
) => {
  const params = scannedFilterParams(opts.filters)
  params.set('page_size', String(opts.pageSize ?? 200))
  if (opts.cursor) params.set('cursor', opts.cursor)
  return request<{ messages: ScannedMessage[]; total: number; page: number; page_size: number; next_cursor: string | null }>(
    `/api/scam/scanned-messages?${params}`, { signal: opts.signal },
  )
}

/** Message-scope verdict: 'scam' | 'not_scam' | null (null clears the manual verdict). */
export const setMessageVerdict = (messages: { message_id: number; conversation_id?: number }[], verdict: ScamManualVerdict) =>
  request<ScamVerdictResponse>('/api/scam/scanned-messages/verdict', {
    method: 'PUT',
    body: JSON.stringify({ messages, verdict }),
  })

/** Exports every matching row (no cap). Resolves to how many rows were written and whether the file was saved. */
export const exportScannedMessages = async (filters?: ScannedMessageFilters) => {
  const params = scannedFilterParams(filters)

  // Backend writes to temp file and returns path
  const result = await request<{ temp_path: string; filename: string; count: number }>(
    `/api/scam/scanned-messages/export?${params}`,
  )

  // In pywebview: open native save dialog
  const pywebview = (window as any).pywebview
  if (pywebview?.api?.save_file_dialog) {
    const saved = await pywebview.api.save_file_dialog(result.temp_path, result.filename)
    return { saved: !!saved, count: result.count, path: saved }
  }

  // Dev mode (browser): download the temp file via a download endpoint
  const blob = await requestBlob(`/api/scam/scanned-messages/export/download?path=${encodeURIComponent(result.temp_path)}`)
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = result.filename
  a.click()
  setTimeout(() => URL.revokeObjectURL(a.href), 0)
  return { saved: true, count: result.count, path: null }
}

export const getScamPipeline = () =>
  request<{
    version: string
    updated_at: string
    categories: ScamCategory[]
    nodes: { id: string; type: string; config: Record<string, any>; short_circuit?: any }[]
    edges: { from: string; to: string }[]
    output: string
  }>('/api/scam/pipeline')

export const reloadScamPipeline = () =>
  request<{ status: string; version: string }>('/api/scam/pipeline/reload', { method: 'POST' })

export const getReportContext = (conversationId: number, targetMessageId: number) =>
  request<{ messages: ReportContextMessage[] }>(`/api/scam/reports/context?conversation_id=${conversationId}&target_message_id=${targetMessageId}`)

export const createScamReport = (data: {
  reported_identity_id: number
  reported_in_conversation_id: number
  reporter_verdict: string
  scam_category?: string
  custom_category?: string
  included_message_ids: number[]
}) =>
  request<{ report_id: string; status: string; created_at: string }>('/api/scam/reports', {
    method: 'POST',
    body: JSON.stringify(data),
  })

export const getScamReports = () =>
  request<{ reports: ScamReport[] }>('/api/scam/reports')

export const triggerMassScan = (maxMessages: number = 200, maxAgeHours: number = 48) =>
  request<{ status: string; messages_queued: number; queue?: ScamQueueStats }>(`/api/scam/mass-scan?max_messages=${maxMessages}&max_age_hours=${maxAgeHours}`, { method: 'POST' })

// ── Shield Cloud Sync ──

export const getShieldCloudSettings = () =>
  request<{ cloud_url: string; api_key_set: boolean; last_synced_at: string | null; sync_dismissals: boolean }>('/api/settings/prefs/shield-cloud')

export const setShieldCloudSettings = (cloudUrl: string, apiKey: string, syncDismissals?: boolean) =>
  request<{ success: boolean }>('/api/settings/prefs/shield-cloud', {
    method: 'POST',
    body: JSON.stringify({ cloud_url: cloudUrl, api_key: apiKey, sync_dismissals: syncDismissals }),
  })

export const getScanQueue = () => request<ScamQueueStats>('/api/scam/queue')

export const getSyncStatus = () =>
  request<{ configured: boolean; cloud_url: string; url_error: string | null; running: boolean; last_synced_at: string | null; pipeline_version: string | null }>('/api/scam/sync/status')

/** Concurrent calls join the single sync in flight on the server, so every caller gets the same result. */
export const triggerSync = () =>
  request<CloudSyncResult>('/api/scam/pipeline/sync', { method: 'POST' })

export const getSyncHistory = () =>
  request<import('./types').SyncHistoryStats>('/api/scam/sync/history')

export const submitFeedback = (data: {
  feedback_type: string
  message: string
  related_conversation_id?: number
  related_message_id?: number
  related_flag_identity_id?: number
}) =>
  request<{ id: number; status: string; sync_error: string | null; created_at: string }>('/api/scam/feedback', {
    method: 'POST',
    body: JSON.stringify(data),
  })

export const getFeedbackList = () =>
  request<{ feedback: import('./types').ScamFeedback[] }>('/api/scam/feedback')

// ── Translation ──

export const openExternalUrl = (url: string) => {
  const pywebview = (window as any).pywebview
  if (pywebview?.api?.open_url) {
    return pywebview.api.open_url(url)
  }
  return request<{ status: string }>('/api/open-url', {
    method: 'POST',
    body: JSON.stringify({ url }),
  }).catch(() => { window.open(url, '_blank') })
}

export const translateMessage = (text: string, targetLanguage: string) =>
  request<{ translation: string }>('/api/translate', {
    method: 'POST',
    body: JSON.stringify({ text, target_language: targetLanguage }),
  })

export const setTranslationLanguage = (language: string) =>
  request<{ success: boolean; language: string }>('/api/settings/prefs/translation-language', {
    method: 'POST',
    body: JSON.stringify({ language }),
  })

// ── MCP Server ──

export const getMcpSettings = () =>
  request<import('./types').McpSettings>('/api/settings/prefs/mcp')

export const setMcpEnabled = (enabled: boolean) =>
  request<{ success: boolean; enabled: boolean; key_set: boolean }>('/api/settings/prefs/mcp', {
    method: 'POST',
    body: JSON.stringify({ enabled }),
  })

export const generateMcpKey = () =>
  request<{ key: string }>('/api/settings/prefs/mcp/generate-key', { method: 'POST' })

export const revealMcpKey = () =>
  request<{ key: string }>('/api/settings/prefs/mcp/reveal-key', { method: 'POST' })

export const setMcpWriteEnabled = (enabled: boolean) =>
  request<{ write_enabled: boolean }>('/api/settings/prefs/mcp/write', {
    method: 'POST',
    body: JSON.stringify({ enabled }),
  })

// ── DM archive ──

export const getArchiveStatus = (signal?: AbortSignal) =>
  request<import('./types').ArchiveStatus>('/api/archive/status', { signal })

export const startArchive = () =>
  request<import('./types').ArchiveStatus>('/api/archive/start', { method: 'POST' })

export const pauseArchive = () =>
  request<import('./types').ArchiveStatus>('/api/archive/pause', { method: 'POST' })

/** Queue a full-history walk of one conversation (DM, group or channel) */
export const archiveConversation = (conversationId: number) =>
  request<import('./types').ChatArchiveResponse>(`/api/conversations/${conversationId}/archive-history`, { method: 'POST' })

// ── Person notes ──

export const getPersonNotes = (personId: number, signal?: AbortSignal) =>
  request<import('./types').PersonNote[]>(`/api/people/${personId}/notes`, { signal })

export const createPersonNote = (personId: number, note: import('./types').PersonNoteInput) =>
  request<import('./types').PersonNote>(`/api/people/${personId}/notes`, {
    method: 'POST',
    body: JSON.stringify(note),
  })

export const updatePersonNote = (personId: number, noteId: number, note: Partial<import('./types').PersonNoteInput>) =>
  request<import('./types').PersonNote>(`/api/people/${personId}/notes/${noteId}`, {
    method: 'PUT',
    body: JSON.stringify(note),
  })

export const deletePersonNote = (personId: number, noteId: number) =>
  request<{ success: boolean } | undefined>(`/api/people/${personId}/notes/${noteId}`, { method: 'DELETE' })

// ── Features ────────────────────────────────────────────────────────────────

export function getFeatures(signal?: AbortSignal): Promise<Features> {
  return request<Features>('/api/features', { signal })
}
