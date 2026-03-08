import { useEffect, useRef } from 'react'
import type { Message } from '../api/types'
import MessageBubble from './MessageBubble'

interface Props {
  messages: Message[]
  chatType: string
}

export default function MessageList({ messages, chatType }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const isGroup = chatType === 'group' || chatType === 'channel'

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  if (messages.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center text-gray-400 text-sm">
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
