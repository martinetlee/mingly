import type { ContactsPage, Features, PersonDetail } from './types'
import type { BackfillResult, ChatSyncResult, MessageWindow, SendFilesResponse } from './types'
import type { AdminActionLog, AdminPermissions, AuthStatus, BoardColumn, BoardData, BriefingJobStatus, BriefingRun, CatchUpItem, CatchUpSummaryJob, ChannelPostStats, Chat, ChatMember, Contact, ContactPosition, ConversationDetail, ConversationSummary, DashboardData, Folder, ForumTopic, GlobalSearchResults, GroupSettings, InviteLink, MentionGroup, Message, OrgDetail, OrgSummary, PersonSummary, PinnedMessage, Prefs, ScamFlag, ScannedMessage, ScamCategory, ScamManualVerdict, ScamVerdictResponse, ScamVerdictFilter, ScamQueueStats, CloudSyncResult, ScamReport, ReportContextMessage, SharedMediaItem, Tag, TagSuggestion, TelegramProfile, TelegramSearchResult, UserPresence, Workspace } from './types'
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

// --- Chats ---

export const getChats = (limit = 50, offset = 0, sortBy = 'recent', tagId?: number, workspaceId?: number, signal?: AbortSignal) =>
  request<Chat[]>(`/api/chats?limit=${limit}&offset=${offset}&sort_by=${sortBy}${tagId != null ? `&tag_id=${tagId}` : ''}${workspaceId != null ? `&workspace_id=${workspaceId}` : ''}`, { signal })

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
export const moveBoardChat = (workspaceId: number, boardId: number, chatId: number, toTagId: number | null) =>
  request<BoardData>(`/api/workspaces/${workspaceId}/move`, { method: 'POST', body: JSON.stringify({ board_id: boardId, chat_id: chatId, to_tag_id: toTagId }) })

export const getChatsByIds = (ids: number[]) =>
  request<Chat[]>('/api/chats/by-ids', {
    method: 'POST',
    body: JSON.stringify({ ids }),
  })

export const searchChatsLocal = (q: string) =>
  request<{ local: Chat[] }>(`/api/chats/search?q=${encodeURIComponent(q)}`)

export const searchChatsTelegram = (q: string, signal?: AbortSignal) =>
  request<Chat[]>(`/api/chats/search/telegram?q=${encodeURIComponent(q)}`, { signal })

export interface MessageSearchResult {
  telegram_id: number
  name: string
  type: string
  messages: { tg_message_id: number; text: string; sender_name: string | null; date: string | null }[]
}

export const searchMessagesGlobal = (q: string, signal?: AbortSignal) =>
  request<MessageSearchResult[]>(`/api/chats/search/messages?q=${encodeURIComponent(q)}`, { signal })

export const bulkSync = () =>
  request<{ synced: number }>('/api/chats/bulk-sync', { method: 'POST' })

export const getFolders = () =>
  request<Folder[]>('/api/chats/folders')

export interface MessageWindowParams {
  limit?: number
  /** Cursor: the `limit` messages immediately older than it */
  before?: string | null
  /** Cursor: the `limit` messages immediately newer than it */
  after?: string | null
  /** tg_message_id to centre the window on */
  anchor?: number | null
  /** ISO datetime: centre on the first message at/after it */
  date?: string | null
  /** Forum topic scope (1 = General, includes untagged rows) */
  topicId?: number | null
}

/** Keyset message window (oldest → newest). Use at most one of before/after/anchor/date. */
export const getMessageWindow = (telegram_id: number, p: MessageWindowParams = {}, signal?: AbortSignal) => {
  const params = new URLSearchParams()
  if (p.limit != null) params.set('limit', String(p.limit))
  if (p.before) params.set('before', p.before)
  if (p.after) params.set('after', p.after)
  if (p.anchor != null) params.set('anchor', String(p.anchor))
  if (p.date) params.set('date', p.date)
  if (p.topicId != null) params.set('topic_id', String(p.topicId))
  return request<MessageWindow>(`/api/chats/${telegram_id}/messages/window?${params}`, { signal })
}

