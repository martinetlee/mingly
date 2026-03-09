import { useState } from 'react'
import { Reply } from 'lucide-react'
import { Link } from 'react-router-dom'
import type { Message } from '../api/types'

interface Props {
  message: Message
  showSender: boolean
  highlighted?: boolean
  onReply?: (msg: Message) => void
}

function formatTime(dateStr: string) {
  return new Date(dateStr).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function safeHref(url: string): string {
  return /^https?:\/\//i.test(url) ? url : '#'
}

function simpleMarkdownToHtml(text: string): string {
  let s = escapeHtml(text)
  // [[display]](url) → [display] as link text (matching official Telegram); \s allows [[link]]\n(url)
  s = s.replace(/\[\[([^\]]+)\]\]\s*\(([^)]+)\)/g, (_, d, u) => `<a href="${safeHref(u)}" target="_blank" rel="noopener noreferrer">[${d}]</a>`)
  s = s.replace(/\[([^\]]+)\]\s*\(([^)]+)\)/g, (_, d, u) => `<a href="${safeHref(u)}" target="_blank" rel="noopener noreferrer">${d}</a>`)
  // **bold** / *italic* / `code`
  s = s.replace(/\*\*(.+?)\*\*/gs, '<b>$1</b>')
  s = s.replace(/\*(.+?)\*/gs, '<i>$1</i>')
  s = s.replace(/`(.+?)`/gs, '<code>$1</code>')
  // bare URLs not already inside <a>
  s = s.replace(/(?<!href=")https?:\/\/[^\s<"]+/g, (url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`)
  // newlines → <br>
  s = s.replace(/\n/g, '<br>')
  return s
}

function FormattedText({ html, isOut }: { html: string; isOut: boolean }) {
  return (
    <span
      className={`formatted-text ${isOut ? 'formatted-out' : 'formatted-in'}`}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}

export default function MessageBubble({ message, showSender, highlighted, onReply }: Props) {
  const isOut = message.is_outgoing
  const hasPhoto = message.media_url && message.media_type === 'photo'
  const [hovered, setHovered] = useState(false)
  const reply = message.reply_to_preview

  return (
    <div
      className={`flex ${isOut ? 'justify-end' : 'justify-start'} mb-1 group/bubble ${highlighted ? 'rounded-lg ring-2 ring-amber-400/50 ring-offset-2 ring-offset-slate-900 transition-all duration-700' : ''}`}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      {/* Reply button for outgoing messages — appears on the left */}
      {isOut && onReply && (
        <button
          onClick={() => onReply(message)}
          className={`mr-1 self-center rounded-full p-1 text-slate-500 transition-all hover:bg-slate-700 hover:text-slate-300 ${hovered ? 'opacity-100' : 'opacity-0'}`}
          title="Reply"
        >
          <Reply size={14} />
        </button>
      )}

      <div className={`max-w-[70%] ${isOut ? 'items-end' : 'items-start'} flex flex-col`}>
        {showSender && !isOut && message.sender_name && (
          message.sender_id ? (
            <Link
              to={`/profile/${message.sender_id}`}
              className="mb-0.5 ml-1 text-xs font-medium text-purple-400 hover:text-purple-300 transition-colors"
            >
              {message.sender_name}
            </Link>
          ) : (
            <span className="mb-0.5 ml-1 text-xs font-medium text-purple-400">
              {message.sender_name}
            </span>
          )
        )}
        <div
          className={`rounded-2xl text-sm leading-relaxed overflow-hidden ${
            isOut
              ? 'rounded-br-sm bg-indigo-600 text-white'
              : 'rounded-bl-sm bg-slate-700 text-gray-100 border border-slate-600'
          }`}
        >
          {/* Reply-to quote block */}
          {reply && (
            <div className={`mx-2 mt-2 flex rounded-lg border-l-2 px-2 py-1 ${
              isOut
                ? 'border-indigo-300/60 bg-indigo-700/60'
                : 'border-purple-400/60 bg-slate-600/60'
            }`}>
              <div className="min-w-0">
                <p className={`text-[11px] font-semibold truncate ${isOut ? 'text-indigo-200' : 'text-purple-300'}`}>
                  {reply.sender_name ?? 'Unknown'}
                </p>
                <p className={`truncate text-[11px] ${isOut ? 'text-indigo-300' : 'text-slate-400'}`}>
                  {reply.text
                    ? reply.text.slice(0, 80)
                    : reply.media_type
                      ? `[${reply.media_type}]`
                      : ''}
                </p>
              </div>
            </div>
          )}
          {message.text && (
            <p className="px-3 py-2">
              <FormattedText html={message.text_html ?? simpleMarkdownToHtml(message.text)} isOut={isOut} />
            </p>
          )}
          {hasPhoto && (
            <img
              src={message.media_url!}
              alt="photo"
              className={`block object-cover ${isOut ? 'ml-auto' : 'mr-auto'}`}
              style={{ maxWidth: 320, maxHeight: 320 }}
            />
          )}
          {message.media_type && !hasPhoto && !message.text && (
            <span className={`px-3 py-2 block italic ${isOut ? 'text-indigo-300' : 'text-slate-400'}`}>
              [{message.media_type}]
            </span>
          )}
        </div>

        {/* Reactions */}
        {message.reactions && message.reactions.length > 0 && (
          <div className={`flex flex-wrap gap-1 mt-0.5 ${isOut ? 'mr-1' : 'ml-1'}`}>
            {message.reactions.map((r, i) => (
              <span
                key={i}
                className="inline-flex items-center gap-0.5 rounded-full bg-slate-700/80 border border-slate-600 px-1.5 py-0.5 text-xs"
              >
                <span>{r.emoji}</span>
                <span className="text-slate-400">{r.count}</span>
              </span>
            ))}
          </div>
        )}

        <span className={`mt-0.5 text-[10px] text-slate-500 ${isOut ? 'mr-1' : 'ml-1'}`}>
          {formatTime(message.date)}
        </span>
      </div>

      {/* Reply button for incoming messages — appears on the right */}
      {!isOut && onReply && (
        <button
          onClick={() => onReply(message)}
          className={`ml-1 self-center rounded-full p-1 text-slate-500 transition-all hover:bg-slate-700 hover:text-slate-300 ${hovered ? 'opacity-100' : 'opacity-0'}`}
          title="Reply"
        >
          <Reply size={14} />
        </button>
      )}
    </div>
  )
}
