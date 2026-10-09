/**
 * Email (Gmail) client functions: sending with subject / recipients, the
 * per-sender "load remote images" preference and per-channel notification
 * preferences.
 */
import { useSyncExternalStore } from 'react'
import { request, upload } from '../lib/http'
import { setCachedChannelNotifPref } from '../lib/notifications'
import type { Conversation, Message, SendFilesResponse } from './types'

export interface EmailFields {
  /** Empty = "Re: <thread subject>" */
  subject?: string
  /** Recipients added to the reply's default (the sender, or your original recipients) */
  to?: string[]
  cc?: string[]
}

export interface EmailSendOptions extends EmailFields {
  clientNonce?: string
}

/** Comma/semicolon/whitespace separated addresses → lowercased, de-duplicated list */
export function parseAddressList(value: string): string[] {
  const out: string[] = []
  for (const raw of value.split(/[,;\s]+/)) {
    const addr = raw.trim().replace(/^<|>$/g, '').toLowerCase()
    if (addr && !out.includes(addr)) out.push(addr)
  }
  return out
}

export function isValidAddress(addr: string): boolean {
  return /^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(addr)
}

export const sendEmail = (conversationId: number, text: string, replyToMessageId: number | undefined, opts: EmailSendOptions = {}) =>
  request<{ success: boolean; message_id: number; message?: Message }>('/api/compose/send', {
    method: 'POST',
    body: JSON.stringify({
      conversation_id: conversationId, text, reply_to_message_id: replyToMessageId,
      subject: opts.subject || undefined, to: opts.to ?? [], cc: opts.cc ?? [],
      client_nonce: opts.clientNonce,
    }),
  })

export function sendEmailFiles(
  conversationId: number,
  files: File[],
  text: string | undefined,
  replyToMessageId: number | undefined,
  onProgress: ((fraction: number) => void) | undefined,
  opts: EmailSendOptions & { signal?: AbortSignal } = {},
) {
  const formData = new FormData()
  formData.append('conversation_id', String(conversationId))
  if (text) formData.append('text', text)
  if (replyToMessageId != null) formData.append('reply_to_message_id', String(replyToMessageId))
  if (opts.clientNonce) formData.append('client_nonce', opts.clientNonce)
  if (opts.subject) formData.append('subject', opts.subject)
  for (const a of opts.to ?? []) formData.append('to', a)
  for (const a of opts.cc ?? []) formData.append('cc', a)
  for (const file of files) formData.append('files', file)
  return upload<SendFilesResponse>('/api/compose/send-files', formData, { onProgress, signal: opts.signal })
}

// ── Remote images (per sender) ──

let remoteSenders: ReadonlySet<string> = new Set()
let remoteLoaded = false
const listeners = new Set<() => void>()

function emit(next: ReadonlySet<string>) {
  remoteSenders = next
  listeners.forEach((l) => l())
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  if (!remoteLoaded) {
    remoteLoaded = true
    request<{ email_remote_images_senders?: string[] }>('/api/settings/prefs')
      .then((p) => emit(new Set(p.email_remote_images_senders ?? [])))
      .catch(() => { remoteLoaded = false })
  }
  return () => { listeners.delete(listener) }
}

/** Senders whose remote images load automatically (kept in prefs) */
export function useRemoteImageSenders(): ReadonlySet<string> {
  return useSyncExternalStore(subscribe, () => remoteSenders)
}

export async function setRemoteImagesAllowed(sender: string, allowed: boolean): Promise<void> {
  const res = await request<{ success: boolean; senders: string[] }>('/api/settings/prefs/email-remote-images', {
    method: 'POST',
    body: JSON.stringify({ sender, allowed }),
  })
  emit(new Set(res.senders))
}

// ── Notifications per channel ──

export type NotifPref = 'all' | 'tracked' | 'off'

/** Email notifications are off until set here; other channels follow the global setting */
export const setChannelNotifPref = async (channel: string, pref: NotifPref) => {
  const result = await request<{ success: boolean; pref: NotifPref; channel: string }>('/api/settings/prefs/notifications', {
    method: 'POST',
    body: JSON.stringify({ pref, channel }),
  })
  // Keep the global NotificationBridge in step with the saved value (as setNotifPref does)
  setCachedChannelNotifPref(channel, result?.pref ?? pref)
  return result
}

// ── Mail space (/api/connectors/gmail) ──

export type MailFolder = 'inbox' | 'sent' | 'archive' | 'all'

export interface ThreadSender {
  name: string | null
  person_id: number | null
  is_me: boolean
}

/** A Conversation of kind email_thread plus its thread-row summary */
export interface MailThread extends Conversation {
  thread: { senders: ThreadSender[]; message_count: number; has_attachments: boolean }
}

export interface MailLabel {
  label: string
  /** Visible threads carrying it (archived included) */
  count: number
}

export const MAIL_PAGE = 50

/** Threads in a folder (or with a label) of one account — every Gmail account when `accountId` is null */
export function listMailThreads(
  opts: { accountId: number | null; folder: MailFolder; label?: string | null; limit?: number; offset?: number },
  signal?: AbortSignal,
) {
  const q = new URLSearchParams({ folder: opts.folder, limit: String(opts.limit ?? MAIL_PAGE), offset: String(opts.offset ?? 0) })
  if (opts.accountId != null) q.set('account_id', String(opts.accountId))
  if (opts.label) q.set('label', opts.label)
  return request<MailThread[]>(`/api/connectors/gmail/threads?${q}`, { signal })
}

export const getMailLabels = (accountId: number | null, signal?: AbortSignal) =>
  request<MailLabel[]>(`/api/connectors/gmail/labels${accountId != null ? `?account_id=${accountId}` : ''}`, { signal })

/** Archive (out of the Inbox) or move back to the Inbox — at Gmail; resolves to the updated conversation */
export const archiveThread = (conversationId: number, archived: boolean) =>
  request<Conversation>(`/api/connectors/gmail/threads/${conversationId}/archive`, {
    method: 'POST',
    body: JSON.stringify({ archived }),
  })

export const markThreadRead = (conversationId: number, read: boolean) =>
  request<Conversation>(`/api/connectors/gmail/threads/${conversationId}/read`, {
    method: 'POST',
    body: JSON.stringify({ read }),
  })

export interface NewEmail {
  accountId: number
  to: string[]
  cc: string[]
  subject: string
  text: string
  files: File[]
  clientNonce?: string
}

/** Start a new thread */
export function composeEmail(draft: NewEmail, onProgress?: (fraction: number) => void) {
  const formData = new FormData()
  formData.append('account_id', String(draft.accountId))
  formData.append('text', draft.text)
  formData.append('subject', draft.subject)
  for (const a of draft.to) formData.append('to', a)
  for (const a of draft.cc) formData.append('cc', a)
  if (draft.clientNonce) formData.append('client_nonce', draft.clientNonce)
  for (const f of draft.files) formData.append('files', f)
  return upload<{ success: boolean; conversation_id: number; message_id: number | null; message: Message | null }>(
    '/api/connectors/gmail/compose', formData, { onProgress },
  )
}
