import type { AuthStatus, Chat, Folder, Message } from './types'

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

export const getChats = () =>
  request<Chat[]>('/api/chats')

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

// --- Compose ---

export const sendMessage = (chat_id: number, text: string, reply_to_id?: number) =>
  request<{ success: boolean; message_id: number }>('/api/compose/send', {
    method: 'POST',
    body: JSON.stringify({ chat_id, text, reply_to_id }),
  })
