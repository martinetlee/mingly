import { useEffect, useRef } from 'react'
import type { Message } from '../api/types'
import MessageBubble from './MessageBubble'

interface Props {
  messages: Message[]
  chatType: string
}

export default function MessageList({ messages, chatType }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const prevCountRef = useRef(0)
  const isGroup = chatType === 'group' || chatType === 'channel'

  useEffect(() => {
    const prevCount = prevCountRef.current
    prevCountRef.current = messages.length
    // Instant jump on initial load; smooth scroll only when new messages arrive
    const behavior = prevCount === 0 ? 'instant' : 'smooth'
    bottomRef.current?.scrollIntoView({ behavior })
  }, [messages])

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

        return (
          <MessageBubble
            key={msg.id}
            message={msg}
            showSender={showSender}
          />
        )
      })}
      <div ref={bottomRef} />
    </div>
  )
}
