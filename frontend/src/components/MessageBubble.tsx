import { memo, useRef, useState, useCallback, useEffect, useMemo } from 'react'
import { Reply, Forward, Pencil, Trash2, MoreVertical, Check, CheckCheck, Loader2, ShieldAlert, Flag, X, Languages, ChevronDown, Copy, User, MessageSquare, AtSign, ShieldBan, Pin, PinOff } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import type { Message } from '../api/types'
import { translateMessage, getPrefs, openExternalUrl } from '../api/client'
import { reportError, toast } from '../lib/toast'
import { fmtTime } from '../utils/time'
import { dismissScamCheck, startScamCheck, useScamCheck } from '../pages/chats/scamChecks'
import { useFeatures } from '../context/FeaturesContext'
import SenderAvatar from './SenderAvatar'
import MediaView, { RENDERED_MEDIA } from './MediaView'
import MediaAlbumGrid from './MediaAlbumGrid'

export type ScamVerdictScope = 'message' | 'sender'

interface Props {
  /** The message (for an album: the item carrying the caption, else the first) */
  message: Message
  /** All items of a media album, in order; the bubble acts on all of them */
  album?: Message[]
  showSender: boolean
  highlighted?: boolean
  onReply?: (msg: Message) => void
  onReact?: (msg: Message, emoji: string | null) => void
  onEdit?: (msg: Message) => void
  /** `album` is passed for album bubbles so delete/forward cover every item */
  onDelete?: (msg: Message, album?: Message[]) => void
  onForward?: (msg: Message, album?: Message[]) => void
  onPin?: (msg: Message) => void
  /** Clicking a reply quote */
  onJumpToMessage?: (tgId: number) => void
  /** Show avatar on this message (first in a sender group) */
  showAvatar?: boolean
  /** Whether this is a group/channel chat (controls avatar indent) */
  isGroupChat?: boolean
  /** Whether the sender is a known flagged scammer */
  isFlaggedSender?: boolean
  onReportScam?: (msg: Message) => void
  /** "Not a scam" on this message / dismiss the sender — the parent calls the verdict API */
  onScamVerdict?: (msg: Message, scope: ScamVerdictScope) => void
  /** Avatar context menu callbacks */
  onOpenProfile?: (senderId: number) => void
  onSendMessage?: (senderId: number) => void
  onMention?: (senderId: number, senderName: string) => void
  onRestrict?: (senderId: number, chatId: number) => void
}

// Common quick-reaction emojis (mirrors Telegram's default set)
const QUICK_EMOJIS = ['👍', '❤️', '🔥', '🥰', '👏', '😁', '🤔', '🤯', '😱', '🙏', '👌', '🎉']

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

/**
 * Plain stored text → HTML. `message.text` is the raw Telegram text (no
 * markdown markers), so only escape, linkify bare URLs and keep newlines.
 */