/** Fetch history older than the oldest stored message from Telegram. */
export const backfillChat = (telegram_id: number, opts: { limit?: number; topicId?: number | null } = {}, signal?: AbortSignal) => {
  const params = new URLSearchParams({ limit: String(opts.limit ?? 100) })
  if (opts.topicId != null) params.set('topic_id', String(opts.topicId))
  return request<BackfillResult>(`/api/chats/${telegram_id}/backfill?${params}`, { method: 'POST', signal })
}

export const searchChatMessages = (telegram_id: number, query: string, limit = 50, signal?: AbortSignal) =>
  request<Message[]>(`/api/chats/${telegram_id}/messages/search?q=${encodeURIComponent(query)}&limit=${limit}`, { signal })

export const syncChat = (telegram_id: number, limit?: number) =>
  request<ChatSyncResult>(`/api/chats/${telegram_id}/sync${limit != null ? `?limit=${limit}` : ''}`, { method: 'POST' })

export const getChatMembers = (telegram_id: number, limit = 100) =>
  request<ChatMember[]>(`/api/chats/${telegram_id}/members?limit=${limit}`)

export const getSharedMedia = (telegram_id: number, mediaFilter: 'media' | 'files' | 'links' = 'media', limit = 50, offset = 0, signal?: AbortSignal) =>
  request<SharedMediaItem[]>(`/api/chats/${telegram_id}/shared-media?media_filter=${mediaFilter}&limit=${limit}&offset=${offset}`, { signal })

export const markChatRead = (telegram_id: number) =>
  request<{ success: boolean }>(`/api/chats/${telegram_id}/mark-read`, { method: 'POST' })

export const getPinnedMessage = async (telegram_id: number, signal?: AbortSignal): Promise<PinnedMessage | null> => {
  const msgs = await request<PinnedMessage[]>(`/api/chats/${telegram_id}/pinned`, { signal })
  return msgs && msgs.length > 0 ? msgs[0] : null
}

export const getHiddenChats = () =>
  request<Chat[]>('/api/chats/hidden')

export const getArchivedChats = () =>
  request<Chat[]>('/api/chats/archived')

export const setPinned = (telegram_id: number, pinned: boolean) =>
  request<{ telegram_id: number; is_pinned: boolean }>(`/api/chats/${telegram_id}/pin`, {
    method: 'POST',
    body: JSON.stringify({ pinned }),
  })

export const setArchived = (telegram_id: number, archived: boolean) =>
  request<{ telegram_id: number; is_archived: boolean }>(`/api/chats/${telegram_id}/archive`, {
    method: 'POST',
    body: JSON.stringify({ archived }),
  })

export const setMuted = (telegram_id: number, muted: boolean, duration?: string) =>
  request<{ telegram_id: number; is_muted: boolean; muted_until: string | null }>(`/api/chats/${telegram_id}/mute`, {
    method: 'POST',
    body: JSON.stringify({ muted, duration }),
  })

export const setMarkedUnread = (telegram_id: number, marked_unread: boolean) =>
  request<{ telegram_id: number; is_marked_unread: boolean }>(`/api/chats/${telegram_id}/mark-unread`, {
    method: 'POST',
    body: JSON.stringify({ marked_unread }),
  })

export const setHidden = (telegram_id: number, hidden: boolean) =>
  request<{ telegram_id: number; is_hidden: boolean }>(`/api/chats/${telegram_id}/hide`, {
    method: 'POST',
    body: JSON.stringify({ hidden }),
  })

export const getTrackedChats = () =>
  request<Chat[]>('/api/chats/tracked')

export const setTracked = (telegram_id: number, tracked: boolean) =>
  request<{ telegram_id: number; is_tracked: boolean }>(`/api/chats/${telegram_id}/track`, {
    method: 'POST',
    body: JSON.stringify({ tracked }),
  })

export const getBriefingChats = () =>
  request<Chat[]>('/api/chats/briefing')

export const setBriefing = (telegram_id: number, briefing: boolean) =>
  request<{ telegram_id: number; is_briefing: boolean }>(`/api/chats/${telegram_id}/briefing`, {
    method: 'POST',
    body: JSON.stringify({ briefing }),
  })

