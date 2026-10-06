/**
 * User-initiated "Check for scam" requests, keyed by (chat, message).
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
const k = (chatId: number, tgId: number) => `${chatId}:${tgId}`

function set(key: string, v: ScamCheck | null) {
  if (v) checks.set(key, v)
  else checks.delete(key)
  listeners.get(key)?.forEach((l) => l())
}

export function getScamCheck(chatId: number, tgId: number): ScamCheck | null {
  return checks.get(k(chatId, tgId)) ?? null
}

export async function startScamCheck(chatId: number, tgId: number): Promise<void> {
  const key = k(chatId, tgId)
  if (checks.get(key)?.status === 'checking') return
  set(key, { status: 'checking' })
  try {
    const res = await checkMessagesForScam(chatId, [tgId])
    if (res.missing_ids?.includes(tgId)) set(key, { status: 'error', error: 'Message not found' })
    // queued or already running: the terminal WS event settles it
  } catch (e) {
    set(key, { status: 'error', error: errorMessage(e) })
  }
}

export function dismissScamCheck(chatId: number, tgId: number): void {
  set(k(chatId, tgId), null)
}

/** Settle pending checks from a terminal scam WS event. Exported for tests. */
export function applyScamEvent(e: WSEvent): void {
  if (e.type !== 'scam_detected' && e.type !== 'scam_check_safe' && e.type !== 'scam_check_error') return
  const states = new Map<number, ScamMsgState>((e.messages ?? []).map((m) => [m.tg_message_id, m]))
  for (const tgId of e.message_ids) {
    const key = k(e.chat_id, tgId)
    if (checks.get(key)?.status !== 'checking') continue
    if (e.type === 'scam_check_error') {
      set(key, { status: 'error', error: e.error || 'Scam check failed' })
      continue
    }
    const st = states.get(tgId)
    const isScam = st ? st.effective_is_scam : e.type === 'scam_detected'
    set(key, { status: 'done', result: isScam ? 'scam' : 'safe' })
  }
}

/** Mount once where chat messages are shown: routes terminal events to pending checks. */
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

export function useScamCheck(chatId: number, tgId: number): ScamCheck | null {
  const key = k(chatId, tgId)
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
