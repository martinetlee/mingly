import type { Message } from '../api/types'

interface Props {
  message: Message
  showSender: boolean
}

function formatTime(dateStr: string) {
  return new Date(dateStr).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export default function MessageBubble({ message, showSender }: Props) {
  const isOut = message.is_outgoing

  return (
    <div className={`flex ${isOut ? 'justify-end' : 'justify-start'} mb-1`}>
      <div className={`max-w-[70%] ${isOut ? 'items-end' : 'items-start'} flex flex-col`}>
        {showSender && !isOut && message.sender_name && (
          <span className="mb-0.5 ml-1 text-xs font-medium text-blue-600">
            {message.sender_name}
          </span>
        )}
        <div
          className={`rounded-2xl px-3 py-2 text-sm leading-relaxed ${
            isOut
              ? 'rounded-br-sm bg-blue-500 text-white'
              : 'rounded-bl-sm bg-white text-gray-900 shadow-sm border border-gray-100'
          }`}
        >
          {message.media_type && !message.text && (
            <span className="italic text-opacity-70">
              [{message.media_type}]
            </span>
          )}
          {message.text}
        </div>
        <span className={`mt-0.5 text-[10px] text-gray-400 ${isOut ? 'mr-1' : 'ml-1'}`}>
          {formatTime(message.date)}
        </span>
      </div>
    </div>
  )
}