export const reactToMessage = (chat_telegram_id: number, msg_tg_id: number, emoji: string | null) =>
  request<{ success: boolean; my_reaction: string | null }>(`/api/chats/${chat_telegram_id}/messages/${msg_tg_id}/react`, {
    method: 'POST',
    body: JSON.stringify({ emoji }),
  })

export const getUserPresence = (telegram_id: number, signal?: AbortSignal) =>
  request<UserPresence>(`/api/chats/${telegram_id}/presence`, { signal })

export const sendTypingAction = (telegram_id: number) =>
  request<{ success: boolean }>(`/api/chats/${telegram_id}/typing`, { method: 'POST' })

// --- Forum Topics ---

export const getForumTopics = (telegram_id: number) =>
  request<ForumTopic[]>(`/api/chats/${telegram_id}/topics`)

export const createForumTopic = (telegram_id: number, title: string) =>
  request<{ success: boolean; topic_id: number | null }>(`/api/chats/${telegram_id}/topics`, {
    method: 'POST',
    body: JSON.stringify({ title }),
  })

export const setTopicState = (telegram_id: number, topic_id: number, closed: boolean) =>
  request<{ success: boolean; topic_id: number; closed: boolean }>(`/api/chats/${telegram_id}/topics/${topic_id}/state`, {
    method: 'POST',
    body: JSON.stringify({ closed }),
  })

// --- Slow Mode ---

export const setSlowMode = (telegram_id: number, seconds: number) =>
  request<{ success: boolean; slow_mode_seconds: number }>(`/api/chats/${telegram_id}/slow-mode`, {
    method: 'POST',
    body: JSON.stringify({ seconds }),
  })

// --- Pin/Unpin Messages ---

export const pinMessage = (telegram_id: number, msg_tg_id: number, pinned: boolean, notify = false) =>
  request<{ success: boolean; is_pinned: boolean }>(`/api/chats/${telegram_id}/messages/${msg_tg_id}/pin`, {
    method: 'POST',
    body: JSON.stringify({ pinned, notify }),
  })

export const getPinnedMessages = (telegram_id: number) =>
  request<Message[]>(`/api/chats/${telegram_id}/pinned`)

// --- Channel Post Stats ---

export const getPostStats = (telegram_id: number, limit = 20) =>
  request<ChannelPostStats[]>(`/api/chats/${telegram_id}/post-stats?limit=${limit}`)

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

export const getChatTags = (telegram_id: number) =>
  request<Tag[]>(`/api/chats/${telegram_id}/tags`)

export const addTagToChat = (telegram_id: number, tag_id: number) =>
  request<{ success: boolean }>(`/api/chats/${telegram_id}/tags`, {
    method: 'POST',
    body: JSON.stringify({ tag_id }),
  })

export const removeTagFromChat = (telegram_id: number, tag_id: number) =>
  request<{ success: boolean }>(`/api/chats/${telegram_id}/tags/${tag_id}`, { method: 'DELETE' })

export const suggestTags = (telegram_id: number) =>
  request<TagSuggestion[]>(`/api/chats/${telegram_id}/tags/suggest`, { method: 'POST' })

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

// --- Contacts ---

export const syncContacts = () =>
  request<{ imported: number; total: number }>('/api/contacts/sync', { method: 'POST' })

export const getContactByTelegram = (telegramId: number, signal?: AbortSignal) =>
  request<Contact>(`/api/contacts/by-telegram/${telegramId}`, { signal })

export const createContact = (data: { telegram_id: number; first_name?: string; last_name?: string; username?: string }) =>
  request<Contact>('/api/contacts', { method: 'POST', body: JSON.stringify(data) })

export const getContactsPage = (
  params: { search?: string; status?: string; important?: boolean; limit?: number; offset?: number } = {},
  signal?: AbortSignal,
) => {
  const qs = new URLSearchParams({
    search: params.search ?? '',
    limit: String(params.limit ?? 100),
    offset: String(params.offset ?? 0),
  })
  if (params.status && params.status !== 'all') qs.set('status', params.status)
  if (params.important) qs.set('important', 'true')
  return request<ContactsPage>(`/api/contacts?${qs}`, { signal })
}

