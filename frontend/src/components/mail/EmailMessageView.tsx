import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Copy, Flag, ImageOff, Paperclip, Reply, ShieldAlert } from 'lucide-react'
import type { Message, MessageParticipant } from '../../api/types'
import { openExternalUrl } from '../../api/client'
import { setRemoteImagesAllowed, useRemoteImageSenders } from '../../api/email'
import { personUrl } from '../../lib/routes'
import { reportError, toast } from '../../lib/toast'
import { fmtDateTime } from '../../utils/time'
import { startScamCheck } from '../../features/chat/scamChecks'
import { useFeatures } from '../../context/FeaturesContext'
import SenderAvatar from '../chat/SenderAvatar'
import DocumentCard from '../media/DocumentCard'
import { ScamSection, plainTextToHtml } from '../chat/MessageBubble'
import type { ScamVerdictScope } from '../chat/MessageBubble'

interface Props {
  message: Message
  highlighted?: boolean
  onReply?: (msg: Message) => void
  onScamVerdict?: (msg: Message, scope: ScamVerdictScope) => void
  /** "Report scam" (Shield): the page opens ReportScamDialog — same action as a chat bubble's menu */
  onReportScam?: (msg: Message) => void
}

const REMOTE_IMG = /<img\b([^>]*?)\sdata-src="/gi

/** Blocked remote images are kept as `data-src` by the backend sanitizer. */
export function hasBlockedImages(html: string | null | undefined): boolean {
  return !!html && /<img\b[^>]*\sdata-src="/i.test(html)
}

/**
 * The iframe document: the (already sanitized) email HTML under a CSP that
 * forbids scripts, frames and every network fetch except images — and
 * those only when remote images are allowed.
 */
export function emailSrcDoc(html: string, allowRemote: boolean): string {
  const body = allowRemote ? html.replace(REMOTE_IMG, '<img$1 src="') : html
  const imgSrc = allowRemote ? 'https: http: data:' : 'data:'
  const csp = `default-src 'none'; img-src ${imgSrc}; style-src 'unsafe-inline'; font-src data:; base-uri 'none'; form-action 'none'`
  return `<!doctype html><html><head><meta charset="utf-8">`
    + `<meta http-equiv="Content-Security-Policy" content="${csp}">`
    + `<meta name="referrer" content="no-referrer">`
    + `<style>html,body{margin:0;padding:0;background:#fff;color:#1f2937}`
    + `body{padding:12px 14px;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;overflow-wrap:anywhere}`
    + `img{max-width:100%;height:auto}table{max-width:100%}pre{white-space:pre-wrap}a{color:#4f46e5}`
    + `blockquote{margin:0 0 0 .5em;padding-left:.75em;border-left:2px solid #d1d5db;color:#4b5563}</style>`
    + `</head><body>${body}</body></html>`
}

/**
 * Sanitized email HTML in a sandboxed iframe: no `allow-scripts`, so nothing
 * in the email can run. `allow-same-origin` lets this component size the
 * frame to its content and route link clicks to the system browser.
 */
const EmailHtmlFrame = memo(function EmailHtmlFrame({ html, allowRemote }: { html: string; allowRemote: boolean }) {
  const ref = useRef<HTMLIFrameElement>(null)
  const [height, setHeight] = useState(80)
  const srcDoc = useMemo(() => emailSrcDoc(html, allowRemote), [html, allowRemote])
  const cleanup = useRef<(() => void) | null>(null)

  const onLoad = useCallback(() => {
    cleanup.current?.()
    const doc = ref.current?.contentDocument
    if (!doc) return
    const measure = () => setHeight(Math.max(40, doc.documentElement.scrollHeight))
    measure()
    const onClick = (e: MouseEvent) => {
      const anchor = (e.target as Element | null)?.closest?.('a')
      if (!anchor) return
      e.preventDefault()
      const href = anchor.getAttribute('href') ?? ''
      if (/^(https?:|mailto:)/i.test(href)) void openExternalUrl(href)
    }
    doc.addEventListener('click', onClick)
    const images = Array.from(doc.images)
    images.forEach((img) => img.addEventListener('load', measure))
    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(measure) : null
    if (observer && doc.body) observer.observe(doc.body)
    cleanup.current = () => {
      doc.removeEventListener('click', onClick)
      images.forEach((img) => img.removeEventListener('load', measure))
      observer?.disconnect()
    }
  }, [])

  useEffect(() => () => cleanup.current?.(), [])

  return (
    <iframe
      ref={ref}
      title="Email body"
      sandbox="allow-same-origin"
      referrerPolicy="no-referrer"
      srcDoc={srcDoc}
      onLoad={onLoad}
      style={{ height }}
      className="block w-full rounded-b-xl border-0 bg-white"
      data-testid="email-html"
    />
  )
})

