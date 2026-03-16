import { useEffect, useRef, useState } from 'react'
import type { Message } from '../api/types'
import MessageBubble from './MessageBubble'

interface Props {
  messages: Message[]
  chatType: string
  highlightTelegramId?: number | null
  onReply?: (msg: Message) => void
  onReact?: (msg: Message, emoji: string | null) => void
}

export default function MessageList({ messages, chatType, highlightTelegramId, onReply, onReact }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const prevCountRef = useRef(0)
  const highlightRef = useRef<HTMLDivElement>(null)
  const isGroup = chatType === 'group' || chatType === 'channel'
  const [dimHighlight, setDimHighlight] = useState(false)

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
    // Instant jump on initial load; smooth scroll only when new messages arrive
    const behavior = prevCount === 0 ? 'instant' : 'smooth'
    bottomRef.current?.scrollIntoView({ behavior })
  }, [messages, highlightTelegramId])

  if (messages.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center text-slate-500 text-sm">
        No messages yet
      </div>
    )
  }

  return (
    <div className="flex flex-1 flex-col overflow-y-auto px-4 py-3">
      {messages.map((msg, i) => {
        const prev = messages[i - 1]
        const showSender =
          isGroup && !msg.is_outgoing && msg.sender_name !== prev?.sender_name
        const isHighlighted = highlightTelegramId != null && msg.telegram_id === highlightTelegramId && !dimHighlight

        return (
          <div key={msg.id} ref={highlightTelegramId === msg.telegram_id ? highlightRef : undefined}>
            <MessageBubble
              message={msg}
              showSender={showSender}
              showAvatar={showSender}
              isGroupChat={isGroup}
              highlighted={isHighlighted}
              onReply={onReply}
              onReact={onReact}
            />
          </div>
        )
      })}
      <div ref={bottomRef} />
    </div>
  )
}
