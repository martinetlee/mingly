/**
 * User-initiated "Check for scam" requests, keyed by (conversation, message).
 *
 * POST /api/scam/check-messages only queues the check; the result arrives as
 * exactly one terminal WS event per message (scam_detected / scam_check_safe /
 * scam_check_error). A pending check ends on the first terminal event whose
 * `message_ids` include the message — never on an old `scam_score` that was
 * already on the message. The verdict shown comes from the event's stored
 * row (`messages[i].effective_is_scam`).
 */
import { useCallback, useEffect, useSyncExternalStore } from 'react'
import type { Message, ScamMsgState, WSEvent } from '../../api/types'
import { checkMessagesForScam, setMessageVerdict, setSenderVerdict } from '../../api/client'
import { errorMessage, reportError, toast } from '../../lib/toast'
import { useWebSocketClient } from '../../context/WebSocketContext'
import { useWsEvent } from '../../hooks/useWebSocket'
import { applyFlagChanges } from '../../hooks/useFlaggedSenders'

export type ScamCheck =
  | { status: 'checking' }
  | { status: 'done'; result: 'scam' | 'safe' }
  | { status: 'error'; error: string }

const checks = new Map<string, ScamCheck>()
const listeners = new Map<string, Set<() => void>>()
const k = (conversationId: number, messageId: number) => `${conversationId}:${messageId}`

function set(key: string, v: ScamCheck | null) {
  if (v) checks.set(key, v)
  else checks.delete(key)
  listeners.get(key)?.forEach((l) => l())
}

export function getScamCheck(conversationId: number, messageId: number): ScamCheck | null {
  return checks.get(k(conversationId, messageId)) ?? null
}

export async function startScamCheck(conversationId: number, messageId: number): Promise<void> {
  const key = k(conversationId, messageId)
  if (checks.get(key)?.status === 'checking') return
  set(key, { status: 'checking' })
  try {
    const res = await checkMessagesForScam(conversationId, [messageId])
    if (res.missing_ids?.includes(messageId)) set(key, { status: 'error', error: 'Message not found' })
    // queued or already running: the terminal WS event settles it
  } catch (e) {
    set(key, { status: 'error', error: errorMessage(e) })
  }
}

export function dismissScamCheck(conversationId: number, messageId: number): void {
  set(k(conversationId, messageId), null)
}

/** Settle pending checks from a terminal scam WS event. Exported for tests. */
export function applyScamEvent(e: WSEvent): void {
  if (e.type !== 'scam_detected' && e.type !== 'scam_check_safe' && e.type !== 'scam_check_error') return
  const states = new Map<number, ScamMsgState>((e.messages ?? []).map((m) => [m.message_id, m]))
  for (const messageId of e.message_ids) {
    const key = k(e.conversation_id, messageId)
    if (checks.get(key)?.status !== 'checking') continue
    if (e.type === 'scam_check_error') {
      set(key, { status: 'error', error: e.error || 'Scam check failed' })
      continue
    }
    const st = states.get(messageId)
    const isScam = st ? st.effective_is_scam : e.type === 'scam_detected'
    set(key, { status: 'done', result: isScam ? 'scam' : 'safe' })
  }
}

/** Mounted once app-wide (Layout): routes terminal events to pending checks wherever they were started. */
export function useScamCheckEvents(): void {
  const client = useWebSocketClient()
  useEffect(() => {
    if (!client) return
    const offs = [
      client.subscribe('scam_detected', applyScamEvent),
      client.subscribe('scam_check_safe', applyScamEvent),
      client.subscribe('scam_check_error', applyScamEvent),
    ]
    return () => offs.forEach((off) => off())
  }, [client])
}

export function useScamCheck(conversationId: number, messageId: number): ScamCheck | null {
  const key = k(conversationId, messageId)
  const sub = useCallback((l: () => void) => {
    let s = listeners.get(key)
    if (!s) { s = new Set(); listeners.set(key, s) }
    s.add(l)
    return () => { s!.delete(l) }
  }, [key])
  const get = useCallback(() => checks.get(key) ?? null, [key])
  return useSyncExternalStore(sub, get, get)
}

/**
 * The stored scam state of messages after a scam event / verdict: `entries`
 * for `conversationId` merged into the matching messages. Returns `messages`
 * itself when nothing matched (safe as a setState updater result).
 */
export function applyScamStatesTo<M extends Pick<Message, 'id'>>(
  messages: M[], conversationId: number, entries: ScamMsgState[] | undefined,
): M[] {
  if (!entries?.length) return messages
  const byId = new Map(entries.filter((e) => e.conversation_id === conversationId).map((e) => [e.message_id, e]))
  if (byId.size === 0) return messages
  let changed = false
  const next = messages.map((m) => {
    const e = byId.get(m.id)
    if (!e) return m
    changed = true
    return { ...m, ...e }
  })
  return changed ? next : messages
}

/**
 * Live scam state for one conversation's messages (for panes that hold
 * their own message list, e.g. Mail's thread view): every scam event and
 * verdict change for `conversationId` is handed to `onStates`, typically
 * `(entries) => setMessages((prev) => applyScamStatesTo(prev, conversationId, entries))`.
 */
export function useScamStateEvents(conversationId: number | null, onStates: (entries: ScamMsgState[]) => void): void {
  const enabled = conversationId != null
  const forThis = (cid: number, entries: ScamMsgState[] | undefined) => {
    if (conversationId != null && cid === conversationId && entries?.length) onStates(entries)
  }
  useWsEvent('scam_detected', (e) => forThis(e.conversation_id, e.messages), enabled)
  useWsEvent('scam_check_safe', (e) => forThis(e.conversation_id, e.messages), enabled)
  useWsEvent('scam_check_error', (e) => forThis(e.conversation_id, e.messages), enabled)
  useWsEvent('scam_verdict_changed', (e) => {
    if (conversationId == null) return
    const mine = (e.messages ?? []).filter((m) => m.conversation_id === conversationId)
    if (mine.length) onStates(mine)
  }, enabled)
}

export type ScamVerdictScope = 'message' | 'sender'

/**
 * "Not a scam" for one message, or "Dismiss sender" for everything they
 * sent. Applies the flag changes app-wide and toasts; resolves to the
 * updated message states (to merge with applyScamStatesTo), or null when
 * it failed (already reported).
 */
export async function submitNotScamVerdict(msg: Pick<Message, 'id' | 'conversation_id' | 'sender'>, scope: ScamVerdictScope): Promise<ScamMsgState[] | null> {
  try {
    const senderIdentity = msg.sender?.identity_id
    const res = scope === 'sender' && senderIdentity != null
      ? await setSenderVerdict(senderIdentity, 'not_scam')
      : await setMessageVerdict([{ message_id: msg.id, conversation_id: msg.conversation_id }], 'not_scam')
    applyFlagChanges(res.flags)
    toast.success(scope === 'sender' ? 'Sender dismissed' : 'Marked as not a scam')
    return res.messages ?? []
  } catch (e) {
    reportError(e, 'Couldn’t update scam verdict')
    return null
  }
}

/** Test-only */
export function __resetScamChecks(): void {
  checks.clear()
  listeners.clear()
}
