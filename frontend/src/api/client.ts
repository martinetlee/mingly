import type { AuthStatus, BriefingHistoryEntry, BriefingRun, CatchUpContext, CatchUpItem, Chat, ChatMember, Contact, ContactPosition, ConversationDetail, ConversationMessageItem, ConversationSummary, DashboardData, Folder, GlobalSearchResults, MentionGroup, Message, OrgDetail, OrgSummary, PersonSummary, Tag, TagSuggestion, TelegramProfile } from './types'
import { getSessionToken, setSessionToken, clearSessionToken, notifySessionExpired } from './session'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options?.headers as Record<string, string> | undefined),
  }
  const token = getSessionToken()
  if (token) {
    headers['X-Session-Token'] = token
  }
  const res = await fetch(path, {
    ...options,
    headers,
  })
  if (res.status === 401 && !path.includes('/api/auth/')) {
    notifySessionExpired()
    throw new Error('Session expired')
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || res.statusText)
  }
  return res.json()
}

// --- Vault ---

export const getVaultStatus = () =>
  request<{ status: 'unset' | 'locked' | 'unlocked' }>('/api/auth/vault-status')

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

export const resetVault = async () => {
  const result = await request<{ success: boolean }>('/api/auth/vault/reset', { method: 'POST' })
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

// --- Chats ---

export const getChats = (limit = 50, offset = 0, sortBy = 'recent') =>
  request<Chat[]>(`/api/chats?limit=${limit}&offset=${offset}&sort_by=${sortBy}`)

export const bulkSync = () =>
  request<{ synced: number }>('/api/chats/bulk-sync', { method: 'POST' })

export const getFolders = () =>
  request<Folder[]>('/api/chats/folders')

export const setMonitored = (telegram_id: number, monitored: boolean) =>
  request<{ telegram_id: number; is_monitored: boolean }>(`/api/chats/${telegram_id}/monitor`, {
    method: 'POST',
    body: JSON.stringify({ monitored }),
  })

export const getMessages = (telegram_id: number, limit = 50, offset = 0, anchorTelegramId?: number) => {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (anchorTelegramId != null) params.set('anchor_telegram_id', String(anchorTelegramId))
  return request<Message[]>(`/api/chats/${telegram_id}/messages?${params}`)
}

export const searchChatMessages = (telegram_id: number, query: string, limit = 50) =>
  request<Message[]>(`/api/chats/${telegram_id}/messages/search?q=${encodeURIComponent(query)}&limit=${limit}`)

export const getChatDateOffset = (telegram_id: number, date: string) =>
  request<{ offset: number }>(`/api/chats/${telegram_id}/messages/date-offset?date=${encodeURIComponent(date)}`)

export const syncChat = (telegram_id: number, limit?: number) =>
  request<{ status: string }>(`/api/chats/${telegram_id}/sync${limit != null ? `?limit=${limit}` : ''}`, { method: 'POST' })

export const getChatMembers = (telegram_id: number, limit = 100) =>
  request<ChatMember[]>(`/api/chats/${telegram_id}/members?limit=${limit}`)

export const markChatRead = (telegram_id: number) =>
  request<{ success: boolean }>(`/api/chats/${telegram_id}/mark-read`, { method: 'POST' })

export const getHiddenChats = () =>
  request<Chat[]>('/api/chats/hidden')

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

export const reactToMessage = (chat_telegram_id: number, msg_telegram_id: number, emoji: string | null) =>
  request<{ success: boolean; my_reaction: string | null }>(`/api/chats/${chat_telegram_id}/messages/${msg_telegram_id}/react`, {
    method: 'POST',
    body: JSON.stringify({ emoji }),
  })

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

export const getOrganizations = (search = '') =>
  request<OrgSummary[]>(`/api/organizations?search=${encodeURIComponent(search)}`)

export const createOrganization = (name: string) =>
  request<OrgDetail>('/api/organizations', { method: 'POST', body: JSON.stringify({ name }) })

export const getOrganization = (orgId: number) =>
  request<OrgDetail>(`/api/organizations/${orgId}`)

export const updateOrganization = (orgId: number, data: { name?: string; context?: string }) =>
  request<OrgDetail>(`/api/organizations/${orgId}`, { method: 'PUT', body: JSON.stringify(data) })

export const deleteOrganization = (orgId: number) =>
  request<{ success: boolean }>(`/api/organizations/${orgId}`, { method: 'DELETE' })

export const autoDiscoverOrgs = () =>
  request<{ created: number; total: number }>('/api/organizations/auto-discover', { method: 'POST' })

export const generateOrgContext = (orgId: number) =>
  request<OrgDetail>(`/api/organizations/${orgId}/generate-context`, { method: 'POST' })

// --- Contacts ---

export const syncContacts = () =>
  request<{ imported: number; total: number }>('/api/contacts/sync', { method: 'POST' })

export const getContactByTelegram = (telegramId: number) =>
  request<Contact>(`/api/contacts/by-telegram/${telegramId}`)

export const createContact = (data: { telegram_id: number; first_name?: string; last_name?: string; username?: string }) =>
  request<Contact>('/api/contacts', { method: 'POST', body: JSON.stringify(data) })

export const getContacts = (search = '', limit = 100, offset = 0) =>
  request<Contact[]>(`/api/contacts?search=${encodeURIComponent(search)}&limit=${limit}&offset=${offset}`)

export const getContact = (contactId: number) =>
  request<Contact>(`/api/contacts/${contactId}`)

export const updateContact = (contactId: number, data: { twitter_handle?: string; linkedin_url?: string; phone?: string; notes?: string; is_important?: boolean }) =>
  request<Contact>(`/api/contacts/${contactId}`, { method: 'PUT', body: JSON.stringify(data) })

export const setContactImportantByTelegram = (telegramId: number, is_important: boolean) =>
  request<Contact>(`/api/contacts/by-telegram/${telegramId}/important`, {
    method: 'POST',
    body: JSON.stringify({ is_important }),
  })

export const addPosition = (
  contactId: number,
  organization: string,
  title?: string,
  is_active = true,
  ended_at?: string,
) =>
  request<ContactPosition>(`/api/contacts/${contactId}/positions`, {
    method: 'POST',
    body: JSON.stringify({ organization, title: title || null, is_active, ended_at: ended_at || null }),
  })

export const updatePosition = (
  contactId: number,
  positionId: number,
  data: { title?: string; is_active?: boolean; ended_at?: string | null },
) =>
  request<ContactPosition>(`/api/contacts/${contactId}/positions/${positionId}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  })

export const removePosition = (contactId: number, positionId: number) =>
  request<{ success: boolean }>(`/api/contacts/${contactId}/positions/${positionId}`, { method: 'DELETE' })

export const setRelationshipStatus = (contactId: number, status: string) =>
  request<Contact>(`/api/contacts/${contactId}/relationship-status`, { method: 'POST', body: JSON.stringify({ status }) })

export const resetRelationshipStatus = (contactId: number) =>
  request<Contact>(`/api/contacts/${contactId}/relationship-status/auto`, { method: 'POST' })

export const refreshAllStatuses = () =>
  request<{ refreshed: number }>('/api/contacts/refresh-statuses', { method: 'POST' })

export const resolvePerson = (telegramId: number) =>
  request<PersonSummary>(`/api/contacts/resolve/${telegramId}`)

export const resolvePersonsBatch = (telegramIds: number[]) =>
  request<Record<number, PersonSummary>>('/api/contacts/resolve-batch', {
    method: 'POST',
    body: JSON.stringify({ telegram_ids: telegramIds }),
  })

// --- Profiles ---

export const getProfile = (userId: number | string) =>
  request<TelegramProfile>(`/api/profiles/${userId}`)

// --- Settings / Prefs ---

export const getPrefs = () =>
  request<{ notif_pref: string }>('/api/settings/prefs')

export const setNotifPref = (pref: 'all' | 'tracked' | 'off') =>
  request<{ success: boolean; pref: string }>('/api/settings/prefs/notifications', {
    method: 'POST',
    body: JSON.stringify({ pref }),
  })

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

export const getBriefingPrompt = () =>
  request<{ prompt: string; is_custom: boolean }>('/api/settings/prefs/briefing-prompt')

export const setBriefingPrompt = (prompt: string) =>
  request<{ success: boolean; is_custom: boolean }>('/api/settings/prefs/briefing-prompt', {
    method: 'POST',
    body: JSON.stringify({ prompt }),
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

export const getBriefingHistory = () =>
  request<BriefingRun[]>('/api/intelligence/briefing/history')

export const deleteBriefingRun = (run_id: number) =>
  request<{ success: boolean }>(`/api/intelligence/briefing/history/${run_id}`, {
    method: 'DELETE',
  })

export const regenerateBriefingEntry = (run_id: number, entry_id: number) =>
  request<BriefingHistoryEntry>(`/api/intelligence/briefing/history/${run_id}/entries/${entry_id}/regenerate`, {
    method: 'POST',
  })

// --- Compose ---

export const sendMessage = (chat_id: number, text: string, reply_to_id?: number) =>
  request<{ success: boolean; message_id: number }>('/api/compose/send', {
    method: 'POST',
    body: JSON.stringify({ chat_id, text, reply_to_id }),
  })

// --- Conversations ---

export const listConversations = () =>
  request<ConversationSummary[]>('/api/conversations/')

export const createConversation = () =>
  request<ConversationDetail>('/api/conversations/', { method: 'POST' })

export const getConversation = (id: number) =>
  request<ConversationDetail>(`/api/conversations/${id}`)

export const updateConversationTitle = (id: number, title: string) =>
  request<ConversationDetail>(`/api/conversations/${id}`, {
    method: 'PUT',
    body: JSON.stringify({ title }),
  })

export const deleteConversation = (id: number) =>
  request<{ success: boolean }>(`/api/conversations/${id}`, { method: 'DELETE' })

export const appendConversationMessage = (id: number, role: string, content: string) =>
  request<ConversationMessageItem>(`/api/conversations/${id}/messages`, {
    method: 'POST',
    body: JSON.stringify({ role, content }),
  })

export const suggestConversationTitle = (id: number) =>
  request<{ title: string | null }>(`/api/conversations/${id}/suggest-title`, { method: 'POST' })

// --- Mentions ---

export const getMentions = (params?: { days?: number; groupName?: string; tagName?: string }) => {
  const qs = new URLSearchParams()
  if (params?.days) qs.set('days', String(params.days))
  if (params?.groupName) qs.set('group_name', params.groupName)
  if (params?.tagName) qs.set('tag_name', params.tagName)
  const q = qs.toString()
  return request<MentionGroup[]>(`/api/intelligence/mentions${q ? `?${q}` : ''}`)
}

// --- Catch Up ---

export const getCatchUpItems = () =>
  request<CatchUpItem[]>('/api/catchup/items')

export const getAllCatchUpItems = () =>
  request<CatchUpItem[]>('/api/catchup/all')

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

export const addCatchUpNote = (chatTelegramId: number, note: string) =>
  request<CatchUpItem>(`/api/catchup/${chatTelegramId}/note`, {
    method: 'POST',
    body: JSON.stringify({ note }),
  })

export const updateCatchUpRelationship = (chatTelegramId: number, status: string) =>
  request<{ contact_id: number; relationship_status: string }>(`/api/catchup/${chatTelegramId}/relationship-status`, {
    method: 'POST',
    body: JSON.stringify({ status }),
  })

export const getCatchUpContext = (chatTelegramId: number) =>
  request<CatchUpContext>(`/api/catchup/${chatTelegramId}/context`)

// --- Dashboard ---

export const getDashboard = () =>
  request<DashboardData>('/api/dashboard')

// --- Global Search ---

export const globalSearch = (query: string, limit = 20) =>
  request<GlobalSearchResults>(`/api/search?q=${encodeURIComponent(query)}&limit=${limit}`)
