/**
 * Message pane state for one view scope {chatId, topicId} on the keyset
 * window API (GET /api/chats/{id}/messages/window).
 *
 * Every async result is applied only if it still belongs to the current
 * scope *and* window generation: a scope change or a jump bumps the
 * generation and aborts everything in flight, so a slow page for chat A can
 * never land in chat B, and an older-page load can't splice into a window
 * that a jump has since replaced.
 *
 * The window tracks {hasOlder, hasNewer}; WS messages are appended only while
 * it is at latest. When local history runs out, older history is backfilled
 * from Telegram (POST /backfill) until it reports `exhausted`.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { AnchorReason, Message, MessageWindow, ScamMsgState } from '../api/types'
import { backfillChat, getMessageWindow, isAbortError } from '../api/client'
import type { MessageWindowParams } from '../api/client'
import { useWsEvent } from './useWebSocket'
import { errorMessage } from '../lib/toast'
import {
  MAX_WINDOW, applyLive, appendNewer, cursorOf, deletionApplies, firstUnreadId, fromPage, inScope,
  mergeLatest, prependOlder,
} from '../pages/chats/messageWindow'
import type { WindowSlice } from '../pages/chats/messageWindow'

export const PAGE_SIZE = 50
/** How long a jumped-to message stays visually highlighted */
export const HIGHLIGHT_MS = 3000

export interface ChatMessagesScope {
  chatId: number | null
  topicId: number | null
  /** Open the scope centred on this message (search hit, mention, notification) */
  anchorTgId?: number | null
  /** Unread count when the chat was selected — places the unread separator */
  unreadCount?: number
  /** Bumped by every selectChat(): re-opening the same scope reloads it */
  nonce: number
}

export type ScrollIntent =
  | { id: number; kind: 'bottom' }
  | { id: number; kind: 'message'; tgId: number }

export type BackfillState = 'idle' | 'loading' | 'exhausted' | 'retry'

export type JumpTarget = { kind: 'message'; tgId: number } | { kind: 'date'; date: string }

/** A jump whose target couldn't be shown (shown inline; the window is left as it was) */
export interface JumpNotice {
  id: number
  target: JumpTarget
  reason: AnchorReason
  /** Error text for reason 'error' */
  detail?: string | null
}

export interface ChatMessagesState extends WindowSlice {
  /** Scope the state belongs to ("chatId|topicId"); null when nothing is open */
  scopeKey: string | null
  /** The initial window for this scope hasn't arrived yet */
  loading: boolean
  /** A jump/date window is replacing the current one (may be fetching from Telegram: ~1 s) */
  replacing: boolean
  /** What the in-flight replacement is jumping to */
  jumpingTo: JumpTarget | null
  jumpNotice: JumpNotice | null
  error: string | null
  loadingOlder: boolean
  loadingNewer: boolean
  backfill: BackfillState
  backfillError: string | null
  /** The server reported a Telegram sync of this chat in flight */
  syncing: boolean
  /** Auto-sync gave up (chat_sync_failed) */
  syncFailed: { status: string; error: string | null } | null
  /** A completed fetch found no retrievable history (chat_sync_empty) */
  syncEmpty: boolean
  firstUnreadTgId: number | null
  /** Visual highlight (cleared after HIGHLIGHT_MS) */
  highlightTgId: number | null
  /** One-shot scroll request for MessageList (consumed by id) */
  scrollIntent: ScrollIntent | null
}

const EMPTY: ChatMessagesState = {
  scopeKey: null, messages: [], hasOlder: false, hasNewer: false,
  loading: false, replacing: false, jumpingTo: null, jumpNotice: null, error: null, loadingOlder: false, loadingNewer: false,
  backfill: 'idle', backfillError: null, syncing: false, syncFailed: null, syncEmpty: false,
  firstUnreadTgId: null, highlightTgId: null, scrollIntent: null,
}

export function scopeKeyOf(chatId: number | null, topicId: number | null): string | null {
  return chatId == null ? null : `${chatId}|${topicId ?? ''}`
}

