import { useCallback, useEffect, useSyncExternalStore } from 'react'
import { getScamFlags } from '../api/client'
import { useFeatures } from '../context/FeaturesContext'
import type { ScamFlagChange } from '../api/types'
import type { WebSocketClient } from '../lib/ws'
import { useWebSocketClient } from '../context/WebSocketContext'
import { reportError } from '../lib/toast'

/**
 * App-wide set of actively flagged scam senders (identity ids), shared by
 * Shield, Chats and MessageBubble.
 *
 * - Seeded from GET /api/scam/flags `flagged_identity_ids` when the first
 *   consumer mounts, and refetched on WS `resync`.
 * - Kept current by WS: `scam_users_flagged` adds; `scam_verdict_changed.flags`
 *   adds (active) or removes (dismissed / reset).
 * - Mutations apply their response `flags` immediately via applyFlagChanges /
 *   markDismissed / markFlagged, so the UI doesn't wait for the WS echo.
 *
 * Changes that land while a refresh is in flight are replayed on top of the
 * refresh result, so a slow GET can't resurrect a just-dismissed sender.
 */

interface Snapshot {
  ids: Set<number>
  loading: boolean
}

let snapshot: Snapshot = { ids: new Set(), loading: false }
const listeners = new Set<() => void>()
/** id → final state, recorded for changes made while a refresh is in flight */
let overlay: Map<number, boolean> | null = null
let refreshSeq = 0

let attachedClient: WebSocketClient | null = null
let attachCount = 0
let detachWs: (() => void) | null = null

function emit(next: Snapshot) {
  snapshot = next
  listeners.forEach((l) => l())
}

function setIds(changes: Iterable<[number, boolean]>) {
  const ids = new Set(snapshot.ids)
  let changed = false
  for (const [id, flagged] of changes) {
    overlay?.set(id, flagged)
    if (flagged && !ids.has(id)) { ids.add(id); changed = true }
    if (!flagged && ids.has(id)) { ids.delete(id); changed = true }
  }
  if (changed) emit({ ...snapshot, ids })
}

/** Apply `flags` from a verdict response or `scam_verdict_changed` event. */
export function applyFlagChanges(flags: ScamFlagChange[] | undefined | null) {
  if (!flags?.length) return
  setIds(flags.map((f) => [f.identity_id, f.status === 'active'] as [number, boolean]))
}

export function markFlaggedSender(id: number) { setIds([[id, true]]) }
export function markDismissedSender(id: number) { setIds([[id, false]]) }

export function refreshFlaggedSenders(): Promise<void> {
  const seq = ++refreshSeq
  overlay = new Map()
  emit({ ...snapshot, loading: true })
  return getScamFlags()
    .then((r) => {
      if (seq !== refreshSeq) return
      const ids = new Set(r.flagged_identity_ids ?? [])
      for (const [id, flagged] of overlay ?? []) {
        if (flagged) ids.add(id)
        else ids.delete(id)
      }
      overlay = null
      emit({ ids, loading: false })
    })
    .catch((e) => {
      if (seq !== refreshSeq) return
      overlay = null
      emit({ ...snapshot, loading: false })
      reportError(e, "Couldn't load flagged senders")
    })
}

function attach(client: WebSocketClient | null): () => void {
  attachCount++
  if (attachCount === 1) {
    // First consumer: the set may be stale from before (or never loaded)
    void refreshFlaggedSenders()
  }
  if (client && client !== attachedClient) {
    detachWs?.()
    attachedClient = client
    const offs = [
      client.subscribe('scam_users_flagged', (e) => setIds(e.identity_ids.map((id) => [id, true] as [number, boolean]))),
      client.subscribe('scam_verdict_changed', (e) => applyFlagChanges(e.flags)),
      client.subscribe('resync', () => { void refreshFlaggedSenders() }),
    ]
    detachWs = () => offs.forEach((off) => off())
  }
  return () => {
    attachCount--
    if (attachCount === 0) {
      detachWs?.()
      detachWs = null
      attachedClient = null
    }
  }
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

const getSnapshot = () => snapshot

const EMPTY_IDS: Set<number> = new Set()

export function useFlaggedSenders(): {
  ids: Set<number>
  loading: boolean
  refresh(): void
  markDismissed(identityId: number): void
  markFlagged(identityId: number): void
} {
  const client = useWebSocketClient()
  const { shield } = useFeatures()
  const snap = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  // While Shield is off nothing is fetched or subscribed, and no one is flagged
  useEffect(() => (shield ? attach(client) : undefined), [client, shield])
  const refresh = useCallback(() => { if (shield) void refreshFlaggedSenders() }, [shield])
  return {
    ids: shield ? snap.ids : EMPTY_IDS,
    loading: shield && snap.loading,
    refresh,
    markDismissed: markDismissedSender,
    markFlagged: markFlaggedSender,
  }
}

/** Test-only: reset module state between tests. */
export function __resetFlaggedSendersForTests() {
  detachWs?.()
  detachWs = null
  attachedClient = null
  attachCount = 0
  overlay = null
  refreshSeq++
  snapshot = { ids: new Set(), loading: false }
  listeners.clear()
}
