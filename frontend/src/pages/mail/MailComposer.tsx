import { useRef, useState } from 'react'
import { Loader2, Paperclip, Send, X } from 'lucide-react'
import type { Message } from '../../api/types'
import { composeEmail, isValidAddress, parseAddressList, sendEmail, sendEmailFiles } from '../../api/email'
import { ApiError } from '../../lib/http'
import { errorMessage } from '../../lib/toast'
import { newNonce, sendKey } from '../../features/chat/chatUiStore'

export type ComposerMode =
  | { kind: 'new'; accountId: number }
  | { kind: 'reply'; conversationId: number; target: Message | null }

export interface ComposerInitial {
  to: string[]
  cc: string[]
  subject: string
}

const MAX_FILE_SIZE = 25 * 1024 * 1024 // Gmail's own limit per message

const fieldCls = 'min-w-0 flex-1 bg-transparent text-sm text-slate-200 placeholder-slate-500 outline-none'

/**
 * Write an email: a new thread from one account, or a reply / reply-all in a
 * thread. To / Cc are the complete recipient lists (prefilled for replies).
 */
export default function MailComposer({ mode, initial, onSent, onCancel }: {
  mode: ComposerMode
  initial: ComposerInitial
  /** Sent: the thread it landed in (a changed subject can start a new one) */
  onSent: (conversationId: number) => void
  onCancel: () => void
}) {
  const [to, setTo] = useState(initial.to.join(', '))
  const [cc, setCc] = useState(initial.cc.join(', '))
  const [showCc, setShowCc] = useState(initial.cc.length > 0)
  const [subject, setSubject] = useState(initial.subject)
  const [text, setText] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Same rule as chat sends (ChatPane): a retry reuses the nonce only when the
  // previous attempt never got an answer AND nothing was changed since —
  // an edited email is a new send, never the cached result of the old one
  const failedSend = useRef<{ key: string; nonce: string } | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)

  const addFiles = (list: FileList | null) => {
    if (!list) return
    const picked = Array.from(list)
    const tooBig = picked.filter((f) => f.size > MAX_FILE_SIZE)
    if (tooBig.length) setError(`Too large (25 MB max): ${tooBig.map((f) => f.name).join(', ')}`)
    setFiles((prev) => [...prev, ...picked.filter((f) => f.size <= MAX_FILE_SIZE)])
  }

  const send = async () => {
    const toList = parseAddressList(to)
    const ccList = parseAddressList(cc)
    const bad = [...toList, ...ccList].filter((a) => !isValidAddress(a))
    if (bad.length) { setError(`Not an email address: ${bad.join(', ')}`); return }
    if (toList.length === 0) { setError('Add at least one recipient'); return }
    if (mode.kind === 'new' && !subject.trim() && !text.trim()) { setError('Write a subject or a message'); return }
    setBusy(true)
    setError(null)
    const key = sendKey(
      JSON.stringify([mode.kind === 'new' ? mode.accountId : mode.conversationId, subject.trim(), toList, ccList, text]),
      files, mode.kind === 'reply' ? mode.target?.id : null,
    )
    const nonce = failedSend.current?.key === key ? failedSend.current.nonce : newNonce()
    try {
      let conversationId: number
      if (mode.kind === 'new') {
        const res = await composeEmail(
          { accountId: mode.accountId, to: toList, cc: ccList, subject: subject.trim(), text, files, clientNonce: nonce },
          files.length ? setProgress : undefined,
        ).promise
        conversationId = res.conversation_id
      } else {
        const opts = { subject: subject.trim() || undefined, to: toList, cc: ccList, clientNonce: nonce }
        const replyTo = mode.target?.id
        if (files.length) {
          await sendEmailFiles(mode.conversationId, files, text || undefined, replyTo, setProgress, opts).promise
        } else {
          await sendEmail(mode.conversationId, text, replyTo, opts)
        }
        conversationId = mode.conversationId
      }
      failedSend.current = null
      onSent(conversationId)
    } catch (e) {
      // No answer: the server may have sent it — an unchanged retry must reuse the nonce
      const answered = e instanceof ApiError && e.status !== 0
      failedSend.current = answered ? null : { key, nonce }
      setError(errorMessage(e))
    } finally {
      setBusy(false)
      setProgress(null)
    }
  }

  return (
    <div className="flex flex-col rounded-xl border border-slate-700 bg-slate-800" data-testid="mail-composer">
      <div className="divide-y divide-slate-700/70 px-3">
        <label className="flex items-center gap-2 py-1.5">
          <span className="w-14 flex-shrink-0 text-xs text-slate-500">To</span>
          <input className={fieldCls} value={to} onChange={(e) => setTo(e.target.value)} placeholder="name@example.com, …" aria-label="To" />
          {!showCc && (
            <button type="button" onClick={() => setShowCc(true)} className="text-xs text-indigo-400 hover:text-indigo-300">Cc</button>
          )}
        </label>
        {showCc && (
          <label className="flex items-center gap-2 py-1.5">
            <span className="w-14 flex-shrink-0 text-xs text-slate-500">Cc</span>
            <input className={fieldCls} value={cc} onChange={(e) => setCc(e.target.value)} placeholder="name@example.com, …" aria-label="Cc" />
          </label>
        )}
        <label className="flex items-center gap-2 py-1.5">
          <span className="w-14 flex-shrink-0 text-xs text-slate-500">Subject</span>
          <input className={fieldCls} value={subject} onChange={(e) => setSubject(e.target.value)} aria-label="Subject" />
        </label>
      </div>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); void send() } }}
        autoFocus
        rows={8}
        placeholder="Write your message…  (⌘↵ to send)"
        aria-label="Message"
        className="min-h-[140px] resize-y border-t border-slate-700/70 bg-transparent px-3 py-2 text-sm text-gray-100 placeholder-slate-500 outline-none"
      />
      {files.length > 0 && (
        <div className="flex flex-wrap gap-1.5 border-t border-slate-700/70 px-3 py-2">
          {files.map((f, i) => (
            <span key={`${f.name}-${i}`} className="inline-flex items-center gap-1 rounded-md border border-slate-600 bg-slate-700/60 px-2 py-0.5 text-xs text-slate-300">
              <Paperclip size={11} /> {f.name}
              <button type="button" onClick={() => setFiles((p) => p.filter((_, j) => j !== i))} disabled={busy} aria-label={`Remove ${f.name}`} className="text-slate-500 hover:text-slate-200">
                <X size={11} />
              </button>
            </span>
          ))}
        </div>
      )}
      {error && <p className="border-t border-slate-700/70 px-3 py-1.5 text-xs text-red-400" role="alert">{error}</p>}
      <div className="flex items-center gap-2 border-t border-slate-700/70 px-3 py-2">
        <button
          type="button"
          onClick={() => void send()}
          disabled={busy}
          className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-400 disabled:opacity-60"
        >
          {busy ? <Loader2 size={13} className="animate-spin" /> : <Send size={13} />}
          {busy && progress != null ? `Sending ${Math.round(progress * 100)}%` : 'Send'}
        </button>
        <button type="button" onClick={() => fileInput.current?.click()} disabled={busy} title="Attach files" className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-700 hover:text-slate-200">
          <Paperclip size={15} />
        </button>
        <input ref={fileInput} type="file" multiple hidden onChange={(e) => { addFiles(e.target.files); e.target.value = '' }} />
        <button type="button" onClick={onCancel} disabled={busy} className="ml-auto text-xs text-slate-400 hover:text-slate-200">Discard</button>
      </div>
    </div>
  )
}