/** First page of matches only — use getContactsPage for lists */
export const getContacts = (search = '', limit = 100, offset = 0) =>
  getContactsPage({ search, limit, offset }).then((page) => page.items)

export const getContact = (contactId: number) =>
  request<Contact>(`/api/contacts/${contactId}`)

export const updateContact = (contactId: number, data: { twitter_handle?: string; linkedin_url?: string; phone?: string; notes?: string; is_important?: boolean }) =>
  request<Contact>(`/api/contacts/${contactId}`, { method: 'PUT', body: JSON.stringify(data) })

export const setContactImportantByTelegram = (telegramId: number, is_important: boolean) =>
  request<Contact>(`/api/contacts/by-telegram/${telegramId}/important`, {
    method: 'POST',
    body: JSON.stringify({ is_important }),
  })

// --- People (local DB, keyed by telegram_id; works for non-contacts and offline) ---

export const getPerson = (telegramId: number, signal?: AbortSignal) =>
  request<PersonDetail>(`/api/people/${telegramId}`, { signal })

export const addPosition = (
  telegramId: number,
  organization: string,
  title?: string,
  is_active = true,
  ended_at?: string,
) =>
  request<ContactPosition>(`/api/people/${telegramId}/positions`, {
    method: 'POST',
    body: JSON.stringify({ organization, title: title || null, is_active, ended_at: ended_at || null }),
  })

