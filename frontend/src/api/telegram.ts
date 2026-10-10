/** Telegram-only operations (409 `unsupported_channel` for other channels). */

import { request } from '../lib/http'
import type { Conversation, Folder, TelegramDirectoryResult, TelegramProfile } from './types'

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
