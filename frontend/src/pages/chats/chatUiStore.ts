/**
 * Per-conversation transient UI state, keyed by conversation id.
 *
 * Reply target, edit buffer, compose draft, upload progress, manual-sync
 * spinner and selected forum topic all live in the slot of the chat they
 * belong to. An action that finishes after the user switched chats writes
 * into *its* chat's slot, so nothing can leak into (or act on) another chat.
 * Slots survive chat switches (drafts are restored when you come back).
 */
import { useCallback, useSyncExternalStore } from 'react'
import type { Message } from '../../api/types'

export interface AttachedFile {
  file: File
  preview?: string // object URL for images
}

export interface ComposeDraft {
  text: string
  attachments: AttachedFile[]
}

export interface ChatUi {
  draft: ComposeDraft
  /** Own message being edited (ComposeBar keeps a separate edit buffer; the draft is untouched) */
  editing: Message | null
  replyTo: Message | null
  /** 0..1 while a file upload for this chat is running */
  uploadProgress: number | null
  /** A send/edit for this chat is in flight */
  sending: boolean
  /** Last send failure; `nonce` is reused when the same content is retried */
  failedSend: { nonce: string; key: string; error: string } | null
  /** Manual "Sync" in flight */
  syncing: boolean
  topic: { id: number; title: string } | null
}

export const EMPTY_CHAT_UI: ChatUi = Object.freeze({
  draft: { text: '', attachments: [] },
  editing: null,
  replyTo: null,
  uploadProgress: null,
  sending: false,
  failedSend: null,
  syncing: false,
  topic: null,
}) as ChatUi

/** Keyed by conversation id */
const slots = new Map<number, ChatUi>()
const listeners = new Map<number, Set<() => void>>()

export function getChatUi(chatId: number): ChatUi {
  return slots.get(chatId) ?? EMPTY_CHAT_UI
}

export function updateChatUi(chatId: number, patch: Partial<ChatUi> | ((prev: ChatUi) => Partial<ChatUi>)): void {
  const prev = getChatUi(chatId)
  const p = typeof patch === 'function' ? patch(prev) : patch
  const next = { ...prev, ...p }
  slots.set(chatId, next)
  listeners.get(chatId)?.forEach((l) => l())
}

function subscribe(chatId: number, l: () => void): () => void {
  let set = listeners.get(chatId)
  if (!set) { set = new Set(); listeners.set(chatId, set) }
  set.add(l)
  return () => { set!.delete(l) }
}

/** Subscribe to one chat's slot (re-renders only when that slot changes). */
export function useChatUi(chatId: number | null): ChatUi {
  const sub = useCallback((l: () => void) => (chatId == null ? () => {} : subscribe(chatId, l)), [chatId])
  const get = useCallback(() => (chatId == null ? EMPTY_CHAT_UI : getChatUi(chatId)), [chatId])
  return useSyncExternalStore(sub, get, get)
}

/** Subscribe to one field of a chat's slot; re-renders only when that value changes. */
export function useChatUiSelect<T>(chatId: number | null, select: (ui: ChatUi) => T): T {
  const sub = useCallback((l: () => void) => (chatId == null ? () => {} : subscribe(chatId, l)), [chatId])
  const get = () => select(chatId == null ? EMPTY_CHAT_UI : getChatUi(chatId))
  return useSyncExternalStore(sub, get, get)
}

/** Test-only */
export function __resetChatUiStore(): void {
  slots.clear()
  listeners.clear()
}

/** A stable fingerprint of what a send would post, for nonce reuse on retry. */
export function sendKey(text: string, files: File[] = [], replyToId?: number | null, topicId?: number | null): string {
  return JSON.stringify([text, files.map((f) => [f.name, f.size, f.lastModified]), replyToId ?? null, topicId ?? null])
}

export function newNonce(): string {
  const c = globalThis.crypto as Crypto | undefined
  if (c?.randomUUID) return c.randomUUID()
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}
