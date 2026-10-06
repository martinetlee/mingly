import { describe, it, expect } from 'vitest'
import type { Message } from '../../../api/types'
import { msg, page } from './fixtures'
import {
  applyLive, appendNewer, cursorOf, deletionApplies, firstUnreadId, fromPage, inScope, mergeLatest, mergeMessages, prependOlder,
} from '../messageWindow'

const ids = (ms: Message[]) => ms.map((m) => m.tg_message_id)

describe('messageWindow', () => {
  it('cursor is "<date>~<tg_message_id>"', () => {
    expect(cursorOf(msg(5))).toBe('2026-10-06T10:00:05Z~5')
  })

  it('merges by tg_message_id without duplicates and keeps order (WS + overlapping page)', () => {
    const w = fromPage(page([10, 11, 12], { has_older: true }))
    // A WS message arrived (13), then an older page that overlaps (shifted offsets used to re-send 10..12)
    const live = applyLive(w, msg(13))
    const merged = prependOlder(live, page([8, 9, 10, 11], { has_older: true }))
    expect(ids(merged.messages)).toEqual([8, 9, 10, 11, 12, 13])
    expect(new Set(ids(merged.messages)).size).toBe(merged.messages.length)
  })

  it('orders same-timestamp album items by tg_message_id', () => {
    const d = '2026-10-06T10:00:00Z'
    const out = mergeMessages([msg(3, { date: d })], [msg(1, { date: d }), msg(2, { date: d })])
    expect(ids(out)).toEqual([1, 2, 3])
  })

  it('appends a live message only when the window is at latest', () => {
    const atLatest = fromPage(page([1, 2]))
    expect(ids(applyLive(atLatest, msg(3)).messages)).toEqual([1, 2, 3])
    const jumped = fromPage(page([1, 2], { has_newer: true }))
    expect(applyLive(jumped, msg(99))).toBe(jumped)
  })

  it('replaces an existing copy in place (edit/echo) even when not at latest', () => {
    const w = fromPage(page([1, 2], { has_newer: true }))
    const out = applyLive(w, msg(2, { text: 'edited' }))
    expect(out.messages[1].text).toBe('edited')
    expect(out.messages).toHaveLength(2)
  })

  it('trims the far end past the window cap and flags it', () => {
    const w = fromPage(page([5, 6, 7], { has_older: true }))
    const older = prependOlder(w, page([2, 3, 4], { has_older: true }), 4)
    expect(ids(older.messages)).toEqual([2, 3, 4, 5])
    expect(older.hasNewer).toBe(true)
    const newer = appendNewer(fromPage(page([1, 2, 3])), page([4, 5], { has_newer: true }), 4)
    expect(ids(newer.messages)).toEqual([2, 3, 4, 5])
    expect(newer.hasOlder).toBe(true)
    expect(newer.hasNewer).toBe(true)
  })

  it('mergeLatest unions a contiguous page, keeps the window on a gap', () => {
    const w = fromPage(page([1, 2, 3]))
    expect(ids(mergeLatest(w, page([3, 4], { has_older: true })).messages)).toEqual([1, 2, 3, 4])
    const gap = mergeLatest(w, page([50, 51], { has_older: true }))
    expect(ids(gap.messages)).toEqual([1, 2, 3])
    expect(gap.hasNewer).toBe(true)
  })

  it('scopes messages by chat and forum topic (General includes untagged)', () => {
    expect(inScope({ chat_id: -100, topic_id: 7 }, { chatId: -100, topicId: null })).toBe(true)
    expect(inScope({ chat_id: -200, topic_id: null }, { chatId: -100, topicId: null })).toBe(false)
    expect(inScope({ chat_id: -100, topic_id: 7 }, { chatId: -100, topicId: 8 })).toBe(false)
    expect(inScope({ chat_id: -100, topic_id: null }, { chatId: -100, topicId: 1 })).toBe(true)
    expect(inScope({ chat_id: -100, topic_id: 7 }, { chatId: -100, topicId: 1 })).toBe(false)
  })

  it('applies chat-less deletions only in the shared id namespace', () => {
    expect(deletionApplies(null, 12345)).toBe(true)
    expect(deletionApplies(null, -1001234567890)).toBe(false)
    expect(deletionApplies(-1001234567890, -1001234567890)).toBe(true)
  })

  it('anchors the unread separator on the first unread incoming message', () => {
    const ms = [msg(1), msg(2), msg(3, { is_outgoing: true }), msg(4), msg(5)]
    expect(firstUnreadId(ms, 2)).toBe(4)
    expect(firstUnreadId(ms, 3)).toBe(2)
    expect(firstUnreadId(ms, 0)).toBeNull()
    expect(firstUnreadId(ms, 99)).toBe(1)
  })
})