/** `ended_at: ""` clears the end date */
export const updatePosition = (
  telegramId: number,
  positionId: number,
  data: { title?: string; is_active?: boolean; ended_at?: string | null },
) =>
  request<ContactPosition>(`/api/people/${telegramId}/positions/${positionId}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  })

export const removePosition = (telegramId: number, positionId: number) =>
  request<{ success: boolean }>(`/api/people/${telegramId}/positions/${positionId}`, { method: 'DELETE' })

export const getPositions = (telegramId: number, signal?: AbortSignal) =>
  request<ContactPosition[]>(`/api/people/${telegramId}/positions`, { signal })

export const setRelationshipStatus = (contactId: number, status: string) =>
  request<Contact>(`/api/contacts/${contactId}/relationship-status`, { method: 'POST', body: JSON.stringify({ status }) })

export const resetRelationshipStatus = (contactId: number) =>
  request<Contact>(`/api/contacts/${contactId}/relationship-status/auto`, { method: 'POST' })

export const resolvePerson = (telegramId: number) =>
  request<PersonSummary>(`/api/contacts/resolve/${telegramId}`)

/** Up to 200 ids per call; ids are returned as string keys */
export const resolvePeople = (telegramIds: number[]) =>
  request<Record<string, PersonSummary>>('/api/contacts/resolve-batch', {
    method: 'POST', body: JSON.stringify({ telegram_ids: telegramIds }),
  })

// --- Profiles ---

// Single attempt: a 503 means "Telegram offline, no cached copy" — retrying
// only delays showing the local data
export const getProfile = (userId: number | string, signal?: AbortSignal, opts: { refresh?: boolean } = {}) =>
  request<TelegramProfile>(`/api/profiles/${encodeURIComponent(String(userId))}${opts.refresh ? '?refresh=true' : ''}`, { signal, attempts: 1 })

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

export const sendMessage = (chat_id: number, text: string, reply_to_id?: number, opts: SendOptions = {}) =>
  request<{ success: boolean; message_id: number; message?: Message }>('/api/compose/send', {
    method: 'POST',
    body: JSON.stringify({
      chat_id, text, reply_to_id,
      topic_id: opts.topicId ?? undefined,
      client_nonce: opts.clientNonce,
    }),
  })

export const editMessage = (chat_id: number, message_id: number, text: string) =>
  request<{ success: boolean; message?: Message }>('/api/compose/edit', {
    method: 'POST',
    body: JSON.stringify({ chat_id, message_id, text }),
  })

export const deleteMessages = (chat_id: number, message_ids: number[], revoke = false) =>
  request<{ success: boolean }>('/api/compose/delete', {
    method: 'POST',
    body: JSON.stringify({ chat_id, message_ids, revoke }),
  })

export const forwardMessages = (from_chat_id: number, message_ids: number[], to_chat_id: number) =>
  request<{ success: boolean }>('/api/compose/forward', {
    method: 'POST',
    body: JSON.stringify({ from_chat_id, message_ids, to_chat_id }),
  })

export function sendFiles(
  chatId: number,
  files: File[],
  text?: string,
  replyToId?: number,
  onProgress?: (fraction: number) => void,
  opts: SendOptions & { signal?: AbortSignal } = {},
): { promise: Promise<SendFilesResponse>; abort: () => void } {
  const formData = new FormData()
  formData.append('chat_id', String(chatId))
  if (text) formData.append('text', text)
  if (replyToId != null) formData.append('reply_to_id', String(replyToId))
  if (opts.topicId != null) formData.append('topic_id', String(opts.topicId))
  if (opts.clientNonce) formData.append('client_nonce', opts.clientNonce)
  for (const file of files) {
    formData.append('files', file)
  }
  // 200 = all sent, 207 = partial (see `files`); 400 (none sent) rejects with ApiError, body.files has per-file errors
  return upload<SendFilesResponse>('/api/compose/send-files', formData, { onProgress, signal: opts.signal })
}

// --- Conversations ---

export const listConversations = () =>
  request<ConversationSummary[]>('/api/conversations/')

export const createConversation = () =>
  request<ConversationDetail>('/api/conversations/', { method: 'POST' })

export const getConversation = (id: number, signal?: AbortSignal) =>
  request<ConversationDetail>(`/api/conversations/${id}`, { signal })

export const updateConversationTitle = (id: number, title: string) =>
  request<ConversationDetail>(`/api/conversations/${id}`, {
    method: 'PUT',
    body: JSON.stringify({ title }),
  })

export const deleteConversation = (id: number) =>
  request<{ success: boolean }>(`/api/conversations/${id}`, { method: 'DELETE' })

// Note: POST /api/intelligence/chat persists both the user message and the
// assistant reply itself — never append turn messages from the client.

/** Compare-and-set on the server: applied=false when the conversation already had a title */
export const suggestConversationTitle = (id: number) =>
  request<{ title: string | null; applied?: boolean }>(`/api/conversations/${id}/suggest-title`, { method: 'POST' })

// --- Mentions ---

export const getMentions = (params?: { days?: number; groupName?: string; tagName?: string }, signal?: AbortSignal) => {
  const qs = new URLSearchParams()
  if (params?.days) qs.set('days', String(params.days))
  if (params?.groupName) qs.set('group_name', params.groupName)
  if (params?.tagName) qs.set('tag_name', params.tagName)
  const q = qs.toString()
  return request<MentionGroup[]>(`/api/intelligence/mentions${q ? `?${q}` : ''}`, { signal })
}

// --- Catch Up ---

export const getCatchUpItems = () =>
  request<CatchUpItem[]>('/api/catchup/items')

export const snoozeCatchUpItem = (chatTelegramId: number, hours = 24) =>
  request<CatchUpItem>(`/api/catchup/${chatTelegramId}/snooze`, {
    method: 'POST',
    body: JSON.stringify({ hours }),
  })

export const markCatchUpHandled = (chatTelegramId: number) =>
  request<CatchUpItem>(`/api/catchup/${chatTelegramId}/handled`, {
    method: 'POST',
    body: JSON.stringify({}),
  })

export const reopenCatchUpItem = (chatTelegramId: number) =>
  request<CatchUpItem>(`/api/catchup/${chatTelegramId}/reopen`, {
    method: 'POST',
    body: JSON.stringify({}),
  })

/** Set the note. A blank note is a no-op on the server — use clearCatchUpNote to remove it. */
export const addCatchUpNote = (chatTelegramId: number, note: string) =>
  request<CatchUpItem>(`/api/catchup/${chatTelegramId}/note`, {
    method: 'POST',
    body: JSON.stringify({ note }),
  })

export const clearCatchUpNote = (chatTelegramId: number) =>
  request<CatchUpItem>(`/api/catchup/${chatTelegramId}/note`, {
    method: 'POST',
    body: JSON.stringify({ clear: true }),
  })

export const updateCatchUpRelationship = (chatTelegramId: number, status: string) =>
  request<{ contact_id: number; relationship_status: string }>(`/api/catchup/${chatTelegramId}/relationship-status`, {
    method: 'POST',
    body: JSON.stringify({ status }),
  })

/** Starts (or joins) the background summary job; returns its status immediately (202). */
export const triggerCatchUpSummaries = (chatIds?: number[]) =>
  request<CatchUpSummaryJob>('/api/catchup/generate-summaries', {
    method: 'POST',
    body: JSON.stringify(chatIds ? { chat_ids: chatIds } : {}),
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

export const telegramSearch = (query: string, limit = 20, signal?: AbortSignal) =>
  request<{ telegram_dialogs: TelegramSearchResult[] }>(`/api/search/telegram?q=${encodeURIComponent(query)}&limit=${limit}`, { signal })

export const ensureChat = (telegramId: number) =>
  request<Chat>(`/api/chats/${telegramId}/ensure`, { method: 'POST' })

// --- Media Download ---

export const downloadMedia = (chatTelegramId: number, msgTelegramId: number, signal?: AbortSignal) =>
  request<{ media_url: string; media_type: string }>(`/api/media-download/${chatTelegramId}/${msgTelegramId}`, {
    method: 'POST',
    signal,
  })

// --- Admin ---

export const promoteAdmin = (telegramId: number, userId: number, permissions: AdminPermissions) =>
  request<{ success: boolean }>(`/api/admin/${telegramId}/promote`, {
    method: 'POST',
    body: JSON.stringify({ user_id: userId, permissions }),
  })

export const demoteAdmin = (telegramId: number, userId: number) =>
  request<{ success: boolean }>(`/api/admin/${telegramId}/demote`, {
    method: 'POST',
    body: JSON.stringify({ user_id: userId }),
  })

export const banMember = (telegramId: number, userId: number) =>
  request<{ success: boolean }>(`/api/admin/${telegramId}/ban`, {
    method: 'POST',
    body: JSON.stringify({ user_id: userId }),
  })

export const restrictMember = (telegramId: number, userId: number, restrictions: Record<string, boolean | number | null>) =>
  request<{ success: boolean }>(`/api/admin/${telegramId}/restrict`, {
    method: 'POST',
    body: JSON.stringify({ user_id: userId, ...restrictions }),
  })

export const approveMember = (telegramId: number, userId: number) =>
  request<{ success: boolean }>(`/api/admin/${telegramId}/approve`, {
    method: 'POST',
    body: JSON.stringify({ user_id: userId }),
  })

export const getInviteLinks = (telegramId: number) =>
  request<InviteLink[]>(`/api/admin/${telegramId}/invite-links`)

export const createInviteLink = (telegramId: number, options?: { expire_date?: number; usage_limit?: number; title?: string; request_needed?: boolean }) =>
  request<InviteLink>(`/api/admin/${telegramId}/invite-links`, {
    method: 'POST',
    body: JSON.stringify(options ?? {}),
  })

export const revokeInviteLink = (telegramId: number, link: string) =>
  request<{ success: boolean }>(`/api/admin/${telegramId}/invite-links/revoke`, {
    method: 'POST',
    body: JSON.stringify({ link }),
  })

export const getGroupSettings = (telegramId: number) =>
  request<GroupSettings>(`/api/admin/${telegramId}/settings`)

export const updateGroupSettings = (telegramId: number, settings: { title?: string; about?: string }) =>
  request<{ success: boolean; updated: string[] }>(`/api/admin/${telegramId}/settings`, {
    method: 'PUT',
    body: JSON.stringify(settings),
  })

export const getAdminActionLog = (telegramId: number, limit = 50) =>
  request<AdminActionLog[]>(`/api/admin/${telegramId}/action-log?limit=${limit}`)

// --- Create Chats ---

export const startPrivateChat = (telegramId: number) =>
  request<Chat>('/api/chats/create/private', {
    method: 'POST',
    body: JSON.stringify({ telegram_id: telegramId }),
  })

export const createGroup = (name: string, memberIds: number[]) =>
  request<Chat>('/api/chats/create/group', {
    method: 'POST',
    body: JSON.stringify({ name, member_ids: memberIds }),
  })

export const createChannel = (name: string, about: string = '', isPublic: boolean = false) =>
  request<Chat>('/api/chats/create/channel', {
    method: 'POST',
    body: JSON.stringify({ name, about, is_public: isPublic }),
  })

export const joinViaInviteLink = (link: string) =>
  request<Chat>('/api/chats/join', {
    method: 'POST',
    body: JSON.stringify({ link }),
  })

// ── Scam Detection ──

export const getScamDetectionSettings = () =>
  request<{ mode: string; api_url: string; api_key: string; api_key_set: boolean; max_tokens: number }>('/api/settings/prefs/scam-detection')

export const setScamDetectionSettings = (mode: string, apiUrl: string, apiKey: string, maxTokens: number = 200) =>
  request<{ success: boolean; mode: string }>('/api/settings/prefs/scam-detection', {
    method: 'POST',
    body: JSON.stringify({ mode, api_url: apiUrl, api_key: apiKey, max_tokens: maxTokens }),
  })

export const getScamFlags = (includeDismissed = false) =>
  request<{ flags: ScamFlag[]; flagged_sender_ids: number[]; flagged_chat_ids: number[] }>(
    `/api/scam/flags${includeDismissed ? '?include_dismissed=true' : ''}`,
  )

/** Sender-scope verdict: 'scam' flags, 'not_scam' dismisses (+ marks unmarked flagged messages), null resets. */
export const setSenderVerdict = (telegramId: number, verdict: ScamManualVerdict) =>
  request<ScamVerdictResponse>(`/api/scam/senders/${telegramId}/verdict`, {
    method: 'PUT',
    body: JSON.stringify({ verdict }),
  })

export const getChatScamStatus = (chatId: number) =>
  request<{ has_scam_flags: boolean; flagged_message_count: number; flagged_message_ids: number[]; flagged_users: { telegram_id: number; confidence: number | null; reason: string | null; source: string }[] }>(`/api/scam/status/${chatId}`)

export const checkMessagesForScam = (chatId: number, messageTelegramIds: number[]) =>
  request<{ status: 'queued'; message_count: number; queued_ids: number[]; already_running_ids: number[]; missing_ids: number[] }>('/api/scam/check-messages', {
    method: 'POST',
    body: JSON.stringify({ chat_id: chatId, message_telegram_ids: messageTelegramIds }),
  })

export interface ScannedMessageFilters {
  sender_id?: number
  chat_id?: number
  min_members?: number
  verdict_filter?: ScamVerdictFilter
}

function scannedFilterParams(filters?: ScannedMessageFilters): URLSearchParams {
  const params = new URLSearchParams()
  if (filters?.sender_id) params.set('sender_id', String(filters.sender_id))
  if (filters?.chat_id) params.set('chat_id', String(filters.chat_id))
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
export const setMessageVerdict = (
  messages: { chat_id: number; tg_message_id: number }[],
  verdict: ScamManualVerdict,
) =>
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

export const getReportContext = (chatId: number, targetMessageId: number) =>
  request<{ messages: ReportContextMessage[] }>(`/api/scam/reports/context?chat_id=${chatId}&target_message_id=${targetMessageId}`)

export const createScamReport = (data: {
  reported_telegram_id: number
  reported_in_chat_id: number
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
  related_chat_id?: number
  related_message_id?: number
  related_flag_telegram_id?: number
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

// ── Features ────────────────────────────────────────────────────────────────

export function getFeatures(signal?: AbortSignal): Promise<Features> {
  return request<Features>('/api/features', { signal })
}
