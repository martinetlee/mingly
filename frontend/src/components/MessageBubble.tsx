import { useRef, useState, useCallback, useEffect } from 'react'
import { Reply, Forward, Pencil, Trash2, MoreVertical, Check, CheckCheck, Clock, Loader2, ShieldAlert, Flag, X, Languages, ChevronDown, Copy, User, MessageSquare, AtSign, Tag, ShieldBan } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import type { Message } from '../api/types'
import { authenticatedMediaUrl } from '../api/session'
import { downloadMedia, dismissScamFlag, checkMessagesForScam, translateMessage, getPrefs, openExternalUrl } from '../api/client'
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
  /** Whether the sender is a known flagged scammer */
  isFlaggedSender?: boolean
  onReportScam?: (msg: Message) => void
  /** Avatar context menu callbacks */
  onOpenProfile?: (senderId: number) => void
  onSendMessage?: (senderId: number) => void
  onMention?: (senderId: number, senderName: string) => void
  onRestrict?: (senderId: number) => void
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

/** Sanitize all href attributes and add title tooltip showing the URL on hover. */
function sanitizeHrefs(html: string): string {
  return html.replace(/<a\s([^>]*?)href="([^"]*)"([^>]*)>/g, (match, before, url, after) => {
    const safe = safeHref(url)
    // Show URL tooltip on hover for http links (not for internal /profile links or mentions)
    if (safe.startsWith('http') && !match.includes('data-mention')) {
      return `<a ${before}href="${safe}" title="${safe}"${after}>`
    }
    return `<a ${before}href="${safe}"${after}>`
  })
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
    if (!href) return
    e.preventDefault()
    if (href.startsWith('/profile/')) {
      navigate(href)
    } else if (href.startsWith('http://') || href.startsWith('https://')) {
      // Open external links in system browser via backend
      openExternalUrl(href)
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
function AvatarContextMenu({
  x,
  y,
  senderId,
  senderName,
  onClose,
  onOpenProfile,
  onSendMessage,
  onMention,
  onRestrict,
}: {
  x: number
  y: number
  senderId: number
  senderName: string
  onClose: () => void
  onOpenProfile?: (senderId: number) => void
  onSendMessage?: (senderId: number) => void
  onMention?: (senderId: number, senderName: string) => void
  onRestrict?: (senderId: number) => void
}) {
  const menuRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        onClose()
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [onClose])

  // Adjust position so the menu doesn't overflow viewport
  useEffect(() => {
    const el = menuRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    if (rect.bottom > window.innerHeight) {
      el.style.top = `${y - rect.height}px`
    }
    if (rect.right > window.innerWidth) {
      el.style.left = `${x - rect.width}px`
    }
  }, [x, y])

  const itemCls = "flex w-full items-center gap-2.5 px-3 py-2 text-xs text-slate-300 hover:bg-slate-700 transition-colors"

  return (
    <div
      ref={menuRef}
      className="fixed z-[100] min-w-[170px] rounded-xl border border-slate-600 bg-slate-800 shadow-2xl py-1.5 animate-in fade-in zoom-in-95 duration-100"
      style={{ left: x, top: y }}
    >
      {onOpenProfile && (
        <button onClick={() => { onClose(); onOpenProfile(senderId) }} className={itemCls}>
          <User size={14} /> Open Profile
        </button>
      )}
      {onSendMessage && (
        <button onClick={() => { onClose(); onSendMessage(senderId) }} className={itemCls}>
          <MessageSquare size={14} /> Send Message
        </button>
      )}
      {onMention && (
        <button onClick={() => { onClose(); onMention(senderId, senderName) }} className={itemCls}>
          <AtSign size={14} /> Mention
        </button>
      )}
      {onRestrict && (
        <>
          <div className="my-1 border-t border-slate-700" />
          <button onClick={() => { onClose(); onRestrict(senderId) }} className="flex w-full items-center gap-2.5 px-3 py-2 text-xs text-red-400 hover:bg-slate-700 transition-colors">
            <ShieldBan size={14} /> Restrict
          </button>
        </>
      )}
    </div>
  )
}

