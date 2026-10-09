import { memo, useRef, useState, useCallback, useEffect, useMemo } from 'react'
import { Reply, Forward, Pencil, Trash2, MoreVertical, Check, CheckCheck, Loader2, ShieldAlert, Flag, X, Languages, ChevronDown, Copy, User, MessageSquare, AtSign, ShieldBan, Pin, PinOff, BarChart3, MapPin, Contact as ContactIcon } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import { personUrl } from '../lib/routes'
import type { Attachment, ChannelName, ConversationKind, Message, MessageSender } from '../api/types'
import { translateMessage, getPrefs, openExternalUrl } from '../api/client'
import { reportError, toast } from '../lib/toast'
import { fmtTime } from '../utils/time'
import { dismissScamCheck, startScamCheck, useScamCheck } from '../pages/chats/scamChecks'
import { useFeatures } from '../context/FeaturesContext'
import SenderAvatar from './SenderAvatar'
import MediaView, { RENDERED_MEDIA } from './MediaView'
import MediaAlbumGrid from './MediaAlbumGrid'
import { mentionUrl } from '../lib/mentions'
import EmailMessageView from './EmailMessageView'

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
  /** Clicking a reply quote (stored target) */
  onJumpToMessage?: (messageId: number) => void
  /** Clicking a reply quote whose target isn't stored (fetched from the channel by its own id) */
  onJumpToExternal?: (externalId: string) => void
  /** The conversation's channel (channel-specific text handling such as Telegram mentions) */
  channel?: ChannelName | null
  /** Email threads render each message as an email (EmailMessageView) */
  conversationKind?: ConversationKind
  /** Show avatar on this message (first in a sender group) */
  showAvatar?: boolean
  /** Whether this is a group/channel chat (controls avatar indent) */
  isGroupChat?: boolean
  /** Whether the sender is a known flagged scammer */
  isFlaggedSender?: boolean
  onReportScam?: (msg: Message) => void
  /** "Not a scam" on this message / dismiss the sender — the parent calls the verdict API */
  onScamVerdict?: (msg: Message, scope: ScamVerdictScope) => void
  /** Avatar context menu callbacks (the parent passes only what the conversation supports) */
  onOpenProfile?: (sender: MessageSender) => void
  onSendMessage?: (sender: MessageSender) => void
  onMention?: (sender: MessageSender) => void
  onRestrict?: (sender: MessageSender, conversationId: number) => void
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
    // Show URL tooltip on hover for http links (not for internal links or mentions)
    if (safe.startsWith('http') && !match.includes('data-mention')) {
      return `<a ${before}href="${safe}" title="${safe}"${after}>`
    }
    return `<a ${before}href="${safe}"${after}>`
  })
}

/**
 * Plain stored text → HTML. `message.text` is the raw channel text (no
 * markdown markers), so only escape, linkify bare URLs and keep newlines.
 */
