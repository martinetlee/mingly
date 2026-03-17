import { useRef, useState, useCallback } from 'react'
import { Reply, Forward, Pencil, Trash2, MoreVertical, Check, CheckCheck, Clock, Loader2 } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import type { Message } from '../api/types'
import { authenticatedMediaUrl } from '../api/session'
import ImageLightbox from './ImageLightbox'
import SenderAvatar from './SenderAvatar'
import VideoPlayer from './VideoPlayer'
import AudioPlayer from './AudioPlayer'
import DocumentCard from './DocumentCard'
import GifPlayer from './GifPlayer'

interface Props {
  message: Message
  showSender: boolean
  highlighted?: boolean
  onReply?: (msg: Message) => void
  onReact?: (msg: Message, emoji: string | null) => void
  onEdit?: (msg: Message) => void
  onDelete?: (msg: Message) => void
  onForward?: (msg: Message) => void
  /** Show avatar on this message (first in a sender group) */
  showAvatar?: boolean
  /** Whether this is a group/channel chat (controls avatar indent) */
  isGroupChat?: boolean
}

// Common quick-reaction emojis (mirrors Telegram's default set)
const QUICK_EMOJIS = ['👍', '❤️', '🔥', '🥰', '👏', '😁', '🤔', '🤯', '😱', '🙏', '👌', '🎉']

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
  const trimmed = url.trim()
  if (trimmed.startsWith('/')) return url
  return /^https?:\/\//i.test(trimmed) ? url : '#'
}

/** Sanitize all href attributes in an HTML string to block dangerous schemes. */
function sanitizeHrefs(html: string): string {
  return html.replace(/href="([^"]*)"/g, (_, url) => `href="${safeHref(url)}"`)
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

