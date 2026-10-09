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
import type { ScamMsgState, WSEvent } from '../../api/types'
import { checkMessagesForScam } from '../../api/client'
import { errorMessage } from '../../lib/toast'
import { useWebSocketClient } from '../../context/WebSocketContext'

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

/** Mount once where conversation messages are shown: routes terminal events to pending checks. */
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

/** Test-only */
export function __resetScamChecks(): void {
  checks.clear()
  listeners.clear()
}
