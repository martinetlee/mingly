import type { AuthStatus, Chat, ChatMember, Folder, Message, Tag, TagSuggestion, TelegramProfile } from './types'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || res.statusText)
  }
  return res.json()
}

// --- Vault ---

export const getVaultStatus = () =>
  request<{ status: 'unset' | 'locked' | 'unlocked' }>('/api/auth/vault-status')

export const initVault = (password: string) =>
  request<{ success: boolean }>('/api/auth/vault/init', {
    method: 'POST',
    body: JSON.stringify({ password }),
  })

export const unlockVault = (password: string) =>
  request<{ success: boolean }>('/api/auth/vault/unlock', {
    method: 'POST',
    body: JSON.stringify({ password }),
  })

export const resetVault = () =>
  request<{ success: boolean }>('/api/auth/vault/reset', { method: 'POST' })

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

export const getChats = (limit = 50, offset = 0) =>
  request<Chat[]>(`/api/chats?limit=${limit}&offset=${offset}`)

export const bulkSync = () =>
  request<{ synced: number }>('/api/chats/bulk-sync', { method: 'POST' })

export const getFolders = () =>
  request<Folder[]>('/api/chats/folders')

export const setMonitored = (telegram_id: number, monitored: boolean) =>
  request<{ telegram_id: number; is_monitored: boolean }>(`/api/chats/${telegram_id}/monitor`, {
    method: 'POST',
    body: JSON.stringify({ monitored }),
  })

export const getMessages = (telegram_id: number, limit = 50, offset = 0) =>
  request<Message[]>(`/api/chats/${telegram_id}/messages?limit=${limit}&offset=${offset}`)

export const syncChat = (telegram_id: number) =>
  request<{ status: string }>(`/api/chats/${telegram_id}/sync`, { method: 'POST' })

export const getChatMembers = (telegram_id: number, limit = 100) =>
  request<ChatMember[]>(`/api/chats/${telegram_id}/members?limit=${limit}`)

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

// --- Profiles ---

export const getProfile = (userId: number) =>
  request<TelegramProfile>(`/api/profiles/${userId}`)

// --- Settings / Prefs ---

export const getPrefs = () =>
  request<{ notif_pref: string }>('/api/settings/prefs')

export const setNotifPref = (pref: 'all' | 'tracked' | 'off') =>
  request<{ success: boolean; pref: string }>('/api/settings/prefs/notifications', {
    method: 'POST',
    body: JSON.stringify({ pref }),
  })

// --- Compose ---

export const sendMessage = (chat_id: number, text: string, reply_to_id?: number) =>
  request<{ success: boolean; message_id: number }>('/api/compose/send', {
    method: 'POST',
    body: JSON.stringify({ chat_id, text, reply_to_id }),
  })