export function plainTextToHtml(text: string): string {
  let s = escapeHtml(text)
  s = s.replace(/https?:\/\/[^\s<"]+/g, (url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`)
  return s.replace(/\n/g, '<br>')
}

/**
 * Telegram mentions link to /mention/telegram/<user id | @username> (the
 * backend already renders text_html mentions that way; this covers tg://user
 * links from older rows and plain @username text). pages/Mention resolves them.
 */
function processTelegramMentions(html: string): string {
  let s = html.replace(
    /href="tg:\/\/user\?id=(\d+)"/g,
    (_m, id: string) => `href="${mentionUrl('telegram', id)}" data-mention="true"`,
  )
  // Wrap plain @username text that isn't already inside an <a> tag
  s = s.replace(
    /(?<!["\/\w])@([A-Za-z]\w{3,})/g,
    (_m, name: string) => `<a href="${mentionUrl('telegram', `@${name}`)}" data-mention="true" class="mention-link">@${name}</a>`,
  )
  return s
}

const FormattedText = memo(function FormattedText({ text, html, isOut, channel }: { text: string; html: string | null; isOut: boolean; channel?: ChannelName | null }) {
  const navigate = useNavigate()
  // Sanitising runs several regexes — only when the content changes
  const safe = useMemo(() => {
    const raw = html ?? plainTextToHtml(text)
    return sanitizeHrefs(channel === 'telegram' ? processTelegramMentions(raw) : raw)
  }, [text, html, channel])

  const handleClick = useCallback((e: React.MouseEvent<HTMLSpanElement>) => {
    const anchor = (e.target as HTMLElement).closest('a')
    if (!anchor) return
    const href = anchor.getAttribute('href')
    if (!href) return
    e.preventDefault()
    if (href.startsWith('/mention/')) {
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

type Meta = Record<string, unknown>
const str = (v: unknown): string | null => (typeof v === 'string' && v.trim() !== '' ? v : null)
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

export type MetaKind = 'poll' | 'geo' | 'contact'

/**
 * The structured part of a message's `meta` we render: `{poll}`, `{venue}`,
 * `{location}` or `{contact}` (backend convert.media_meta). Other keys
 * (forward, service, dice, game…) fall back to the stored text.
 */
export function structuredMeta(meta: Meta | null | undefined): { kind: MetaKind; data: Meta } | null {
  if (!meta || typeof meta !== 'object') return null
  const obj = (v: unknown): Meta | null => (v && typeof v === 'object' && !Array.isArray(v) ? v as Meta : null)
  const poll = obj(meta.poll)
  if (poll && str(poll.question)) return { kind: 'poll', data: poll }
  const venue = obj(meta.venue)
  if (venue) return { kind: 'geo', data: venue }
  const location = obj(meta.location)
  if (location) return { kind: 'geo', data: location }
  const contact = obj(meta.contact)
  if (contact) return { kind: 'contact', data: contact }
  return null
}

interface PollOption { text: string; voters: number | null }

function pollOptions(meta: Meta): PollOption[] {
  const list = Array.isArray(meta.options) ? meta.options : []
  return list.flatMap((o): PollOption[] => {
    if (typeof o === 'string') return [{ text: o, voters: null }]
    if (o && typeof o === 'object') {
      const r = o as Meta
      const text = str(r.text)
      return text ? [{ text, voters: num(r.voters) }] : []
    }
    return []
  })
}

/** Compact readable render of a poll, location/venue or shared contact */
export function MetaContent({ meta: raw, isOut }: { meta: Meta; isOut: boolean }) {
  const structured = structuredMeta(raw)
  if (!structured) return null
  const { kind, data: meta } = structured
  const muted = isOut ? 'text-indigo-200' : 'text-slate-400'
  if (kind === 'poll') {
    const options = pollOptions(meta)
    const total = num(meta.total_voters)
    const closed = meta.closed === true
    return (
      <div className="px-3 py-2" data-testid="meta-poll">
        <p className={`mb-1 flex items-center gap-1 text-[11px] ${muted}`}>
          <BarChart3 size={11} /> {meta.quiz === true ? 'Quiz' : 'Poll'}{closed ? ' · closed' : ''}
        </p>
        <p className="font-medium">{str(meta.question) ?? 'Poll'}</p>
        {options.length > 0 && (
          <ul className="mt-1 space-y-0.5">
            {options.map((o, i) => (
              <li key={i} className="flex items-center gap-2 text-[13px]">
                <span className={`h-1.5 w-1.5 flex-shrink-0 rounded-full ${isOut ? 'bg-indigo-200' : 'bg-slate-400'}`} />
                <span className="flex-1">{o.text}</span>
                {o.voters != null && <span className={`text-[11px] ${muted}`}>{o.voters}</span>}
              </li>
            ))}
          </ul>
        )}
        {total != null && <p className={`mt-1 text-[11px] ${muted}`}>{total} vote{total === 1 ? '' : 's'}</p>}
      </div>
    )
  }
  if (kind === 'geo') {
    const lat = num(meta.lat)
    const long = num(meta.long)
    const title = str(meta.title)
    const address = str(meta.address)
    const coords = lat != null && long != null ? `${lat.toFixed(5)}, ${long.toFixed(5)}` : null
    const live = num(meta.live_period) != null
    return (
      <div className="flex items-start gap-2 px-3 py-2" data-testid="meta-geo">
        <MapPin size={15} className={`mt-0.5 flex-shrink-0 ${muted}`} />
        <div className="min-w-0">
          <p className="font-medium">{title ?? (live ? 'Live location' : 'Location')}</p>
          {address && <p className={`text-[12px] ${muted}`}>{address}</p>}
          {coords && (
            <a
              href={`https://www.openstreetmap.org/?mlat=${lat}&mlon=${long}#map=16/${lat}/${long}`}
              onClick={(e) => { e.preventDefault(); openExternalUrl(`https://www.openstreetmap.org/?mlat=${lat}&mlon=${long}#map=16/${lat}/${long}`) }}
              className={`text-[11px] underline underline-offset-2 ${muted}`}
            >
              {coords}
            </a>
          )}
        </div>
      </div>
    )
  }
  if (kind === 'contact') {
    const name = [str(meta.first_name), str(meta.last_name)].filter(Boolean).join(' ') || 'Contact'
    const phone = str(meta.phone)
    const userId = num(meta.user_id)
    return (
      <div className="flex items-start gap-2 px-3 py-2" data-testid="meta-contact">
        <ContactIcon size={15} className={`mt-0.5 flex-shrink-0 ${muted}`} />
        <div className="min-w-0">
          {userId ? <SharedContactLink userId={userId} name={name} /> : <p className="font-medium">{name}</p>}
          {phone && <p className={`text-[12px] ${muted}`}>{phone.startsWith('+') ? phone : `+${phone}`}</p>}
        </div>
      </div>
    )
  }
  return null
}

/** A shared Telegram contact card: links through the mention route (resolves the user to a person) */
function SharedContactLink({ userId, name }: { userId: number; name: string }) {
  return <Link to={mentionUrl('telegram', String(userId))} className="font-medium hover:underline">{name}</Link>
}

/** Where a reply quote / pin line points: a stored message id, or the channel's id of an unstored one */
function replyTarget(message: Message): { messageId: number } | { externalId: string } | null {
  if (message.reply_to_message_id != null) return { messageId: message.reply_to_message_id }
  if (message.reply_to_external_id) return { externalId: message.reply_to_external_id }
  return null
}

/**
 * A service message (joined, left, pinned, call, title change…): a centered
 * system line with no bubble and no actions. `text` is the backend's readable
 * description.
 */
export const ServiceMessage = memo(function ServiceMessage({ message, highlighted, onJumpToMessage, onJumpToExternal }: {
  message: Message
  highlighted?: boolean
  /** A pin line jumps to the pinned message (its reply target) */
  onJumpToMessage?: (messageId: number) => void
  onJumpToExternal?: (externalId: string) => void
}) {
  const service = message.meta?.service as { action?: string } | undefined
  const pinned = service?.action === 'pin_message' ? replyTarget(message) : null
  const jump = pinned && 'messageId' in pinned
    ? (onJumpToMessage ? () => onJumpToMessage(pinned.messageId) : null)
    : pinned && onJumpToExternal ? () => onJumpToExternal(pinned.externalId) : null
  const cls = `max-w-[85%] rounded-full bg-slate-800/80 px-3 py-1 text-center text-[11px] text-slate-400 ${
    highlighted ? 'ring-2 ring-amber-400/50 transition-all duration-700' : ''
  }`
  const content = (
    <>
      {message.text || 'Service message'}
      <span className="ml-1.5 text-[10px] text-slate-500">{fmtTime(message.date)}</span>
    </>
  )
  return (
    <div className="my-1.5 flex justify-center" data-testid="service-message">
      {jump ? (
        <button type="button" onClick={jump} title="Go to the pinned message" className={`${cls} hover:text-slate-200`}>
          {content}
        </button>
      ) : (
        <span className={cls}>{content}</span>
      )}
    </div>
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

function AvatarContextMenu({ x, y, sender, onClose, onOpenProfile, onSendMessage, onMention, onRestrict }: {
  x: number
  y: number
  sender: MessageSender
  onClose: () => void
  onOpenProfile?: (sender: MessageSender) => void
  onSendMessage?: (sender: MessageSender) => void
  onMention?: (sender: MessageSender) => void
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
      {onOpenProfile && sender.person_id != null && (
        <button onClick={() => { onClose(); onOpenProfile(sender) }} className={itemCls}><User size={14} /> Open Profile</button>
      )}
      {onSendMessage && sender.person_id != null && (
        <button onClick={() => { onClose(); onSendMessage(sender) }} className={itemCls}><MessageSquare size={14} /> Send Message</button>
      )}
      {onMention && sender.name && (
        <button onClick={() => { onClose(); onMention(sender) }} className={itemCls}><AtSign size={14} /> Mention</button>
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
export function ScamSection({ message, onScamVerdict }: { message: Message; onScamVerdict?: (msg: Message, scope: ScamVerdictScope) => void }) {
  const check = useScamCheck(message.conversation_id, message.id)
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
          <button onClick={() => dismissScamCheck(message.conversation_id, message.id)} aria-label="Dismiss" className="hover:text-emerald-100"><X size={11} /></button>
        </div>
      )}
      {check?.status === 'error' && (
        <div className="flex items-center gap-1.5 px-3 py-1.5 border-t border-orange-500/30 bg-orange-500/10 text-orange-300 text-xs">
          <ShieldAlert size={13} className="flex-shrink-0" />
          <span className="flex-1">{check.error || 'Scam check failed'}</span>
          <button onClick={() => void startScamCheck(message.conversation_id, message.id)} className="underline hover:text-orange-100">Retry</button>
          <button onClick={() => dismissScamCheck(message.conversation_id, message.id)} aria-label="Dismiss" className="hover:text-orange-100"><X size={11} /></button>
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
                  {message.sender?.identity_id != null && !message.is_outgoing && (
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
  onJumpToExternal, channel, showAvatar, isGroupChat, isFlaggedSender: flaggedProp, onReportScam: reportProp, onScamVerdict: verdictProp,
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
    onClick: () => void startScamCheck(message.conversation_id, message.id),
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
  const meta = !album && structuredMeta(message.meta) ? message.meta! : null
  // Polls, locations and contacts carry no caption; `text` is only their plain fallback
  const showText = !!message.text && !meta
  // An album shows every item's attachments; one message may carry several (email)
  const attachments: Attachment[] = album ? album.flatMap((m) => m.attachments ?? []) : (meta ? [] : message.attachments ?? [])
  const rendered = attachments.filter((a) => RENDERED_MEDIA.has(a.kind))
  const unrendered = attachments.find((a) => !RENDERED_MEDIA.has(a.kind))
  const mediaOnly = !album && !meta && rendered.length === 0 && unrendered && !message.text
  const sender = message.sender
  const senderName = sender?.name ?? null
  const reply_to = replyTarget(message)
  const jumpToReply = reply_to && 'messageId' in reply_to
    ? (onJumpToMessage ? () => onJumpToMessage(reply_to.messageId) : null)
    : reply_to && onJumpToExternal ? () => onJumpToExternal(reply_to.externalId) : null

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
              avatarUrl={sender?.avatar_url}
              name={senderName}
              personId={sender?.person_id}
              size={28}
              onContextMenu={sender?.identity_id != null ? (e) => {
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

      {avatarMenu && sender && (
        <AvatarContextMenu
          x={avatarMenu.x}
          y={avatarMenu.y}
          sender={sender}
          onClose={() => setAvatarMenu(null)}
          onOpenProfile={onOpenProfile}
          onSendMessage={onSendMessage}
          onMention={onMention}
          onRestrict={isGroupChat && onRestrict && sender.identity_id != null ? () => onRestrict(sender, message.conversation_id) : undefined}
        />
      )}

      <div className={`max-w-[70%] ${isOut ? 'items-end' : 'items-start'} flex flex-col`}>
        {showSender && !isOut && senderName && (
          sender?.person_id != null ? (
            <Link to={personUrl(sender.person_id)} className={`mb-0.5 ml-1 text-xs font-medium transition-colors inline-flex items-center gap-1 ${senderLabelCls}`}>
              {isFlaggedSender && <ShieldAlert size={11} className="flex-shrink-0" />}
              {senderName}
            </Link>
          ) : (
            <span className={`mb-0.5 ml-1 text-xs font-medium inline-flex items-center gap-1 ${isFlaggedSender ? 'text-red-400' : 'text-purple-400'}`}>
              {isFlaggedSender && <ShieldAlert size={11} className="flex-shrink-0" />}
              {senderName}
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
              onClick={jumpToReply ?? undefined}
              disabled={!jumpToReply}
              title={jumpToReply ? 'Go to the original message' : undefined}
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
          {message.fwd_from_name && (
            <p className={`px-3 pt-2 text-[11px] ${isOut ? 'text-indigo-200' : 'text-sky-300'}`} data-testid="forward-header">
              <Forward size={10} className="mr-1 inline align-[-1px]" />
              Forwarded from <span className="font-medium">{message.fwd_from_name}</span>
            </p>
          )}
          {message.subject && (
            <p className="px-3 pt-2 font-semibold select-text" data-testid="message-subject">{message.subject}</p>
          )}
          {meta ? <MetaContent meta={meta} isOut={isOut} />
            : rendered.length > 1 ? <MediaAlbumGrid attachments={rendered} isOut={isOut} />
            : rendered.length === 1 ? <MediaView attachment={rendered[0]} isOut={isOut} />
            : null}
          {showText && (
            <p className="px-3 py-2 select-text">
              <FormattedText text={message.text!} html={message.text_html} isOut={isOut} channel={channel} />
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
            <span className={`px-3 py-2 block italic ${isOut ? 'text-indigo-300' : 'text-slate-400'}`}>[{unrendered.kind}]</span>
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

/** Emails (an email thread, or any message with a subject) render as an email, everything else as a chat bubble. */
function MessageView(props: Props) {
  if (props.conversationKind === 'email_thread' || props.message.subject != null) {
    return (
      <EmailMessageView
        message={props.message}
        highlighted={props.highlighted}
        onReply={props.onReply}
        onScamVerdict={props.onScamVerdict}
      />
    )
  }
  return <MessageBubble {...props} />
}

/** Re-renders only when its own message (or album items) or a prop actually changes. */
export default memo(MessageView, (prev, next) => {
  for (const k of Object.keys(next) as (keyof Props)[]) {
    if (k === 'album') continue
    if (prev[k] !== next[k]) return false
  }
  return Object.keys(prev).length === Object.keys(next).length && sameAlbum(prev.album, next.album)
})
