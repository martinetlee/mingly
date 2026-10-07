import { useCallback, useEffect, useSyncExternalStore } from 'react'
import type { PersonSummary } from '../api/types'
import { resolvePeople } from '../api/client'

/**
 * Keyed store of PersonSummary (what PersonCard shows for a telegram_id).
 *
 * - Lookups made in the same tick are batched into one /resolve-batch call.
 * - Every mutation of a contact or position calls invalidatePerson(id) (or
 *   invalidateAllPeople() for org renames/merges/deletes), which drops the
 *   entry and makes mounted cards re-resolve.
 * - A response that was requested before an invalidation is discarded: the
 *   entry's version moved on, so the old summary can't overwrite the new one.
 */

interface Entry {
  data?: PersonSummary
  failed?: boolean
  inflight: boolean
  version: number
}

const entries = new Map<number, Entry>()
const listeners = new Map<number, Set<() => void>>()
let queue = new Set<number>()
let flushTimer: ReturnType<typeof setTimeout> | null = null
const BATCH_MAX = 200

function entryFor(id: number): Entry {
  let e = entries.get(id)
  if (!e) {
    e = { inflight: false, version: 0 }
    entries.set(id, e)
  }
  return e
}

function notify(id: number) {
  listeners.get(id)?.forEach((l) => l())
}

function fallback(id: number): PersonSummary {
  return {
    telegram_id: id, name: String(id), username: null, is_important: false, contact_id: null,
    relationship_status: null, last_interaction_at: null, primary_position: null,
    phone: null, twitter_handle: null, linkedin_url: null, has_notes: false,
  }
}

function flush() {
  flushTimer = null
  const ids = Array.from(queue)
  queue = new Set()
  for (let i = 0; i < ids.length; i += BATCH_MAX) {
    const chunk = ids.slice(i, i + BATCH_MAX)
    const versions = new Map(chunk.map((id) => [id, entryFor(id).version]))
    resolvePeople(chunk).then(
      (result) => {
        for (const id of chunk) {
          const e = entryFor(id)
          if (e.version !== versions.get(id)) continue // invalidated meanwhile
          e.inflight = false
          const summary = result[String(id)]
          if (summary) { e.data = summary; e.failed = false } else { e.failed = true }
          notify(id)
        }
      },
      () => {
        for (const id of chunk) {
          const e = entryFor(id)
          if (e.version !== versions.get(id)) continue
          e.inflight = false
          e.failed = true
          notify(id)
        }
      },
    )
  }
}

function request(id: number) {
  const e = entryFor(id)
  if (e.data || e.inflight || e.failed) return
  e.inflight = true
  queue.add(id)
  if (!flushTimer) flushTimer = setTimeout(flush, 0)
}

function invalidateIds(ids: Iterable<number>) {
  for (const id of ids) {
    const e = entries.get(id)
    if (!e) continue
    e.version++
    e.data = undefined
    e.failed = false
    e.inflight = false
    queue.delete(id)
    // Mounted cards re-request on notify; unmounted entries stay empty
    if (listeners.get(id)?.size) request(id)
    notify(id)
  }
}

/** Drop one person's cached summary (after a contact/position mutation). */
export function invalidatePerson(telegramId: number) {
  invalidateIds([telegramId])
}

/** Drop every cached summary (org rename/merge/delete, contacts sync). */
export function invalidateAllPeople() {
  invalidateIds(Array.from(entries.keys()))
}

/** Seed the store with summaries the caller already has. */
export function primePeople(data: Record<number, PersonSummary>) {
  for (const [tid, summary] of Object.entries(data)) {
    const e = entryFor(Number(tid))
    e.data = summary
    e.failed = false
    notify(Number(tid))
  }
}

/** Test helper */
export function resetPersonStore() {
  entries.clear()
  queue = new Set()
  if (flushTimer) clearTimeout(flushTimer)
  flushTimer = null
}

/**
 * The PersonSummary for `telegramId`, resolved on demand. `enabled=false`
 * (every displayed field was supplied by the caller) skips the request.
 * Returns `undefined` while loading and a name-only fallback if it fails.
 */
export function usePersonSummary(telegramId: number, enabled = true): PersonSummary | undefined {
  const subscribe = useCallback((cb: () => void) => {
    let set = listeners.get(telegramId)
    if (!set) { set = new Set(); listeners.set(telegramId, set) }
    set.add(cb)
    return () => { set!.delete(cb) }
  }, [telegramId])
  const getSnapshot = useCallback(() => {
    const e = entries.get(telegramId)
    if (!e) return undefined
    if (e.data) return e.data
    return e.failed ? failedSnapshot(telegramId) : undefined
  }, [telegramId])
  const person = useSyncExternalStore(subscribe, getSnapshot)

  useEffect(() => {
    if (enabled) request(telegramId)
  }, [telegramId, enabled, person])

  return person
}

// Stable fallback objects so useSyncExternalStore doesn't loop
const failedSnapshots = new Map<number, PersonSummary>()
function failedSnapshot(id: number): PersonSummary {
  let s = failedSnapshots.get(id)
  if (!s) { s = fallback(id); failedSnapshots.set(id, s) }
  return s
}