function ContextMenu({
  isOut,
  canEdit,
  canDelete,
  canForward,
  onEdit,
  onDelete,
  onForward,
  onCheckScam,
  onReportScam,
  onTranslate,
  onCopy,
}: {
  isOut: boolean
  canEdit: boolean
  canDelete: boolean
  canForward: boolean
  onEdit: () => void
  onDelete: () => void
  onForward: () => void
  onCheckScam?: () => void
  onReportScam?: () => void
  onTranslate?: () => void
  onCopy?: () => void
}) {
  const menuRef = useRef<HTMLDivElement>(null)
  const [flipDown, setFlipDown] = useState(false)

  useEffect(() => {
    const el = menuRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    // Flip down if the menu extends above the visible area (viewport + ~80px for header)
    if (rect.top < 80) {
      setFlipDown(true)
    }
  }, [])

  return (
    <div
      ref={menuRef}
      className={`absolute z-50 min-w-[140px] rounded-lg border border-slate-600 bg-slate-800 shadow-xl py-1 ${
        flipDown ? 'top-full mt-1' : 'bottom-full mb-1'
      } ${isOut ? 'right-0' : 'left-0'}`}
    >
      {onCopy && (
        <button
          onClick={onCopy}
          className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-700 transition-colors"
        >
          <Copy size={13} />
          Copy text
        </button>
      )}
      {onTranslate && (
        <button
          onClick={onTranslate}
          className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-700 transition-colors"
        >
          <Languages size={13} />
          Translate
        </button>
      )}
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
      {onCheckScam && (
        <button
          onClick={onCheckScam}
          className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-amber-400 hover:bg-slate-700 transition-colors"
        >
          <ShieldAlert size={13} />
          Check for scam
        </button>
      )}
      {onReportScam && (
        <button
          onClick={onReportScam}
          className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-red-400 hover:bg-slate-700 transition-colors"
        >
          <Flag size={13} />
          Report scam
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

export default function MessageBubble({ message, showSender, highlighted, onReply, onReact, onEdit, onDelete, onForward, showAvatar, isGroupChat, isFlaggedSender, onReportScam, onOpenProfile, onSendMessage, onMention, onRestrict }: Props) {
  const isOut = message.is_outgoing
  // Lazy photo download: if media_type is photo but media_url is missing, download it
  const [lazyPhotoUrl, setLazyPhotoUrl] = useState<string | null>(null)
  const [photoLoading, setPhotoLoading] = useState(false)
  useEffect(() => {
    if (message.media_type === 'photo' && !message.media_url && !lazyPhotoUrl && !photoLoading) {
      setPhotoLoading(true)
      downloadMedia(message.chat_id, message.tg_message_id)
        .then((res) => setLazyPhotoUrl(res.media_url))
        .catch(() => {})
        .finally(() => setPhotoLoading(false))
    }
  }, [message.media_type, message.media_url, message.chat_id, message.tg_message_id, lazyPhotoUrl, photoLoading])

  const photoUrl = message.media_url || lazyPhotoUrl
  const hasPhoto = !!photoUrl && message.media_type === 'photo'
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
  const [scamChecking, setScamChecking] = useState(false)
  const [scamCheckDone, setScamCheckDone] = useState<'safe' | 'scam' | 'error' | null>(null)
  const [scamError, setScamError] = useState<string | null>(null)
  const [scamInfoExpanded, setScamInfoExpanded] = useState(false)
  const [scamDismissed, setScamDismissed] = useState(false)
  const [translation, setTranslation] = useState<string | null>(null)
  const [translating, setTranslating] = useState(false)
  const [showAvatarMenu, setShowAvatarMenu] = useState(false)
  const [avatarMenuPos, setAvatarMenuPos] = useState<{ x: number; y: number }>({ x: 0, y: 0 })

  const handleCheckScam = useCallback(async () => {
    setScamChecking(true)
    setScamCheckDone(null)
    try {
      await checkMessagesForScam(message.chat_id, [message.tg_message_id])
      // Result arrives via WebSocket (scam_detected or scam_check_error).
      // Keep showing "Checking…" until then.
    } catch {
      setScamChecking(false)
      setScamCheckDone('error')
      setScamError('Failed to start scam check')
    }
  }, [message.chat_id, message.tg_message_id])

  const handleTranslate = useCallback(async () => {
    if (!message.text || translating) return
    setTranslating(true)
    try {
      const prefs = await getPrefs()
      const lang = prefs.translation_language || 'English'
      const result = await translateMessage(message.text, lang)
      setTranslation(result.translation)
    } catch {
      setTranslation('[Translation failed]')
    } finally {
      setTranslating(false)
    }
  }, [message.text, translating])

  // If scam_score updates (via parent re-render from WS event), reflect it
  useEffect(() => {
    if (scamChecking && message.scam_score != null) {
      setScamChecking(false)
      setScamCheckDone(message.scam_score > 0.5 ? 'scam' : 'safe')
    }
  }, [message.scam_score, scamChecking])

  // Listen for scam check results via WS
  useEffect(() => {
    if (!scamChecking) return
    const onError = (e: Event) => {
      const detail = (e as CustomEvent).detail
      if (detail.message_ids?.includes(message.tg_message_id)) {
        setScamChecking(false)
        setScamCheckDone('error')
        setScamError(detail.error || 'Scam check failed')
      }
    }
    const onSafe = (e: Event) => {
      const detail = (e as CustomEvent).detail
      if (detail.message_ids?.includes(message.tg_message_id)) {
        setScamChecking(false)
        setScamCheckDone('safe')
      }
    }
    window.addEventListener('scam_check_error', onError)
    window.addEventListener('scam_check_safe', onSafe)
    return () => {
      window.removeEventListener('scam_check_error', onError)
      window.removeEventListener('scam_check_safe', onSafe)
    }
  }, [scamChecking, message.tg_message_id])
  const pickerRef = useRef<HTMLDivElement>(null)
  const contextMenuRef = useRef<HTMLDivElement>(null)
  const reply = message.reply_to_preview

  const handleSelectEmoji = (emoji: string | null) => {
    setShowPicker(false)
    onReact?.(message, emoji)
  }

  const isScam = !scamDismissed && message.scam_score != null && message.scam_score > 0.5
  const showAvatarColumn = isGroupChat && !isOut

  return (
    <div
      className={`flex ${isOut ? 'justify-end' : 'justify-start'} mb-1 group/bubble ${highlighted ? 'rounded-lg ring-2 ring-amber-400/50 ring-offset-2 ring-offset-slate-900 transition-all duration-700' : ''}`}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => { setHovered(false); setShowPicker(false); setShowContextMenu(false); setShowAvatarMenu(false) }}
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
                  onCheckScam={() => { setShowContextMenu(false); handleCheckScam() }}
                  onReportScam={onReportScam ? () => { setShowContextMenu(false); onReportScam(message) } : undefined}
                  onTranslate={message.text ? () => { setShowContextMenu(false); handleTranslate() } : undefined}
                  onCopy={message.text ? () => { setShowContextMenu(false); navigator.clipboard.writeText(message.text!) } : undefined}
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
            <SenderAvatar
              senderId={message.sender_id}
              senderName={message.sender_name}
              size={28}
              onContextMenu={message.sender_id ? (e) => {
                e.preventDefault()
                e.stopPropagation()
                setAvatarMenuPos({ x: e.clientX, y: e.clientY })
                setShowAvatarMenu(true)
              } : undefined}
            />
          ) : (
            /* Transparent spacer to keep messages aligned */
            <div className="w-7" />
          )}
        </div>
      )}

      {/* Avatar right-click context menu (portal to body to avoid clipping) */}
      {showAvatarMenu && message.sender_id && (
        <AvatarContextMenu
          x={avatarMenuPos.x}
          y={avatarMenuPos.y}
          senderId={message.sender_id}
          senderName={message.sender_name || `ID: ${message.sender_id}`}
          onClose={() => setShowAvatarMenu(false)}
          onOpenProfile={onOpenProfile}
          onSendMessage={onSendMessage}
          onMention={onMention}
          onRestrict={isGroupChat ? onRestrict : undefined}
        />
      )}

      <div className={`max-w-[70%] ${isOut ? 'items-end' : 'items-start'} flex flex-col`}>
        {showSender && !isOut && message.sender_name && (
          message.sender_id ? (
            <Link
              to={`/profile/${message.sender_id}`}
              className={`mb-0.5 ml-1 text-xs font-medium transition-colors inline-flex items-center gap-1 ${
                isFlaggedSender ? 'text-red-400 hover:text-red-300' : 'text-purple-400 hover:text-purple-300'
              }`}
            >
              {isFlaggedSender && <ShieldAlert size={11} className="flex-shrink-0" />}
              {message.sender_name}
            </Link>
          ) : (
            <span className={`mb-0.5 ml-1 text-xs font-medium inline-flex items-center gap-1 ${
              isFlaggedSender ? 'text-red-400' : 'text-purple-400'
            }`}>
              {isFlaggedSender && <ShieldAlert size={11} className="flex-shrink-0" />}
              {message.sender_name}
            </span>
          )
        )}
        <div
          className={`rounded-2xl text-sm leading-relaxed overflow-hidden ${
            isScam
              ? isOut
                ? 'rounded-br-sm bg-red-900/80 text-white border border-red-500/40'
                : 'rounded-bl-sm bg-red-900/40 text-gray-100 border border-red-500/40'
              : isOut
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
            <p className="px-3 py-2 select-text">
              <FormattedText html={
                // If text_html still contains raw Markdown link syntax, it was mangled by
                // entity processing — fall back to client-side Markdown conversion
                message.text_html && !/\]\s*\(https?:\/\//.test(message.text_html)
                  ? message.text_html
                  : simpleMarkdownToHtml(message.text)
              } isOut={isOut} />
            </p>
          )}
          {/* Translation */}
          {translating && (
            <div className={`flex items-center gap-1.5 px-3 py-1.5 text-xs ${isOut ? 'text-indigo-300' : 'text-slate-400'}`}>
              <Loader2 size={12} className="animate-spin" />
              <span>Translating…</span>
            </div>
          )}
          {translation && !translating && (
            <div className={`px-3 py-1.5 border-t ${isOut ? 'border-indigo-500/30' : 'border-slate-600/50'}`}>
              <div className={`flex items-center gap-1 mb-0.5 text-[10px] ${isOut ? 'text-indigo-300/70' : 'text-slate-500'}`}>
                <Languages size={10} />
                <span>Translation</span>
                <button onClick={() => setTranslation(null)} className="ml-auto hover:text-slate-300"><X size={10} /></button>
              </div>
              <p className={`text-sm ${isOut ? 'text-indigo-100' : 'text-slate-200'}`}>{translation}</p>
            </div>
          )}
          {hasPhoto && (
            <>
              <img
                src={authenticatedMediaUrl(photoUrl!)}
                alt="photo"
                className={`block cursor-pointer object-cover ${isOut ? 'ml-auto' : 'mr-auto'} hover:opacity-90 transition-opacity`}
                style={{ maxWidth: 320, maxHeight: 320 }}
                onClick={() => setLightboxOpen(true)}
              />
              {lightboxOpen && (
                <ImageLightbox
                  src={authenticatedMediaUrl(photoUrl!)}
                  onClose={() => setLightboxOpen(false)}
                />
              )}
            </>
          )}
          {photoLoading && message.media_type === 'photo' && !hasPhoto && (
            <div className={`px-3 py-4 flex items-center gap-2 ${isOut ? 'text-indigo-300' : 'text-slate-400'}`}>
              <Loader2 className="w-4 h-4 animate-spin" />
              <span className="text-xs">Loading photo…</span>
            </div>
          )}
          {(hasVideo || hasVideoNote) && (
            <VideoPlayer
              chatTelegramId={message.chat_id}
              msgTelegramId={message.tg_message_id}
              mediaUrl={message.media_url || null}
              isVideoNote={hasVideoNote}
              isOut={isOut}
            />
          )}
          {(hasVoice || hasAudio) && (
            <AudioPlayer
              chatTelegramId={message.chat_id}
              msgTelegramId={message.tg_message_id}
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
          {message.media_type && !hasPhoto && !photoLoading && !hasPlayableMedia && !hasGif && !hasDocument && !message.text && (
            <span className={`px-3 py-2 block italic ${isOut ? 'text-indigo-300' : 'text-slate-400'}`}>
              [{message.media_type}]
            </span>
          )}
          {/* Scam check in progress */}
          {scamChecking && (
            <div className="flex items-center gap-1.5 px-3 py-1.5 border-t border-amber-500/30 bg-amber-500/10 text-amber-300 text-xs">
              <Loader2 size={13} className="animate-spin flex-shrink-0" />
              <span>Checking for scam…</span>
            </div>
          )}
          {/* Scam check result: safe */}
          {!scamChecking && scamCheckDone === 'safe' && !isScam && (
            <div className="flex items-center gap-1.5 px-3 py-1.5 border-t border-emerald-500/30 bg-emerald-500/10 text-emerald-300 text-xs">
              <Check size={13} className="flex-shrink-0" />
              <span>No scam detected</span>
            </div>
          )}
          {/* Scam check error */}
          {!scamChecking && scamCheckDone === 'error' && (
            <div className="flex items-center gap-1.5 px-3 py-1.5 border-t border-orange-500/30 bg-orange-500/10 text-orange-300 text-xs">
              <ShieldAlert size={13} className="flex-shrink-0" />
              <span className="flex-1">{scamError || 'Scam check failed'}</span>
            </div>
          )}
          {/* Scam warning banner */}
          {isScam && (
            <div className="border-t border-red-500/30 bg-red-500/20">
              <button
                onClick={(e) => { e.stopPropagation(); setScamInfoExpanded((v) => !v) }}
                className="flex w-full items-center gap-1.5 px-3 py-1.5 text-red-300 text-xs hover:bg-red-500/10 transition-colors"
              >
                <ShieldAlert size={13} className="flex-shrink-0" />
                <span className="flex-1 text-left">Potential scam detected</span>
                <ChevronDown size={12} className={`flex-shrink-0 transition-transform ${scamInfoExpanded ? 'rotate-180' : ''}`} />
              </button>
              {scamInfoExpanded && (
                <div className="px-3 pb-2 text-xs text-red-300/80 space-y-1.5">
                  <p>{message.scam_reason || 'This message has been flagged as a potential scam.'}</p>
                  <button
                    onClick={(e) => {
                      e.stopPropagation()
                      if (message.sender_id) {
                        setScamDismissed(true)
                        dismissScamFlag(message.sender_id).catch(() => setScamDismissed(false))
                      }
                    }}
                    className="text-red-400 hover:text-red-200 underline"
                  >
                    Dismiss flag
                  </button>
                </div>
              )}
            </div>
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
                  onCheckScam={() => { setShowContextMenu(false); handleCheckScam() }}
                  onReportScam={onReportScam ? () => { setShowContextMenu(false); onReportScam(message) } : undefined}
                  onTranslate={message.text ? () => { setShowContextMenu(false); handleTranslate() } : undefined}
                  onCopy={message.text ? () => { setShowContextMenu(false); navigator.clipboard.writeText(message.text!) } : undefined}
                />
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
