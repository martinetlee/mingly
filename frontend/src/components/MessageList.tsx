import { useCallback, useEffect, useRef, useState } from 'react'
import type { Message } from '../api/types'
import MessageBubble from './MessageBubble'
import MediaAlbumGrid from './MediaAlbumGrid'

interface Props {
  messages: Message[]
  chatType: string
  highlightTelegramId?: number | null
  onReply?: (msg: Message) => void
  onReact?: (msg: Message, emoji: string | null) => void
  onEdit?: (msg: Message) => void
  onDelete?: (msg: Message) => void
  onForward?: (msg: Message) => void
  /** Number of unread messages from the end of the list */
  unreadCount?: number
  /** Called when user scrolls near the top */
  onScrollNearTop?: () => void
  /** Whether older messages are currently loading */
  loadingOlder?: boolean
}

/**
 * Group consecutive messages that share the same grouped_id into album clusters.
 * Returns an array of items: either a single message or an album group.
 */
function groupMessages(messages: Message[]): Array<{ type: 'single'; message: Message } | { type: 'album'; messages: Message[]; groupedId: number }> {
  const result: Array<{ type: 'single'; message: Message } | { type: 'album'; messages: Message[]; groupedId: number }> = []
  let i = 0

  while (i < messages.length) {
    const msg = messages[i]
    if (msg.grouped_id) {
      // Collect all consecutive messages with the same grouped_id
      const albumMessages: Message[] = [msg]
      let j = i + 1
      while (j < messages.length && messages[j].grouped_id === msg.grouped_id) {
        albumMessages.push(messages[j])
        j++
      }
      if (albumMessages.length > 1) {
        result.push({ type: 'album', messages: albumMessages, groupedId: msg.grouped_id })
        i = j
        continue
      }
    }
    result.push({ type: 'single', message: msg })
    i++
  }

  return result
}

/** Separator line between read and unread messages */
function UnreadSeparator({ innerRef }: { innerRef?: React.Ref<HTMLDivElement> }) {
  return (
    <div ref={innerRef} className="flex items-center gap-3 my-2 px-2">
      <div className="flex-1 border-t border-indigo-500/50" />
      <span className="flex-shrink-0 text-[11px] font-medium text-indigo-400">
        Unread messages
      </span>
      <div className="flex-1 border-t border-indigo-500/50" />
    </div>
  )
}

