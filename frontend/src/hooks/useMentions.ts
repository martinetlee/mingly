import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { getMentions, reactToMessage } from '../api/client'
import type { MentionGroup } from '../api/types'
import { reportError } from '../lib/toast'
import { useLatestQuery } from './useLatestQuery'
import { useCoalescedRefetch } from './useCoalescedRefetch'
import { useWebSocket } from './useWebSocket'
import { useKeyboardShortcuts } from './useKeyboardShortcuts'

export interface MentionFilters {
  days: number
  groupName?: string
  tagName?: string
}

/** Wait after an incoming message before refetching, so the backend has stored it */
const NEW_MESSAGE_SETTLE_MS = 1500
const REFETCH_INTERVAL_MS = 3000

/**
 * Mentions for the given server-side filters, shared by Home and
 * Intelligence. Results are bound to the filters they were requested for
 * (latest-only, aborting superseded requests), and WebSocket storms
 * (new_message, chats_synced, bulk_sync_complete, resync) collapse into at
 * most one refetch per few seconds.
 */
export function useMentions(filters: MentionFilters) {
  const { days, groupName = '', tagName = '' } = filters
  const query = useLatestQuery(
    ['mentions', days, groupName, tagName],
    (signal) => getMentions({ days, groupName, tagName }, signal),
  )
  const { refetch, setData } = query

  const refresh = useCoalescedRefetch(refetch, REFETCH_INTERVAL_MS)
  const settleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => {
    if (settleTimerRef.current) clearTimeout(settleTimerRef.current)
  }, [])

  useWebSocket((evt) => {
    if (evt.type === 'new_message' && !evt.is_outgoing) {
      // One pending timer at a time: a burst of messages yields one refetch
      if (settleTimerRef.current) return
      settleTimerRef.current = setTimeout(() => {
        settleTimerRef.current = null
        refresh()
      }, NEW_MESSAGE_SETTLE_MS)
    } else if (evt.type === 'chats_synced' || evt.type === 'bulk_sync_complete' || evt.type === 'resync') {
      refresh()
    }
  })

  /** Toggle the 👀 acknowledgement reaction on a mention (optimistic, reverts on failure). */
  const toggleAcknowledge = useCallback((g: MentionGroup) => {
    const chatId = g.chat.telegram_id
    const msgId = g.mention_msg.tg_message_id
    const previous = g.mention_msg.my_reaction ?? null
    const emoji = previous ? null : '👀'
    const patch = (reaction: string | null) =>
      setData((prev) => prev?.map((pg) =>
        pg.chat.telegram_id === chatId && pg.mention_msg.tg_message_id === msgId
          ? { ...pg, mention_msg: { ...pg.mention_msg, my_reaction: reaction } }
          : pg,
      ))
    patch(emoji)
    reactToMessage(chatId, msgId, emoji).catch((e) => {
      patch(previous)
      reportError(e, 'Reaction failed')
    })
  }, [setData])

  return {
    groups: query.data ?? [],
    loading: query.loading,
    fetching: query.fetching,
    error: query.error,
    refetch,
    toggleAcknowledge,
  }
}

/**
 * j/k (arrows) selection, `a` acknowledge and `r` reply over a visible list
 * of mentions. Returns the selected index and a ref for the selected card.
 */
export function useMentionNavigator(
  visible: MentionGroup[],
  { enabled = true, onAcknowledge }: { enabled?: boolean; onAcknowledge: (g: MentionGroup) => void },
) {
  const navigate = useNavigate()
  const [selectedIdx, setSelectedIdx] = useState(0)
  const selectedRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (visible.length > 0) setSelectedIdx((i) => Math.min(i, visible.length - 1))
  }, [visible.length])

  useEffect(() => {
    selectedRef.current?.scrollIntoView?.({ block: 'nearest' })
  }, [selectedIdx])

  const openInChat = useCallback((chatId: number, msgId: number) => {
    navigate(`/chats?chatId=${chatId}&msgId=${msgId}`)
  }, [navigate])

  const shortcuts = useMemo(() => ({
    j: () => setSelectedIdx((i) => Math.min(i + 1, visible.length - 1)),
    arrowdown: () => setSelectedIdx((i) => Math.min(i + 1, visible.length - 1)),
    k: () => setSelectedIdx((i) => Math.max(i - 1, 0)),
    arrowup: () => setSelectedIdx((i) => Math.max(i - 1, 0)),
    a: () => {
      const g = visible[selectedIdx]
      if (!g) return false
      onAcknowledge(g)
    },
    r: () => {
      const g = visible[selectedIdx]
      if (!g) return false
      openInChat(g.chat.telegram_id, g.mention_msg.tg_message_id)
    },
  }), [visible, selectedIdx, onAcknowledge, openInChat])

  useKeyboardShortcuts(shortcuts, { enabled, yieldScrollKeys: false })

  return { selectedIdx, setSelectedIdx, selectedRef, openInChat }
}