function addressLabel(p: Pick<MessageParticipant, 'display_name' | 'external_id'>): string {
  return p.display_name ? `${p.display_name} <${p.external_id}>` : p.external_id
}

function Recipients({ label, people }: { label: string; people: MessageParticipant[] }) {
  if (people.length === 0) return null
  return (
    <p className="truncate text-[11px] text-slate-400" title={people.map(addressLabel).join(', ')}>
      <span className="text-slate-500">{label} </span>
      {people.map((p, i) => (
        <span key={`${p.identity_id}-${p.role}`}>
          {i > 0 && ', '}
          <Link to={personUrl(p.person_id)} className="hover:text-slate-200 hover:underline">
            {p.display_name || p.external_id}
          </Link>
        </span>
      ))}
    </p>
  )
}

/** One email in a thread: header (from / to / cc / subject / date), body, attachments. */
function EmailMessageView({ message, highlighted, onReply, onScamVerdict: verdictProp, onReportScam: reportProp }: Props) {
  const { shield } = useFeatures()
  const onScamVerdict = shield ? verdictProp : undefined
  const onReportScam = shield ? reportProp : undefined
  const participants = message.participants ?? []
  const from = participants.find((p) => p.role === 'from')
  const senderEmail = from?.external_id ?? null
  const byRole = (role: MessageParticipant['role']) => participants.filter((p) => p.role === role)
  const allowedSenders = useRemoteImageSenders()
  const [loadOnce, setLoadOnce] = useState(false)
  const allowRemote = loadOnce || (senderEmail != null && allowedSenders.has(senderEmail))
  const blocked = hasBlockedImages(message.text_html)
  const attachments = message.attachments ?? []
  const name = message.sender?.name || from?.display_name || senderEmail || 'Unknown sender'

  const alwaysLoad = async () => {
    if (!senderEmail) return
    try {
      await setRemoteImagesAllowed(senderEmail, true)
    } catch (e) {
      reportError(e, 'Could not save the preference')
    }
  }
  const stopLoading = async () => {
    if (!senderEmail) return
    setLoadOnce(false)
    try {
      await setRemoteImagesAllowed(senderEmail, false)
    } catch (e) {
      reportError(e, 'Could not save the preference')
    }
  }
  const copyText = () => {
    if (!message.text) return
    navigator.clipboard.writeText(message.text).then(() => toast.success('Copied'), (e) => reportError(e, 'Copy failed'))
  }

  return (
    <div
      className={`group/email mb-2 rounded-xl border bg-slate-800/60 ${
        message.effective_is_scam ? 'border-red-500/50' : 'border-slate-700'
      } ${highlighted ? 'ring-2 ring-amber-400/50 transition-all duration-700' : ''}`}
      data-testid="email-message"
    >
      <div className="flex items-start gap-2.5 px-3 pt-2.5 pb-2">
        <SenderAvatar avatarUrl={message.sender?.avatar_url} name={name} personId={message.sender?.person_id} size={32} />
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2">
            {message.sender?.person_id != null ? (
              <Link to={personUrl(message.sender.person_id)} className="truncate text-sm font-semibold text-slate-100 hover:underline">
                {name}
              </Link>
            ) : (
              <span className="truncate text-sm font-semibold text-slate-100">{name}</span>
            )}
            {senderEmail && senderEmail !== name && <span className="truncate text-[11px] text-slate-500">{senderEmail}</span>}
            <span className="ml-auto flex-shrink-0 text-[11px] text-slate-500">{fmtDateTime(message.date)}</span>
          </div>
          <Recipients label="to" people={byRole('to')} />
          <Recipients label="cc" people={byRole('cc')} />
          {message.is_outgoing && <Recipients label="bcc" people={byRole('bcc')} />}
          {message.subject && (
            <p className="mt-0.5 text-[13px] font-medium text-slate-200 select-text" data-testid="message-subject">{message.subject}</p>
          )}
        </div>
        <div className="flex flex-shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover/email:opacity-100 group-focus-within/email:opacity-100">
          {onReply && (
            <button onClick={() => onReply(message)} className="rounded-full p-1 text-slate-500 hover:bg-slate-700 hover:text-slate-300" title="Reply" aria-label="Reply">
              <Reply size={14} />
            </button>
          )}
          {message.text && (
            <button onClick={copyText} className="rounded-full p-1 text-slate-500 hover:bg-slate-700 hover:text-slate-300" title="Copy text" aria-label="Copy text">
              <Copy size={14} />
            </button>
          )}
          {shield && (
            <button
              onClick={() => void startScamCheck(message.conversation_id, message.id)}
              className="rounded-full p-1 text-slate-500 hover:bg-slate-700 hover:text-amber-400"
              title="Check for scam"
              aria-label="Check for scam"
            >
              <ShieldAlert size={14} />
            </button>
          )}
          {onReportScam && (
            <button
              onClick={() => onReportScam(message)}
              className="rounded-full p-1 text-slate-500 hover:bg-slate-700 hover:text-red-400"
              title="Report scam"
              aria-label="Report scam"
            >
              <Flag size={14} />
            </button>
          )}
        </div>
      </div>

      {blocked && !allowRemote && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-slate-700 px-3 py-1.5 text-[11px] text-slate-400" data-testid="remote-images-banner">
          <ImageOff size={12} className="flex-shrink-0" />
          <span>Remote images are hidden to protect your privacy.</span>
          <button onClick={() => setLoadOnce(true)} className="text-indigo-400 hover:text-indigo-300">Load images</button>
          {senderEmail && (
            <button onClick={() => void alwaysLoad()} className="text-indigo-400 hover:text-indigo-300">
              Always load from {senderEmail}
            </button>
          )}
        </div>
      )}
      {blocked && allowRemote && senderEmail && allowedSenders.has(senderEmail) && (
        <div className="border-t border-slate-700 px-3 py-1 text-[11px] text-slate-500">
          Images from {senderEmail} load automatically.{' '}
          <button onClick={() => void stopLoading()} className="text-indigo-400 hover:text-indigo-300">Stop</button>
        </div>
      )}

      {message.text_html ? (
        <EmailHtmlFrame html={message.text_html} allowRemote={allowRemote} />
      ) : message.text ? (
        <p
          className="border-t border-slate-700 px-3 py-2 text-sm leading-relaxed text-gray-100 select-text formatted-text formatted-in"
          dangerouslySetInnerHTML={{ __html: plainTextToHtml(message.text) }}
          onClick={(e) => {
            const anchor = (e.target as HTMLElement).closest('a')
            const href = anchor?.getAttribute('href')
            if (!href) return
            e.preventDefault()
            void openExternalUrl(href)
          }}
        />
      ) : null}

      {attachments.length > 0 && (
        <div className="border-t border-slate-700 px-2 py-1.5" data-testid="email-attachments">
          <p className="flex items-center gap-1 px-1 text-[11px] text-slate-500">
            <Paperclip size={11} /> {attachments.length} attachment{attachments.length === 1 ? '' : 's'}
          </p>
          <div className="grid grid-cols-1 gap-0.5 sm:grid-cols-2">
            {attachments.map((a) => (
              <DocumentCard key={a.id} attachmentId={a.id} filename={a.filename} fileSize={a.size} mediaUrl={a.url} isOut={false} />
            ))}
          </div>
        </div>
      )}

      {shield && <ScamSection message={message} onScamVerdict={onScamVerdict} />}
    </div>
  )
}

export default memo(EmailMessageView)