export default function MessageList({ messages, chatType, highlightTelegramId, onReply, onReact, onEdit, onDelete, onForward, unreadCount, onScrollNearTop, loadingOlder }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const prevCountRef = useRef(0)
  const highlightRef = useRef<HTMLDivElement>(null)
  const unreadRef = useRef<HTMLDivElement>(null)
  const isGroup = chatType === 'group' || chatType === 'channel'
  const [dimHighlight, setDimHighlight] = useState(false)

  // Compute index where unread separator should appear
  // unreadCount = N means the last N messages are unread
  const unreadSeparatorIndex = (unreadCount && unreadCount > 0 && unreadCount < messages.length)
    ? messages.length - unreadCount
    : null

  useEffect(() => {
    const prevCount = prevCountRef.current
    prevCountRef.current = messages.length
    if (highlightTelegramId && highlightRef.current) {
      // Scroll to highlighted message instead of bottom
      highlightRef.current.scrollIntoView({ behavior: 'instant', block: 'center' })
      // Clear highlight after 3s
      setDimHighlight(false)
      const t = setTimeout(() => setDimHighlight(true), 3000)
      return () => clearTimeout(t)
    }
    // Jump to unread separator on initial load if present
    if (prevCount === 0 && unreadRef.current) {
      unreadRef.current.scrollIntoView({ behavior: 'instant', block: 'center' })
      return
    }
    // Instant jump on initial load; smooth scroll only when new messages arrive
    const behavior = prevCount === 0 ? 'instant' : 'smooth'
    bottomRef.current?.scrollIntoView({ behavior })
  }, [messages, highlightTelegramId])

  // Preserve scroll position when older messages are prepended
  const prevScrollHeightRef = useRef(0)
  useEffect(() => {
    const el = scrollContainerRef.current
    if (!el) return
    const prevCount = prevCountRef.current
    // If messages were prepended (count grew but we're not at initial load)
    if (prevCount > 0 && messages.length > prevCount) {
      const heightDiff = el.scrollHeight - prevScrollHeightRef.current
      if (heightDiff > 0 && el.scrollTop < 100) {
        el.scrollTop = heightDiff
      }
    }
    prevScrollHeightRef.current = el.scrollHeight
  })

  const handleScroll = useCallback(() => {
    const el = scrollContainerRef.current
    if (!el || !onScrollNearTop) return
    if (el.scrollTop < 200) {
      onScrollNearTop()
    }
  }, [onScrollNearTop])

  if (messages.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center text-slate-500 text-sm">
        No messages yet
      </div>
    )
  }

  const grouped = groupMessages(messages)

  return (
    <div ref={scrollContainerRef} onScroll={handleScroll} className="flex flex-1 flex-col overflow-y-auto px-4 py-3">
      {loadingOlder && (
        <div className="flex justify-center py-3">
          <div className="h-5 w-5 animate-spin rounded-full border-2 border-indigo-400 border-t-transparent" />
        </div>
      )}
      {grouped.map((item, idx) => {
        if (item.type === 'album') {
          const firstMsg = item.messages[0]
          const isOut = firstMsg.is_outgoing
          const isHighlighted = item.messages.some(
            (m) => highlightTelegramId != null && m.tg_message_id === highlightTelegramId && !dimHighlight
          )
          const hasHighlightRef = item.messages.some(
            (m) => highlightTelegramId === m.tg_message_id
          )
          // Collect text from all album messages (captions)
          const captionMsg = item.messages.find((m) => m.text)

          return (
            <div
              key={`album-${item.groupedId}`}
              ref={hasHighlightRef ? highlightRef : undefined}
              className={`flex ${isOut ? 'justify-end' : 'justify-start'} mb-1 ${
                isHighlighted ? 'rounded-lg ring-2 ring-amber-400/50 ring-offset-2 ring-offset-slate-900 transition-all duration-700' : ''
              }`}
            >
              <div className={`max-w-[70%] ${isOut ? 'items-end' : 'items-start'} flex flex-col`}>
                <div
                  className={`rounded-2xl text-sm leading-relaxed overflow-hidden ${
                    isOut
                      ? 'rounded-br-sm bg-indigo-600 text-white'
                      : 'rounded-bl-sm bg-slate-700 text-gray-100 border border-slate-600'
                  }`}
                >
                  <MediaAlbumGrid messages={item.messages} isOut={isOut} />
                  {captionMsg?.text && (
                    <p className="px-3 py-2 text-sm">{captionMsg.text}</p>
                  )}
                </div>
                <span className={`mt-0.5 text-[10px] text-slate-500 ${isOut ? 'mr-1' : 'ml-1'}`}>
                  {new Date(firstMsg.date).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                </span>
              </div>
            </div>
          )
        }

        const msg = item.message
        const prevItem = idx > 0 ? grouped[idx - 1] : null
        const prevMsg = prevItem?.type === 'single' ? prevItem.message : prevItem?.type === 'album' ? prevItem.messages[prevItem.messages.length - 1] : undefined
        const showSender =
          isGroup && !msg.is_outgoing && msg.sender_name !== prevMsg?.sender_name
        const isHighlighted = highlightTelegramId != null && msg.tg_message_id === highlightTelegramId && !dimHighlight

        return (
          <div key={msg.id}>
            {unreadSeparatorIndex === idx && (
              <UnreadSeparator innerRef={unreadRef} />
            )}
            <div ref={highlightTelegramId === msg.tg_message_id ? highlightRef : undefined}>
              <MessageBubble
                message={msg}
                showSender={showSender}
                showAvatar={showSender}
                isGroupChat={isGroup}
                highlighted={isHighlighted}
                onReply={onReply}
                onReact={onReact}
                onEdit={onEdit}
                onDelete={onDelete}
                onForward={onForward}
              />
            </div>
          </div>
        )
      })}
      <div ref={bottomRef} />
    </div>
  )
}
