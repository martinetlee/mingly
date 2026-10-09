import { useCallback, useEffect, useRef, useState } from 'react'
import { Archive, Inbox, Loader2, MailOpen, Reply, ReplyAll } from 'lucide-react'
import type { Conversation, Message } from '../../api/types'
import { getConversation, getMessageWindow, isAbortError } from '../../api/client'
import { archiveThread, markThreadRead } from '../../api/email'
import { useWsEvent } from '../../hooks/useWebSocket'
import { errorMessage, reportError } from '../../lib/toast'
import EmailMessageView from '../../components/EmailMessageView'
import MailComposer from './MailComposer'
import type { ComposerInitial } from './MailComposer'
import { replyAllRecipients, replyRecipients, replySubject } from './recipients'

const THREAD_LIMIT = 200

type Reply = { target: Message | null; initial: ComposerInitial }

/** The reading pane: every message of one thread, thread actions and the reply composer. */
export default function ThreadView({ conversationId, messageId, me, onChanged }: {
  conversationId: number
  /** Scroll to and highlight this message */
  messageId: number | null
  /** The signed-in address of the thread's account (excluded from reply-all) */
  me: (accountId: number) => string | null
  /** The thread changed (read state, archive, new message): patch the list */
  onChanged: (conv: Conversation) => void
}) {
  const [conv, setConv] = useState<Conversation | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [reply, setReply] = useState<Reply | null>(null)
  const [busy, setBusy] = useState(false)
  const scrolledFor = useRef<string | null>(null)
  const autoRead = useRef<number | null>(null)
  const onChangedRef = useRef(onChanged)
  onChangedRef.current = onChanged

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const [c, win] = await Promise.all([
        getConversation(conversationId, signal),
        getMessageWindow(conversationId, { limit: THREAD_LIMIT }, signal),
      ])
      setConv(c)
      setMessages(win.messages)
      setError(null)
      return c
    } catch (e) {
      if (!isAbortError(e)) setError(errorMessage(e))
      return null
    } finally {
      setLoading(false)
    }
  }, [conversationId])

  useEffect(() => {
    const ctl = new AbortController()
    setLoading(true)
    setConv(null)
    setMessages([])
    setReply(null)
    scrolledFor.current = null
    void load(ctl.signal).then((c) => {
      // Opening a thread reads it, like any mail client
      if (c && c.unread_count > 0 && autoRead.current !== c.id) {
        autoRead.current = c.id
        markThreadRead(c.id, true).then((fresh) => { setConv(fresh); onChangedRef.current(fresh) }).catch(() => {})
      }
    })
    return () => ctl.abort()
  }, [load])

  useWsEvent('new_message', (e) => { if (e.conversation_id === conversationId) void load() })
  useWsEvent('conversations_synced', (e) => { if (e.conversation_ids.includes(conversationId)) void load() })

  // Centre the requested message once its element exists
  useEffect(() => {
    const key = `${conversationId}:${messageId}`
    if (messageId == null || scrolledFor.current === key || !messages.some((m) => m.id === messageId)) return
    scrolledFor.current = key
    document.getElementById(`mail-msg-${messageId}`)?.scrollIntoView?.({ block: 'center' })
  }, [conversationId, messageId, messages])

  const latest = messages[messages.length - 1] ?? null
  const myAddress = conv ? me(conv.account_id) : null

  const startReply = (target: Message | null, all: boolean) => {
    const subject = replySubject(target?.subject ?? conv?.title)
    const initial = all ? { ...replyAllRecipients(target, myAddress), subject } : { to: replyRecipients(target), cc: [], subject }
    setReply({ target, initial })
  }

  const act = async (fn: () => Promise<Conversation>, what: string) => {
    setBusy(true)
    try {
      const fresh = await fn()
      setConv(fresh)
      onChanged(fresh)
    } catch (e) {
      reportError(e, what)
    } finally {
      setBusy(false)
    }
  }

  if (loading && !conv) {
    return <div className="flex flex-1 items-center justify-center text-slate-500"><Loader2 size={18} className="animate-spin" /></div>
  }
  if (error && !conv) {
    return <div className="flex flex-1 items-center justify-center px-8 text-center text-sm text-red-400">Couldn’t load this email: {error}</div>
  }
  if (!conv) return null

  const btn = 'inline-flex items-center gap-1 rounded-lg border border-slate-600 px-2 py-1 text-xs text-slate-300 hover:bg-slate-700 disabled:opacity-50'
  return (
    <div className="flex min-w-0 flex-1 flex-col overflow-hidden" data-testid="mail-thread">
      <div className="flex flex-wrap items-start gap-2 border-b border-slate-700/50 px-5 py-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-base font-semibold text-slate-100 select-text">{conv.title || '(no subject)'}</h2>
          {(conv.labels ?? []).length > 0 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {(conv.labels ?? []).map((l) => (
                <span key={l} className="rounded bg-slate-700/70 px-1.5 py-0.5 text-[10px] text-slate-300">{l}</span>
              ))}
            </div>
          )}
        </div>
        <div className="flex flex-shrink-0 flex-wrap items-center gap-1.5">
          <button className={btn} onClick={() => startReply(latest, false)} disabled={!latest}><Reply size={13} /> Reply</button>
          <button className={btn} onClick={() => startReply(latest, true)} disabled={!latest}><ReplyAll size={13} /> Reply all</button>
          {conv.is_archived ? (
            <button className={btn} disabled={busy} onClick={() => void act(() => archiveThread(conv.id, false), 'Could not move to Inbox')}>
              <Inbox size={13} /> Move to Inbox
            </button>
          ) : (
            <button className={btn} disabled={busy} onClick={() => void act(() => archiveThread(conv.id, true), 'Could not archive')}>
              <Archive size={13} /> Archive
            </button>
          )}
          <button
            className={btn}
            disabled={busy}
            onClick={() => {
              autoRead.current = conv.id // don't immediately re-read it
              void act(() => markThreadRead(conv.id, conv.unread_count > 0), 'Could not change the read state')
            }}
          >
            <MailOpen size={13} /> {conv.unread_count > 0 ? 'Mark read' : 'Mark unread'}
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-5 py-4">
        {messages.length === 0 && <p className="text-center text-sm text-slate-500">No messages stored for this thread yet.</p>}
        {messages.map((m) => (
          <div key={m.id} id={`mail-msg-${m.id}`}>
            <EmailMessageView message={m} highlighted={m.id === messageId} onReply={(target) => startReply(target, false)} />
          </div>
        ))}
        {reply ? (
          <div className="mt-3">
            <MailComposer
              key={`${reply.target?.id ?? 'thread'}-${reply.initial.to.join(',')}`}
              mode={{ kind: 'reply', conversationId: conv.id, target: reply.target }}
              initial={reply.initial}
              onSent={() => { setReply(null); void load().then((c) => { if (c) onChanged(c) }) }}
              onCancel={() => setReply(null)}
            />
          </div>
        ) : latest && (
          <div className="mt-3 flex gap-2">
            <button className={btn} onClick={() => startReply(latest, false)}><Reply size={13} /> Reply</button>
            <button className={btn} onClick={() => startReply(latest, true)}><ReplyAll size={13} /> Reply all</button>
          </div>
        )}
      </div>
    </div>
  )
}