/** Convert tg://user mention links to in-app /profile links and wrap plain @username text */
function processMentions(html: string): string {
  // Convert any remaining tg://user?id=X links from older syncs
  let s = html.replace(
    /href="tg:\/\/user\?id=(\d+)"/g,
    'href="/profile/$1" data-mention="true"',
  )
  // Wrap plain @username text that isn't already inside an <a> tag
  s = s.replace(
    /(?<!["\/\w])@([A-Za-z]\w{3,})/g,
    '<a href="/profile/@$1" data-mention="true" class="mention-link">@$1</a>',
  )
  return s
}

function FormattedText({ html, isOut }: { html: string; isOut: boolean }) {
  const navigate = useNavigate()

  const handleClick = useCallback((e: React.MouseEvent<HTMLSpanElement>) => {
    const anchor = (e.target as HTMLElement).closest('a')
    if (!anchor) return
    const href = anchor.getAttribute('href')
    if (href && href.startsWith('/profile/')) {
      e.preventDefault()
      navigate(href)
    }
  }, [navigate])

  return (
    <span
      className={`formatted-text ${isOut ? 'formatted-out' : 'formatted-in'}`}
      dangerouslySetInnerHTML={{ __html: sanitizeHrefs(processMentions(html)) }}
      onClick={handleClick}
    />
  )
}

/** Delivery state indicator for outgoing messages */
function DeliveryIndicator({ state }: { state?: string }) {
  switch (state) {
    case 'sending':
      return <Loader2 size={12} className="text-indigo-300/60 animate-spin" />
    case 'sent':
      return <Check size={12} className="text-indigo-300/60" />
    case 'delivered':
      return <CheckCheck size={12} className="text-indigo-300/60" />
    case 'read':
      return <CheckCheck size={12} className="text-sky-400" />
    default:
      return <Check size={12} className="text-indigo-300/60" />
  }
}

/** Floating emoji picker shown on hover */
function EmojiPicker({
  isOut,
  myReaction,
  onSelect,
}: {
  isOut: boolean
  myReaction: string | null | undefined
  onSelect: (emoji: string | null) => void
}) {
  return (
    <div
      className={`absolute bottom-full mb-1 z-50 flex gap-0.5 rounded-xl border border-slate-600 bg-slate-800 shadow-xl p-1 ${
        isOut ? 'right-0' : 'left-0'
      }`}
    >
      {QUICK_EMOJIS.map((e) => (
        <button
          key={e}
          onClick={() => onSelect(myReaction === e ? null : e)}
          className={`flex h-7 w-7 items-center justify-center rounded-lg text-base transition-all hover:scale-125 hover:bg-slate-700 ${
            myReaction === e ? 'bg-indigo-500/30 ring-1 ring-indigo-400' : ''
          }`}
          title={myReaction === e ? 'Remove reaction' : e}
        >
          {e}
        </button>
      ))}
    </div>
  )
}

/** Context menu dropdown for message actions */
function ContextMenu({
  isOut,
  canEdit,
  canDelete,
  canForward,
  onEdit,
  onDelete,
  onForward,
}: {
  isOut: boolean
  canEdit: boolean
  canDelete: boolean
  canForward: boolean
  onEdit: () => void
  onDelete: () => void
  onForward: () => void
}) {
  return (
    <div
      className={`absolute bottom-full mb-1 z-50 min-w-[140px] rounded-lg border border-slate-600 bg-slate-800 shadow-xl py-1 ${
        isOut ? 'right-0' : 'left-0'
      }`}
    >
      {canForward && (
        <button
          onClick={onForward}
          className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-700 transition-colors"
        >
          <Forward size={13} />
          Forward
        </button>
      )}
      {canEdit && (
        <button
          onClick={onEdit}
          className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-700 transition-colors"
        >
          <Pencil size={13} />
          Edit
        </button>
      )}
      {canDelete && (
        <button
          onClick={onDelete}
          className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-red-400 hover:bg-slate-700 transition-colors"
        >
          <Trash2 size={13} />
          Delete
        </button>
      )}
    </div>
  )
}

export default function MessageBubble({ message, showSender, highlighted, onReply, onReact, onEdit, onDelete, onForward, showAvatar, isGroupChat }: Props) {
  const isOut = message.is_outgoing
  const hasPhoto = message.media_url && message.media_type === 'photo'
  const hasVideo = message.media_type === 'video'
  const hasVideoNote = message.media_type === 'video_note'
  const hasVoice = message.media_type === 'voice'
  const hasAudio = message.media_type === 'audio'
  const hasPlayableMedia = hasVideo || hasVideoNote || hasVoice || hasAudio
  const hasGif = message.media_url && message.media_type === 'gif'
  const hasDocument = message.media_type === 'document'
  const [hovered, setHovered] = useState(false)
  const [showPicker, setShowPicker] = useState(false)
  const [showContextMenu, setShowContextMenu] = useState(false)
  const [lightboxOpen, setLightboxOpen] = useState(false)
  const pickerRef = useRef<HTMLDivElement>(null)
  const contextMenuRef = useRef<HTMLDivElement>(null)
  const reply = message.reply_to_preview

  const handleSelectEmoji = (emoji: string | null) => {
    setShowPicker(false)
    onReact?.(message, emoji)
  }

  const showAvatarColumn = isGroupChat && !isOut

  return (
    <div
      className={`flex ${isOut ? 'justify-end' : 'justify-start'} mb-1 group/bubble ${highlighted ? 'rounded-lg ring-2 ring-amber-400/50 ring-offset-2 ring-offset-slate-900 transition-all duration-700' : ''}`}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => { setHovered(false); setShowPicker(false); setShowContextMenu(false) }}
      onContextMenu={(e) => {
        if (onEdit || onDelete || onForward) {
          e.preventDefault()
          setShowContextMenu((v) => !v)
        }
      }}
    >
      {/* Action buttons for outgoing messages — appear on the left */}
      {isOut && (
        <div className={`mr-1 flex items-center gap-0.5 self-center transition-opacity ${hovered ? 'opacity-100' : 'opacity-0'}`}>
          {/* Emoji reaction button */}
          {onReact && (
            <div className="relative" ref={pickerRef}>
              <button
                onClick={() => setShowPicker((v) => !v)}
                className="rounded-full p-1 text-slate-500 hover:bg-slate-700 hover:text-amber-400 transition-all"
                title="React with emoji"
              >
                <span className="text-sm leading-none">😊</span>
              </button>
              {showPicker && (
                <EmojiPicker isOut={isOut} myReaction={message.my_reaction} onSelect={handleSelectEmoji} />
              )}
            </div>
          )}
          {onReply && (
            <button
              onClick={() => onReply(message)}
              className="rounded-full p-1 text-slate-500 hover:bg-slate-700 hover:text-slate-300 transition-all"
              title="Reply"
            >
              <Reply size={14} />
            </button>
          )}
          {/* More actions menu */}
          {(onEdit || onDelete || onForward) && (
            <div className="relative" ref={contextMenuRef}>
              <button
                onClick={() => setShowContextMenu((v) => !v)}
                className="rounded-full p-1 text-slate-500 hover:bg-slate-700 hover:text-slate-300 transition-all"
                title="More actions"
              >
                <MoreVertical size={14} />
              </button>
              {showContextMenu && (
                <ContextMenu
                  isOut={isOut}
                  canEdit={isOut && !!message.text}
                  canDelete
                  canForward
                  onEdit={() => { setShowContextMenu(false); onEdit?.(message) }}
                  onDelete={() => { setShowContextMenu(false); onDelete?.(message) }}
                  onForward={() => { setShowContextMenu(false); onForward?.(message) }}
                />
              )}
            </div>
          )}
        </div>
      )}

      {/* Avatar column for incoming messages in group chats */}
      {showAvatarColumn && (
        <div className="w-7 flex-shrink-0 mr-2 self-end">
          {showAvatar ? (
            <SenderAvatar senderId={message.sender_id} senderName={message.sender_name} size={28} />
          ) : (
            /* Transparent spacer to keep messages aligned */
            <div className="w-7" />
          )}
        </div>
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
            <>
              <img
                src={authenticatedMediaUrl(message.media_url!)}
                alt="photo"
                className={`block cursor-pointer object-cover ${isOut ? 'ml-auto' : 'mr-auto'} hover:opacity-90 transition-opacity`}
                style={{ maxWidth: 320, maxHeight: 320 }}
                onClick={() => setLightboxOpen(true)}
              />
              {lightboxOpen && (
                <ImageLightbox
                  src={authenticatedMediaUrl(message.media_url!)}
                  onClose={() => setLightboxOpen(false)}
                />
              )}
            </>
          )}
          {(hasVideo || hasVideoNote) && (
            <VideoPlayer
              chatTelegramId={message.chat_id}
              msgTelegramId={message.telegram_id}
              mediaUrl={message.media_url || null}
              isVideoNote={hasVideoNote}
              isOut={isOut}
            />
          )}
          {(hasVoice || hasAudio) && (
            <AudioPlayer
              chatTelegramId={message.chat_id}
              msgTelegramId={message.telegram_id}
              mediaUrl={message.media_url || null}
              mediaType={hasVoice ? 'voice' : 'audio'}
              isOut={isOut}
            />
          )}
          {hasGif && (
            <GifPlayer mediaUrl={message.media_url!} isOut={isOut} />
          )}
          {hasDocument && (
            <DocumentCard
              filename={message.media_filename ?? null}
              fileSize={message.media_size ?? null}
              mediaUrl={message.media_url ?? null}
              isOut={isOut}
            />
          )}
          {message.media_type && !hasPhoto && !hasPlayableMedia && !hasGif && !hasDocument && !message.text && (
            <span className={`px-3 py-2 block italic ${isOut ? 'text-indigo-300' : 'text-slate-400'}`}>
              [{message.media_type}]
            </span>
          )}
        </div>

        {/* Reactions row: existing aggregate reactions + my reaction highlight */}
        {(message.reactions && message.reactions.length > 0) || message.my_reaction ? (
          <div className={`flex flex-wrap gap-1 mt-0.5 ${isOut ? 'mr-1' : 'ml-1'}`}>
            {(message.reactions ?? []).map((r, i) => (
              <button
                key={i}
                onClick={() => onReact?.(message, message.my_reaction === r.emoji ? null : r.emoji)}
                className={`inline-flex items-center gap-0.5 rounded-full border px-1.5 py-0.5 text-xs transition-all hover:border-indigo-400 ${
                  message.my_reaction === r.emoji
                    ? 'border-indigo-400/60 bg-indigo-500/20 text-indigo-300'
                    : 'border-slate-600 bg-slate-700/80 text-slate-300'
                }`}
                title={message.my_reaction === r.emoji ? 'Remove reaction' : `React with ${r.emoji}`}
              >
                <span>{r.emoji}</span>
                <span className="text-slate-400">{r.count}</span>
              </button>
            ))}
            {/* Show my_reaction pill if not already in aggregate reactions */}
            {message.my_reaction && !(message.reactions ?? []).some((r) => r.emoji === message.my_reaction) && (
              <button
                onClick={() => onReact?.(message, null)}
                className="inline-flex items-center gap-0.5 rounded-full border border-indigo-400/60 bg-indigo-500/20 px-1.5 py-0.5 text-xs text-indigo-300 hover:border-red-400/60 transition-all"
                title="Remove reaction"
              >
                <span>{message.my_reaction}</span>
                <span className="text-slate-400">1</span>
              </button>
            )}
          </div>
        ) : null}

        <span className={`mt-0.5 flex items-center gap-1 text-[10px] text-slate-500 ${isOut ? 'mr-1' : 'ml-1'}`}>
          {message.is_edited && (
            <span className="italic text-slate-500/70">edited</span>
          )}
          {formatTime(message.date)}
          {isOut && <DeliveryIndicator state={message.delivery_state} />}
        </span>
      </div>

      {/* Action buttons for incoming messages — appear on the right */}
      {!isOut && (
        <div className={`ml-1 flex items-center gap-0.5 self-center transition-opacity ${hovered ? 'opacity-100' : 'opacity-0'}`}>
          {onReact && (
            <div className="relative" ref={pickerRef}>
              <button
                onClick={() => setShowPicker((v) => !v)}
                className="rounded-full p-1 text-slate-500 hover:bg-slate-700 hover:text-amber-400 transition-all"
                title="React with emoji"
              >
                <span className="text-sm leading-none">😊</span>
              </button>
              {showPicker && (
                <EmojiPicker isOut={isOut} myReaction={message.my_reaction} onSelect={handleSelectEmoji} />
              )}
            </div>
          )}
          {onReply && (
            <button
              onClick={() => onReply(message)}
              className="rounded-full p-1 text-slate-500 hover:bg-slate-700 hover:text-slate-300 transition-all"
              title="Reply"
            >
              <Reply size={14} />
            </button>
          )}
          {/* More actions menu (forward & delete for incoming) */}
          {(onDelete || onForward) && (
            <div className="relative" ref={contextMenuRef}>
              <button
                onClick={() => setShowContextMenu((v) => !v)}
                className="rounded-full p-1 text-slate-500 hover:bg-slate-700 hover:text-slate-300 transition-all"
                title="More actions"
              >
                <MoreVertical size={14} />
              </button>
              {showContextMenu && (
                <ContextMenu
                  isOut={isOut}
                  canEdit={false}
                  canDelete
                  canForward
                  onEdit={() => {}}
                  onDelete={() => { setShowContextMenu(false); onDelete?.(message) }}
                  onForward={() => { setShowContextMenu(false); onForward?.(message) }}
                />
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
