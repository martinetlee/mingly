import { useCallback, useEffect, useSyncExternalStore } from 'react'
import type { Person } from '../api/types'
import { getPerson } from '../api/client'

/** What PersonCard shows for a person (derived from GET /api/people/{person_id}) */
export interface PersonSummary {
  person_id: number
  name: string
  username: string | null
  is_important: boolean
  relationship_status: string | null
  last_interaction_at: string | null
  primary_position: { title: string | null; organization: string; organization_id?: number | null } | null
  phone: string | null
  twitter_handle: string | null
  linkedin_url: string | null
  avatar_url: string | null
  has_notes: boolean
}

/** Summary fields of a Person payload */
export function summarizePerson(p: Person): PersonSummary {
  const pos = (p.positions ?? []).find((x) => x.is_active !== false) ?? null
  return {
    person_id: p.id,
    name: p.name,
    username: p.identities.find((i) => i.username)?.username ?? null,
    is_important: p.is_important,
    relationship_status: p.relationship_status,
    last_interaction_at: p.last_interaction_at,
    primary_position: pos ? { title: pos.title, organization: pos.organization, organization_id: pos.organization_id } : null,
    phone: p.identities.find((i) => i.phone)?.phone ?? null,
    twitter_handle: p.twitter_handle,
    linkedin_url: p.linkedin_url,
    avatar_url: p.avatar_url ?? p.identities.find((i) => i.avatar_url)?.avatar_url ?? null,
    has_notes: !!p.notes,
  }
}

/**
 * Keyed store of PersonSummary (what PersonCard shows for a person id).
 *
 * - Lookups made in the same tick are queued together and fetched through
 *   GET /api/people/{person_id} with at most MAX_CONCURRENT requests in flight.
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
const MAX_CONCURRENT = 6
let running = 0

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
    person_id: id, name: `Person ${id}`, username: null, is_important: false,
    relationship_status: null, last_interaction_at: null, primary_position: null,
    phone: null, twitter_handle: null, linkedin_url: null, avatar_url: null, has_notes: false,
  }
}

function flush() {
  flushTimer = null
  pump()
}

function pump() {
  while (running < MAX_CONCURRENT && queue.size > 0) {
    const id: number = queue.values().next().value as number
    queue.delete(id)
    const version = entryFor(id).version
    running++
    getPerson(id).then(
      (person) => {
        const e = entryFor(id)
        if (e.version !== version) return // invalidated meanwhile
        e.inflight = false
        e.data = summarizePerson(person)
        e.failed = false
        notify(id)
      },
      () => {
        const e = entryFor(id)
        if (e.version !== version) return
        e.inflight = false
        e.failed = true
        notify(id)
      },
    ).finally(() => {
      running--
      pump()
    })
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
export function invalidatePerson(personId: number) {
  invalidateIds([personId])
}

/** Drop every cached summary (org rename/merge/delete, contacts sync). */
export function invalidateAllPeople() {
  invalidateIds(Array.from(entries.keys()))
}

/** Seed the store with summaries the caller already has. */
export function primePeople(data: Record<number, PersonSummary>) {
  for (const [pid, summary] of Object.entries(data)) {
    const e = entryFor(Number(pid))
    e.data = summary
    e.failed = false
    notify(Number(pid))
  }
}

/** Test helper */
export function resetPersonStore() {
  entries.clear()
  queue = new Set()
  running = 0
  if (flushTimer) clearTimeout(flushTimer)
  flushTimer = null
}

/**
 * The PersonSummary for `personId`, resolved on demand. `enabled=false`
 * (every displayed field was supplied by the caller) skips the request.
 * Returns `undefined` while loading and a name-only fallback if it fails.
 */
export function usePersonSummary(personId: number, enabled = true): PersonSummary | undefined {
  const subscribe = useCallback((cb: () => void) => {
    let set = listeners.get(personId)
    if (!set) { set = new Set(); listeners.set(personId, set) }
    set.add(cb)
    return () => { set!.delete(cb) }
  }, [personId])
  const getSnapshot = useCallback(() => {
    const e = entries.get(personId)
    if (!e) return undefined
    if (e.data) return e.data
    return e.failed ? failedSnapshot(personId) : undefined
  }, [personId])
  const person = useSyncExternalStore(subscribe, getSnapshot)

  useEffect(() => {
    if (enabled) request(personId)
  }, [personId, enabled, person])

  return person
}

// Stable fallback objects so useSyncExternalStore doesn't loop
const failedSnapshots = new Map<number, PersonSummary>()
function failedSnapshot(id: number): PersonSummary {
  let s = failedSnapshots.get(id)
  if (!s) { s = fallback(id); failedSnapshots.set(id, s) }
  return s
}
