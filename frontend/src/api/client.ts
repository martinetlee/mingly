import type { AuthStatus, Chat, Message } from './types'

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
  request<AuthStatus>('/auth/status')

export const sendCode = (phone: string) =>
  request<{ phone_code_hash: string }>('/auth/send-code', {
    method: 'POST',
    body: JSON.stringify({ phone }),
  })

export const verifyCode = (phone: string, code: string, phone_code_hash: string) =>
  request<{ success: boolean; requires_2fa: boolean; first_name?: string }>('/auth/verify-code', {
    method: 'POST',
    body: JSON.stringify({ phone, code, phone_code_hash }),
  })

export const verify2FA = (password: string) =>
  request<{ success: boolean; first_name?: string }>('/auth/verify-2fa', {
    method: 'POST',
    body: JSON.stringify({ password }),
  })

export const logout = () =>
  request<{ success: boolean }>('/auth/logout', { method: 'POST' })

// --- Chats ---

export const getChats = () =>
  request<Chat[]>('/chats')

export const setMonitored = (telegram_id: number, monitored: boolean) =>
  request<{ telegram_id: number; is_monitored: boolean }>(`/chats/${telegram_id}/monitor`, {
    method: 'POST',
    body: JSON.stringify({ monitored }),
  })

export const getMessages = (telegram_id: number, limit = 50, offset = 0) =>
  request<Message[]>(`/chats/${telegram_id}/messages?limit=${limit}&offset=${offset}`)

export const syncChat = (telegram_id: number) =>
  request<{ status: string }>(`/chats/${telegram_id}/sync`, { method: 'POST' })

// --- Compose ---

export const sendMessage = (chat_id: number, text: string, reply_to_id?: number) =>
  request<{ success: boolean; message_id: number }>('/compose/send', {
    method: 'POST',
    body: JSON.stringify({ chat_id, text, reply_to_id }),
  })