let intentSeq = 0
let noticeSeq = 0
const nextIntent = (i: { kind: 'bottom' } | { kind: 'message'; tgId: number }): ScrollIntent => ({ id: ++intentSeq, ...i })

export interface ChatMessages extends ChatMessagesState {
  atLatest: boolean
  loadOlder: () => Promise<void>
  loadNewer: () => Promise<void>
  /** Fetch older history from Telegram (also triggered by loadOlder when local history is exhausted) */
  fetchOlderFromTelegram: () => Promise<void>
  jumpTo: (tgId: number) => Promise<void>
  jumpToDate: (isoDate: string) => Promise<void>
  jumpToLatest: () => Promise<void>
  /** Re-read the newest page and merge it (after a sync/reconnect) */
  refresh: () => Promise<void>
  /** A message for this chat (own send / WS); ignored when it's out of scope */
  applyMessage: (msg: Message) => void
  patchMessage: (tgId: number, patch: Partial<Message> | ((m: Message) => Message)) => void
  removeMessages: (tgIds: number[]) => void
  clearHighlight: () => void
  dismissJumpNotice: () => void
}

export function useChatMessages(scope: ChatMessagesScope): ChatMessages {
  const { chatId, topicId, nonce } = scope
  const key = scopeKeyOf(chatId, topicId)
  const [state, setState] = useState<ChatMessagesState>(EMPTY)
  const stateRef = useRef(state)
  stateRef.current = state

  const scopeRef = useRef(scope)
  scopeRef.current = scope
  const keyRef = useRef<string | null>(key)
  /** Bumped whenever the window is replaced; page loads check it before applying */
  const genRef = useRef(0)
  const ctlsRef = useRef(new Set<AbortController>())
  const highlightTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const olderInFlight = useRef(false)
  const newerInFlight = useRef(false)
  const backfillInFlight = useRef(false)

  const abortAll = useCallback(() => {
    for (const c of ctlsRef.current) c.abort()
    ctlsRef.current.clear()
  }, [])

  const newCtl = useCallback(() => {
    const c = new AbortController()
    ctlsRef.current.add(c)
    return c
  }, [])

  /** setState that is dropped if the scope changed since `forKey` was captured */
  const update = useCallback((forKey: string | null, fn: (s: ChatMessagesState) => ChatMessagesState) => {
    if (forKey !== keyRef.current) return
    setState((s) => (s.scopeKey === forKey ? fn(s) : s))
  }, [])

  const highlight = useCallback((forKey: string | null, tgId: number) => {
    if (highlightTimerRef.current) clearTimeout(highlightTimerRef.current)
    update(forKey, (s) => ({ ...s, highlightTgId: tgId }))
    highlightTimerRef.current = setTimeout(() => {
      update(forKey, (s) => (s.highlightTgId === tgId ? { ...s, highlightTgId: null } : s))
    }, HIGHLIGHT_MS)
  }, [update])

  /** Replace the window. `pick` chooses the scroll intent from the new page. */
  const loadWindow = useCallback(async (
    params: MessageWindowParams,
    opts: {
      initial: boolean
      /** Set for jumps: shown while loading; failures become a JumpNotice */
      target?: JumpTarget
      pick: (page: MessageWindow) => { intent: ScrollIntent; highlightId?: number | null; notFound?: boolean }
    },
  ) => {
    const forKey = keyRef.current
    const id = scopeRef.current.chatId
    if (forKey == null || id == null) return
    const gen = ++genRef.current
    abortAll()
    olderInFlight.current = newerInFlight.current = backfillInFlight.current = false
    const ctl = newCtl()
    update(forKey, (s) => ({
      ...s, replacing: !opts.initial, jumpingTo: opts.target ?? null, jumpNotice: null,
      loadingOlder: false, loadingNewer: false, error: null,
    }))
    try {
      const page = await getMessageWindow(id, { ...params, topicId: scopeRef.current.topicId, limit: PAGE_SIZE }, ctl.signal)
      if (gen !== genRef.current) return
      const { intent, highlightId, notFound } = opts.pick(page)
      const notice: JumpNotice | null = notFound && opts.target
        ? { id: ++noticeSeq, target: opts.target, reason: page.anchor_reason ?? 'not_found' }
        : null
      if (notice && !opts.initial && stateRef.current.messages.length > 0) {
        // Keep the window the user was reading; just say why the jump didn't happen
        update(forKey, (s) => ({ ...s, replacing: false, jumpingTo: null, jumpNotice: notice }))
        return
      }
      update(forKey, (s) => ({
        ...s,
        ...fromPage(page),
        loading: false,
        replacing: false,
        jumpingTo: null,
        jumpNotice: notice,
        syncing: page.syncing,
        backfill: page.has_older ? 'idle' : s.backfill === 'exhausted' ? 'exhausted' : 'idle',
        firstUnreadTgId: opts.initial ? firstUnreadId(page.messages, scopeRef.current.unreadCount ?? 0) : s.firstUnreadTgId,
        scrollIntent: intent,
      }))
      if (highlightId != null) highlight(forKey, highlightId)
    } catch (e) {
      if (isAbortError(e) || gen !== genRef.current) return
      const failedJump = opts.target && !opts.initial && stateRef.current.messages.length > 0
      update(forKey, (s) => ({
        ...s, loading: false, replacing: false, jumpingTo: null,
        // A failed jump keeps the current window and reports inline; a failed first load is an error state
        ...(failedJump
          ? { jumpNotice: { id: ++noticeSeq, target: opts.target!, reason: 'error' as const, detail: errorMessage(e) } }
          : { error: errorMessage(e) }),
      }))
    } finally {
      ctlsRef.current.delete(ctl)
    }
  }, [abortAll, newCtl, update, highlight])

  const loadLatestInitial = useCallback(() => loadWindow({}, {
    initial: true,
    pick: (page) => {
      const unread = firstUnreadId(page.messages, scopeRef.current.unreadCount ?? 0)
      return { intent: unread != null ? nextIntent({ kind: 'message', tgId: unread }) : nextIntent({ kind: 'bottom' }) }
    },
  }), [loadWindow])

  const loadAnchor = useCallback((tgId: number, initial: boolean) => loadWindow({ anchor: tgId }, {
    initial,
    target: { kind: 'message', tgId },
    pick: (page) => page.anchor_found
      ? { intent: nextIntent({ kind: 'message', tgId }), highlightId: tgId }
      : { intent: nextIntent({ kind: 'bottom' }), notFound: true },
  }), [loadWindow])

  // Scope change (or re-selection): reset and load the first window
  useEffect(() => {
    keyRef.current = key
    genRef.current++
    abortAll()
    olderInFlight.current = newerInFlight.current = backfillInFlight.current = false
    if (highlightTimerRef.current) clearTimeout(highlightTimerRef.current)
    setState({ ...EMPTY, scopeKey: key, loading: key != null })
    if (key == null) return
    const anchor = scopeRef.current.anchorTgId
    if (anchor != null) void loadAnchor(anchor, true)
    else void loadLatestInitial()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, nonce])

  useEffect(() => () => {
    abortAll()
    if (highlightTimerRef.current) clearTimeout(highlightTimerRef.current)
  }, [abortAll])

  const fetchOlderFromTelegram = useCallback(async () => {
    const forKey = keyRef.current
    const id = scopeRef.current.chatId
    if (forKey == null || id == null || backfillInFlight.current) return
    backfillInFlight.current = true
    const gen = genRef.current
    const ctl = newCtl()
    update(forKey, (s) => ({ ...s, backfill: 'loading', backfillError: null }))
    try {
      const res = await backfillChat(id, { limit: 100, topicId: scopeRef.current.topicId }, ctl.signal)
      if (gen !== genRef.current) return
      if (res.added > 0) {
        const s = stateRef.current
        const oldest = s.messages[0]
        const page = await getMessageWindow(id, {
          ...(oldest ? { before: cursorOf(oldest) } : {}),
          topicId: scopeRef.current.topicId, limit: PAGE_SIZE,
        }, ctl.signal)
        if (gen !== genRef.current) return
        update(forKey, (st) => ({
          ...st,
          ...(oldest ? prependOlder(st, page) : fromPage(page)),
          backfill: page.has_older ? 'idle' : res.exhausted ? 'exhausted' : 'idle',
        }))
      } else {
        // Nothing new: exhausted → final; otherwise don't auto-loop, offer a retry
        update(forKey, (st) => ({ ...st, backfill: res.exhausted ? 'exhausted' : 'retry' }))
      }
    } catch (e) {
      if (isAbortError(e) || gen !== genRef.current) return
      update(forKey, (st) => ({ ...st, backfill: 'retry', backfillError: errorMessage(e) }))
    } finally {
      ctlsRef.current.delete(ctl)
      if (gen === genRef.current) backfillInFlight.current = false
    }
  }, [newCtl, update])

  const loadOlder = useCallback(async () => {
    const forKey = keyRef.current
    const id = scopeRef.current.chatId
    const s = stateRef.current
    if (forKey == null || id == null || s.loading || s.replacing || olderInFlight.current) return
    if (!s.hasOlder) {
      if (s.backfill === 'idle' && s.messages.length > 0) await fetchOlderFromTelegram()
      return
    }
    const oldest = s.messages[0]
    if (!oldest) return
    olderInFlight.current = true
    const gen = genRef.current
    const ctl = newCtl()
    update(forKey, (st) => ({ ...st, loadingOlder: true }))
    try {
      const page = await getMessageWindow(id, { before: cursorOf(oldest), topicId: scopeRef.current.topicId, limit: PAGE_SIZE }, ctl.signal)
      if (gen !== genRef.current) return
      update(forKey, (st) => ({ ...st, ...prependOlder(st, page, MAX_WINDOW), loadingOlder: false }))
    } catch (e) {
      if (isAbortError(e) || gen !== genRef.current) return
      update(forKey, (st) => ({ ...st, loadingOlder: false, error: errorMessage(e) }))
    } finally {
      ctlsRef.current.delete(ctl)
      if (gen === genRef.current) olderInFlight.current = false
    }
  }, [fetchOlderFromTelegram, newCtl, update])

  const loadNewer = useCallback(async () => {
    const forKey = keyRef.current
    const id = scopeRef.current.chatId
    const s = stateRef.current
    if (forKey == null || id == null || !s.hasNewer || s.loading || s.replacing || newerInFlight.current) return
    const newest = s.messages[s.messages.length - 1]
    if (!newest) return
    newerInFlight.current = true
    const gen = genRef.current
    const ctl = newCtl()
    update(forKey, (st) => ({ ...st, loadingNewer: true }))
    try {
      const page = await getMessageWindow(id, { after: cursorOf(newest), topicId: scopeRef.current.topicId, limit: PAGE_SIZE }, ctl.signal)
      if (gen !== genRef.current) return
      update(forKey, (st) => ({ ...st, ...appendNewer(st, page, MAX_WINDOW), loadingNewer: false }))
    } catch (e) {
      if (isAbortError(e) || gen !== genRef.current) return
      update(forKey, (st) => ({ ...st, loadingNewer: false, error: errorMessage(e) }))
    } finally {
      ctlsRef.current.delete(ctl)
      if (gen === genRef.current) newerInFlight.current = false
    }
  }, [newCtl, update])

  const jumpTo = useCallback(async (tgId: number) => {
    const forKey = keyRef.current
    if (forKey == null) return
    if (stateRef.current.messages.some((m) => m.tg_message_id === tgId)) {
      update(forKey, (s) => ({ ...s, scrollIntent: nextIntent({ kind: 'message', tgId }) }))
      highlight(forKey, tgId)
      return
    }
    await loadAnchor(tgId, false)
  }, [highlight, loadAnchor, update])

  const jumpToDate = useCallback((isoDate: string) => loadWindow({ date: isoDate }, {
    initial: false,
    target: { kind: 'date', date: isoDate },
    pick: (page) => {
      const target = page.messages.find((m) => m.date >= isoDate) ?? page.messages[page.messages.length - 1]
      return target && page.anchor_found
        ? { intent: nextIntent({ kind: 'message', tgId: target.tg_message_id }), highlightId: target.tg_message_id }
        : { intent: nextIntent({ kind: 'bottom' }), notFound: true }
    },
  }), [loadWindow])

  const dismissJumpNotice = useCallback(() => {
    update(keyRef.current, (s) => ({ ...s, jumpNotice: null }))
  }, [update])

  const jumpToLatest = useCallback(async () => {
    const forKey = keyRef.current
    if (forKey == null) return
    if (!stateRef.current.hasNewer) {
      update(forKey, (s) => ({ ...s, scrollIntent: nextIntent({ kind: 'bottom' }) }))
      return
    }
    await loadWindow({}, { initial: false, pick: () => ({ intent: nextIntent({ kind: 'bottom' }) }) })
  }, [loadWindow, update])

  const refresh = useCallback(async () => {
    const forKey = keyRef.current
    const id = scopeRef.current.chatId
    if (forKey == null || id == null) return
    const s = stateRef.current
    if (s.loading || s.replacing) return
    if (s.error && s.messages.length === 0) { await loadLatestInitial(); return }
    if (s.hasNewer) return // not at latest: "Jump to latest" already covers it
    // Doesn't bump the generation: it merges into the newest end, which no
    // other in-flight page touches, but a jump in the meantime discards it.
    const gen = genRef.current
    const ctl = newCtl()
    try {
      const page = await getMessageWindow(id, { topicId: scopeRef.current.topicId, limit: PAGE_SIZE }, ctl.signal)
      if (gen !== genRef.current) return
      update(forKey, (st) => {
        const wasEmpty = st.messages.length === 0
        const merged = mergeLatest(st, page)
        return {
          ...st,
          ...merged,
          syncing: page.syncing,
          // First history after a background sync: land at the bottom
          scrollIntent: wasEmpty && merged.messages.length > 0 ? nextIntent({ kind: 'bottom' }) : st.scrollIntent,
        }
      })
    } catch (e) {
      if (isAbortError(e) || gen !== genRef.current) return
      // A failed background refresh keeps the current window; the next event retries
      console.warn('message refresh failed:', e)
    } finally {
      ctlsRef.current.delete(ctl)
    }
  }, [loadLatestInitial, newCtl, update])

  const applyMessage = useCallback((msg: Message) => {
    const forKey = keyRef.current
    const sc = scopeRef.current
    if (forKey == null || sc.chatId == null || !inScope(msg, { chatId: sc.chatId, topicId: sc.topicId })) return
    update(forKey, (s) => {
      const next = applyLive(s, msg)
      return next === s ? s : { ...s, ...next, syncEmpty: false }
    })
  }, [update])

  const patchMessage = useCallback((tgId: number, patch: Partial<Message> | ((m: Message) => Message)) => {
    update(keyRef.current, (s) => {
      const idx = s.messages.findIndex((m) => m.tg_message_id === tgId)
      if (idx === -1) return s
      const messages = [...s.messages]
      messages[idx] = typeof patch === 'function' ? patch(messages[idx]) : { ...messages[idx], ...patch }
      return { ...s, messages }
    })
  }, [update])

  const replaceIfPresent = useCallback((msg: Message) => {
    patchMessage(msg.tg_message_id, () => msg)
  }, [patchMessage])

  const removeMessages = useCallback((tgIds: number[]) => {
    const ids = new Set(tgIds)
    update(keyRef.current, (s) => {
      const messages = s.messages.filter((m) => !ids.has(m.tg_message_id))
      return messages.length === s.messages.length ? s : { ...s, messages }
    })
  }, [update])

  const applyScamStates = useCallback((eventChatId: number, entries: ScamMsgState[] | undefined) => {
    if (!entries?.length || eventChatId !== scopeRef.current.chatId) return
    const byId = new Map(entries.filter((e) => e.chat_id === eventChatId).map((e) => [e.tg_message_id, e]))
    if (byId.size === 0) return
    update(keyRef.current, (s) => {
      let changed = false
      const messages = s.messages.map((m) => {
        const e = byId.get(m.tg_message_id)
        if (!e) return m
        changed = true
        return { ...m, ...e }
      })
      return changed ? { ...s, messages } : s
    })
  }, [update])

  const clearHighlight = useCallback(() => {
    if (highlightTimerRef.current) clearTimeout(highlightTimerRef.current)
    update(keyRef.current, (s) => ({ ...s, highlightTgId: null }))
  }, [update])

  const isMine = (eventChatId: number | null | undefined) =>
    eventChatId != null && eventChatId === scopeRef.current.chatId

  // ── Live updates ─────────────────────────────────────────────────────────
  useWsEvent('new_message', (e) => {
    if (!isMine(e.chat_id)) return
    if (e.message) applyMessage(e.message)
    else void refresh()
  })
  useWsEvent('message_edited', (e) => { if (isMine(e.chat_id)) replaceIfPresent(e.message) })
  useWsEvent('messages_deleted', (e) => {
    const id = scopeRef.current.chatId
    if (id != null && deletionApplies(e.chat_id, id)) removeMessages(e.message_ids)
  })
  useWsEvent('messages_pinned', (e) => {
    if (!isMine(e.chat_id)) return
    for (const tgId of e.message_ids) patchMessage(tgId, { is_pinned: e.pinned })
  })
  useWsEvent('media_ready', (e) => {
    if (isMine(e.chat_id)) patchMessage(e.tg_message_id, { media_url: e.media_url })
  })
  useWsEvent('chat_synced', (e) => {
    if (!isMine(e.chat_id)) return
    update(keyRef.current, (s) => ({ ...s, syncFailed: null, syncEmpty: false }))
    void refresh()
  })
  useWsEvent('chats_synced', (e) => {
    if (scopeRef.current.chatId != null && e.chat_ids.includes(scopeRef.current.chatId)) void refresh()
  })
  useWsEvent('chat_sync_failed', (e) => {
    if (isMine(e.chat_id)) update(keyRef.current, (s) => ({ ...s, syncing: false, syncFailed: { status: e.status, error: e.error } }))
  })
  useWsEvent('chat_sync_empty', (e) => {
    if (isMine(e.chat_id)) update(keyRef.current, (s) => ({ ...s, syncing: false, syncEmpty: true }))
  })
  useWsEvent('resync', () => { void refresh() })
  useWsEvent('scam_detected', (e) => applyScamStates(e.chat_id, e.messages))
  useWsEvent('scam_check_safe', (e) => applyScamStates(e.chat_id, e.messages))
  useWsEvent('scam_check_error', (e) => applyScamStates(e.chat_id, e.messages))
  useWsEvent('scam_verdict_changed', (e) => {
    const id = scopeRef.current.chatId
    if (id != null) applyScamStates(id, e.messages)
  })

  const atLatest = !state.hasNewer
  return useMemo(() => ({
    ...state,
    atLatest,
    loadOlder, loadNewer, fetchOlderFromTelegram, jumpTo, jumpToDate, jumpToLatest, refresh,
    applyMessage, patchMessage, removeMessages, clearHighlight, dismissJumpNotice,
  }), [state, atLatest, dismissJumpNotice, loadOlder, loadNewer, fetchOlderFromTelegram, jumpTo, jumpToDate, jumpToLatest, refresh,
    applyMessage, patchMessage, removeMessages, clearHighlight])
}