export function plainTextToHtml(text: string): string {
  let s = escapeHtml(text)
  s = s.replace(/https?:\/\/[^\s<"]+/g, (url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`)
  return s.replace(/\n/g, '<br>')
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

const FormattedText = memo(function FormattedText({ text, html, isOut }: { text: string; html: string | null; isOut: boolean }) {
  const navigate = useNavigate()
  // Sanitising runs several regexes — only when the content changes
  const safe = useMemo(() => sanitizeHrefs(processMentions(html ?? plainTextToHtml(text))), [text, html])

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
      dangerouslySetInnerHTML={{ __html: safe }}
      onClick={handleClick}
    />
  )
})

/** Delivery state indicator for outgoing messages */
function DeliveryIndicator({ state }: { state?: string }) {
  switch (state) {
    case 'sending':
      return <Loader2 size={12} className="text-indigo-300/60 animate-spin" />
    case 'delivered':
      return <CheckCheck size={12} className="text-indigo-300/60" />
    case 'read':
      return <CheckCheck size={12} className="text-sky-400" />
    default:
      return <Check size={12} className="text-indigo-300/60" />
  }
}

function useDismissOnOutsideClick(ref: React.RefObject<HTMLElement>, onClose: () => void) {
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [ref, onClose])
}

/** Floating emoji picker */
function EmojiPicker({ isOut, myReaction, onSelect }: {
  isOut: boolean
  myReaction: string | null | undefined
  onSelect: (emoji: string | null) => void
}) {
  return (
    <div className={`absolute bottom-full mb-1 z-50 flex gap-0.5 rounded-xl border border-slate-600 bg-slate-800 shadow-xl p-1 ${isOut ? 'right-0' : 'left-0'}`}>
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

function AvatarContextMenu({ x, y, senderId, senderName, onClose, onOpenProfile, onSendMessage, onMention, onRestrict }: {
  x: number
  y: number
  senderId: number
  senderName: string
  onClose: () => void
  onOpenProfile?: (senderId: number) => void
  onSendMessage?: (senderId: number) => void
  onMention?: (senderId: number, senderName: string) => void
  onRestrict?: () => void
}) {
  const menuRef = useRef<HTMLDivElement>(null)
  useDismissOnOutsideClick(menuRef, onClose)

  // Keep the menu inside the viewport
  useEffect(() => {
    const el = menuRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    if (rect.bottom > window.innerHeight) el.style.top = `${y - rect.height}px`
    if (rect.right > window.innerWidth) el.style.left = `${x - rect.width}px`
  }, [x, y])

  const itemCls = 'flex w-full items-center gap-2.5 px-3 py-2 text-xs text-slate-300 hover:bg-slate-700 transition-colors'
  return (
    <div ref={menuRef} className="fixed z-[100] min-w-[170px] rounded-xl border border-slate-600 bg-slate-800 shadow-2xl py-1.5" style={{ left: x, top: y }}>
      {onOpenProfile && (
        <button onClick={() => { onClose(); onOpenProfile(senderId) }} className={itemCls}><User size={14} /> Open Profile</button>
      )}
      {onSendMessage && (
        <button onClick={() => { onClose(); onSendMessage(senderId) }} className={itemCls}><MessageSquare size={14} /> Send Message</button>
      )}
      {onMention && (
        <button onClick={() => { onClose(); onMention(senderId, senderName) }} className={itemCls}><AtSign size={14} /> Mention</button>
      )}
      {onRestrict && (
        <>
          <div className="my-1 border-t border-slate-700" />
          <button onClick={() => { onClose(); onRestrict() }} className="flex w-full items-center gap-2.5 px-3 py-2 text-xs text-red-400 hover:bg-slate-700 transition-colors">
            <ShieldBan size={14} /> Restrict
          </button>
        </>
      )}
    </div>
  )
}

interface MenuItem {
  key: string
  label: string
  icon: React.ReactNode
  onClick: () => void
  tone?: 'danger' | 'warn'
}

function ActionMenu({ isOut, items, onClose }: { isOut: boolean; items: MenuItem[]; onClose: () => void }) {
  const menuRef = useRef<HTMLDivElement>(null)
  const [flipDown, setFlipDown] = useState(false)
  useDismissOnOutsideClick(menuRef, onClose)

  useEffect(() => {
    const el = menuRef.current
    // Flip down if the menu would extend above the visible area (header ~80px)
    if (el && el.getBoundingClientRect().top < 80) setFlipDown(true)
  }, [])

  return (
    <div
      ref={menuRef}
      role="menu"
      className={`absolute z-50 min-w-[150px] rounded-lg border border-slate-600 bg-slate-800 shadow-xl py-1 ${
        flipDown ? 'top-full mt-1' : 'bottom-full mb-1'
      } ${isOut ? 'right-0' : 'left-0'}`}
    >
      {items.map((it) => (
        <button
          key={it.key}
          role="menuitem"
          onClick={() => { onClose(); it.onClick() }}
          className={`flex w-full items-center gap-2 px-3 py-1.5 text-xs hover:bg-slate-700 transition-colors ${
            it.tone === 'danger' ? 'text-red-400' : it.tone === 'warn' ? 'text-amber-400' : 'text-slate-300'
          }`}
        >
          {it.icon}
          {it.label}
        </button>
      ))}
    </div>
  )
}

/** Scam status for a message: server verdict + any user-initiated check in progress. */
function ScamSection({ message, onScamVerdict }: { message: Message; onScamVerdict?: (msg: Message, scope: ScamVerdictScope) => void }) {
  const check = useScamCheck(message.chat_id, message.tg_message_id)
  const [expanded, setExpanded] = useState(false)
  // THE verdict comes from the server; never re-derived from scam_score
  const isScam = !!message.effective_is_scam

  return (
    <>
      {check?.status === 'checking' && (
        <div className="flex items-center gap-1.5 px-3 py-1.5 border-t border-amber-500/30 bg-amber-500/10 text-amber-300 text-xs">
          <Loader2 size={13} className="animate-spin flex-shrink-0" />
          <span>Checking for scam…</span>
        </div>
      )}
      {check?.status === 'done' && !isScam && (
        <div className="flex items-center gap-1.5 px-3 py-1.5 border-t border-emerald-500/30 bg-emerald-500/10 text-emerald-300 text-xs">
          <Check size={13} className="flex-shrink-0" />
          <span className="flex-1">No scam detected</span>
          <button onClick={() => dismissScamCheck(message.chat_id, message.tg_message_id)} aria-label="Dismiss" className="hover:text-emerald-100"><X size={11} /></button>
        </div>
      )}
      {check?.status === 'error' && (
        <div className="flex items-center gap-1.5 px-3 py-1.5 border-t border-orange-500/30 bg-orange-500/10 text-orange-300 text-xs">
          <ShieldAlert size={13} className="flex-shrink-0" />
          <span className="flex-1">{check.error || 'Scam check failed'}</span>
          <button onClick={() => void startScamCheck(message.chat_id, message.tg_message_id)} className="underline hover:text-orange-100">Retry</button>
          <button onClick={() => dismissScamCheck(message.chat_id, message.tg_message_id)} aria-label="Dismiss" className="hover:text-orange-100"><X size={11} /></button>
        </div>
      )}
      {isScam && (
        <div className="border-t border-red-500/30 bg-red-500/20">
          <button
            onClick={(e) => { e.stopPropagation(); setExpanded((v) => !v) }}
            className="flex w-full items-center gap-1.5 px-3 py-1.5 text-red-300 text-xs hover:bg-red-500/10 transition-colors"
          >
            <ShieldAlert size={13} className="flex-shrink-0" />
            <span className="flex-1 text-left">{message.scam_manual_verdict === 'scam' ? 'Manually marked as scam' : 'Potential scam detected'}</span>
            <ChevronDown size={12} className={`flex-shrink-0 transition-transform ${expanded ? 'rotate-180' : ''}`} />
          </button>
          {expanded && (
            <div className="px-3 pb-2 text-xs text-red-300/80 space-y-1.5">
              <p>{message.scam_reason || 'This message has been flagged as a potential scam.'}</p>
              {onScamVerdict && (
                <div className="flex gap-3">
                  <button onClick={(e) => { e.stopPropagation(); onScamVerdict(message, 'message') }} className="text-red-400 hover:text-red-200 underline">
                    Not a scam
                  </button>
                  {message.sender_id != null && !message.is_outgoing && (
                    <button onClick={(e) => { e.stopPropagation(); onScamVerdict(message, 'sender') }} className="text-red-400 hover:text-red-200 underline">
                      Dismiss sender
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </>
  )
}

function MessageBubble({
  message, album, showSender, highlighted, onReply, onReact, onEdit, onDelete, onForward, onPin, onJumpToMessage,
  showAvatar, isGroupChat, isFlaggedSender: flaggedProp, onReportScam: reportProp, onScamVerdict: verdictProp,
  onOpenProfile, onSendMessage, onMention, onRestrict,
}: Props) {
  // Every scam surface (banner, check, report, flagged-sender marker) follows the Shield feature
  const { shield } = useFeatures()
  const isFlaggedSender = shield && !!flaggedProp
  const onReportScam = shield ? reportProp : undefined
  const onScamVerdict = shield ? verdictProp : undefined
  const isOut = message.is_outgoing
  const [hovered, setHovered] = useState(false)
  const [showPicker, setShowPicker] = useState(false)
  const [showMenu, setShowMenu] = useState(false)
  const [translation, setTranslation] = useState<string | null>(null)
  const [translating, setTranslating] = useState(false)
  const [avatarMenu, setAvatarMenu] = useState<{ x: number; y: number } | null>(null)
  const closeMenu = useCallback(() => setShowMenu(false), [])
  const closePicker = useCallback(() => setShowPicker(false), [])
  const pickerRef = useRef<HTMLDivElement>(null)
  useDismissOnOutsideClick(pickerRef, closePicker)

  const handleTranslate = useCallback(async () => {
    if (!message.text || translating) return
    setTranslating(true)
    try {
      const prefs = await getPrefs()
      const result = await translateMessage(message.text, prefs.translation_language || 'English')
      setTranslation(result.translation)
    } catch (e) {
      reportError(e, 'Translation failed')
    } finally {
      setTranslating(false)
    }
  }, [message.text, translating])

  const reply = message.reply_to_preview
  const isScam = !!message.effective_is_scam
  const showAvatarColumn = isGroupChat && !isOut
  const hasMenu = !!(onEdit || onDelete || onForward || onPin || onReportScam)

  const menuItems: MenuItem[] = []
  if (message.text) {
    menuItems.push({
      key: 'copy', label: 'Copy text', icon: <Copy size={13} />,
      onClick: () => { navigator.clipboard.writeText(message.text!).then(() => toast.success('Copied'), (e) => reportError(e, 'Copy failed')) },
    })
    menuItems.push({ key: 'translate', label: 'Translate', icon: <Languages size={13} />, onClick: () => void handleTranslate() })
  }
  if (onForward) menuItems.push({ key: 'forward', label: 'Forward', icon: <Forward size={13} />, onClick: () => onForward(message, album) })
  if (onPin) {
    menuItems.push({
      key: 'pin', label: message.is_pinned ? 'Unpin' : 'Pin', icon: message.is_pinned ? <PinOff size={13} /> : <Pin size={13} />,
      onClick: () => onPin(message),
    })
  }
  if (onEdit && isOut && message.text && !album) menuItems.push({ key: 'edit', label: 'Edit', icon: <Pencil size={13} />, onClick: () => onEdit(message) })
  menuItems.push({
    key: 'scam', label: 'Check for scam', icon: <ShieldAlert size={13} />, tone: 'warn',
    onClick: () => void startScamCheck(message.chat_id, message.tg_message_id),
  })
  if (onReportScam) menuItems.push({ key: 'report', label: 'Report scam', icon: <Flag size={13} />, tone: 'danger', onClick: () => onReportScam(message) })
  if (onDelete) menuItems.push({ key: 'delete', label: 'Delete', icon: <Trash2 size={13} />, tone: 'danger', onClick: () => onDelete(message, album) })

  const actions = (
    <div className={`${isOut ? 'mr-1' : 'ml-1'} flex items-center gap-0.5 self-center transition-opacity ${hovered || showMenu || showPicker ? 'opacity-100' : 'opacity-0'}`}>
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
            <EmojiPicker
              isOut={isOut}
              myReaction={message.my_reaction}
              onSelect={(emoji) => { setShowPicker(false); onReact(message, emoji) }}
            />
          )}
        </div>
      )}
      {onReply && (
        <button onClick={() => onReply(message)} className="rounded-full p-1 text-slate-500 hover:bg-slate-700 hover:text-slate-300 transition-all" title="Reply">
          <Reply size={14} />
        </button>
      )}
      {hasMenu && (
        <div className="relative">
          <button
            onClick={() => setShowMenu((v) => !v)}
            className="rounded-full p-1 text-slate-500 hover:bg-slate-700 hover:text-slate-300 transition-all"
            title="More actions"
            aria-label="More actions"
            aria-haspopup="menu"
          >
            <MoreVertical size={14} />
          </button>
          {showMenu && <ActionMenu isOut={isOut} items={menuItems} onClose={closeMenu} />}
        </div>
      )}
    </div>
  )

  const senderLabelCls = isFlaggedSender ? 'text-red-400 hover:text-red-300' : 'text-purple-400 hover:text-purple-300'
  const mediaOnly = !album && message.media_type && !RENDERED_MEDIA.has(message.media_type) && !message.text

  return (
    <div
      className={`flex ${isOut ? 'justify-end' : 'justify-start'} mb-1 group/bubble ${highlighted ? 'rounded-lg ring-2 ring-amber-400/50 ring-offset-2 ring-offset-slate-900 transition-all duration-700' : ''}`}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onContextMenu={(e) => {
        if (!hasMenu) return
        e.preventDefault()
        setShowMenu(true)
      }}
    >
      {isOut && actions}

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
                setAvatarMenu({ x: e.clientX, y: e.clientY })
              } : undefined}
            />
          ) : (
            <div className="w-7" />
          )}
        </div>
      )}

      {avatarMenu && message.sender_id && (
        <AvatarContextMenu
          x={avatarMenu.x}
          y={avatarMenu.y}
          senderId={message.sender_id}
          senderName={message.sender_name || `ID: ${message.sender_id}`}
          onClose={() => setAvatarMenu(null)}
          onOpenProfile={onOpenProfile}
          onSendMessage={onSendMessage}
          onMention={onMention}
          onRestrict={isGroupChat && onRestrict ? () => onRestrict(message.sender_id!, message.chat_id) : undefined}
        />
      )}

      <div className={`max-w-[70%] ${isOut ? 'items-end' : 'items-start'} flex flex-col`}>
        {showSender && !isOut && message.sender_name && (
          message.sender_id ? (
            <Link to={`/profile/${message.sender_id}`} className={`mb-0.5 ml-1 text-xs font-medium transition-colors inline-flex items-center gap-1 ${senderLabelCls}`}>
              {isFlaggedSender && <ShieldAlert size={11} className="flex-shrink-0" />}
              {message.sender_name}
            </Link>
          ) : (
            <span className={`mb-0.5 ml-1 text-xs font-medium inline-flex items-center gap-1 ${isFlaggedSender ? 'text-red-400' : 'text-purple-400'}`}>
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
          {reply && (
            <button
              type="button"
              onClick={message.reply_to_tg_message_id != null && onJumpToMessage ? () => onJumpToMessage(message.reply_to_tg_message_id!) : undefined}
              disabled={message.reply_to_tg_message_id == null || !onJumpToMessage}
              title={onJumpToMessage ? 'Go to the original message' : undefined}
              className={`mx-2 mt-2 flex w-[calc(100%-1rem)] rounded-lg border-l-2 px-2 py-1 text-left enabled:cursor-pointer enabled:hover:brightness-110 ${
                isOut ? 'border-indigo-300/60 bg-indigo-700/60' : 'border-purple-400/60 bg-slate-600/60'
              }`}
            >
              <div className="min-w-0">
                <p className={`text-[11px] font-semibold truncate ${isOut ? 'text-indigo-200' : 'text-purple-300'}`}>
                  {reply.sender_name ?? 'Unknown'}
                </p>
                <p className={`truncate text-[11px] ${isOut ? 'text-indigo-300' : 'text-slate-400'}`}>
                  {reply.text ? reply.text.slice(0, 80) : reply.media_type ? `[${reply.media_type}]` : ''}
                </p>
              </div>
            </button>
          )}
          {album
            ? <MediaAlbumGrid messages={album} isOut={isOut} />
            : message.media_type && <MediaView message={message} isOut={isOut} />}
          {message.text && (
            <p className="px-3 py-2 select-text">
              <FormattedText text={message.text} html={message.text_html} isOut={isOut} />
            </p>
          )}
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
                <button onClick={() => setTranslation(null)} className="ml-auto hover:text-slate-300" aria-label="Close translation"><X size={10} /></button>
              </div>
              <p className={`text-sm ${isOut ? 'text-indigo-100' : 'text-slate-200'}`}>{translation}</p>
            </div>
          )}
          {mediaOnly && (
            <span className={`px-3 py-2 block italic ${isOut ? 'text-indigo-300' : 'text-slate-400'}`}>[{message.media_type}]</span>
          )}
          {shield && <ScamSection message={message} onScamVerdict={onScamVerdict} />}
        </div>

        {(message.reactions && message.reactions.length > 0) || message.my_reaction ? (
          <div className={`flex flex-wrap gap-1 mt-0.5 ${isOut ? 'mr-1' : 'ml-1'}`}>
            {(message.reactions ?? []).map((r) => (
              <button
                key={r.emoji}
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
            {/* my_reaction not yet reflected in the aggregate */}
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
          {message.is_pinned && <Pin size={9} className="text-indigo-400" aria-label="Pinned" />}
          {message.is_edited && <span className="italic text-slate-500/70">edited</span>}
          {fmtTime(message.date)}
          {isOut && <DeliveryIndicator state={message.delivery_state} />}
        </span>
      </div>

      {!isOut && actions}
    </div>
  )
}

function sameAlbum(a?: Message[], b?: Message[]): boolean {
  if (a === b) return true
  if (!a || !b || a.length !== b.length) return false
  return a.every((m, i) => m === b[i])
}

/** Re-renders only when its own message (or album items) or a prop actually changes. */
export default memo(MessageBubble, (prev, next) => {
  for (const k of Object.keys(next) as (keyof Props)[]) {
    if (k === 'album') continue
    if (prev[k] !== next[k]) return false
  }
  return Object.keys(prev).length === Object.keys(next).length && sameAlbum(prev.album, next.album)
})
