/**
 * Status chrome shared by every message pane (Chats, Contacts, Tracked): the
 * history header (older history from the channel), the "jumping…" line and
 * the notice for a jump that couldn't land — so all panes behave the same.
 */
import { AlertTriangle, Loader2, X } from 'lucide-react'
import type { ChatMessages, JumpNotice } from '../../hooks/useChatMessages'

/** Shown above the oldest loaded message once local history runs out */
export function HistoryHeader({ msgs, canBackfill }: { msgs: ChatMessages; canBackfill: boolean }) {
  if (msgs.messages.length === 0 || msgs.hasOlder) return null
  return (
    <div className="flex justify-center py-2 text-[11px] text-slate-500">
      {msgs.backfill === 'loading' ? (
        <span className="flex items-center gap-1.5"><Loader2 size={12} className="animate-spin" /> Loading older messages…</span>
      ) : msgs.backfill === 'exhausted' || !canBackfill ? (
        <span>Beginning of conversation</span>
      ) : (
        <span className="flex items-center gap-2">
          {msgs.backfillError && <span className="text-orange-300">{msgs.backfillError}</span>}
          <button onClick={() => void msgs.fetchOlderFromChannel()} className="rounded border border-slate-600 px-2 py-0.5 hover:bg-slate-700 hover:text-slate-300">
            Load older messages
          </button>
        </span>
      )}
    </div>
  )
}

/** "Loading the message…" while a jump replaces the window */
export function JumpingStatus({ msgs }: { msgs: ChatMessages }) {
  if (!msgs.replacing || !msgs.jumpingTo) return null
  return (
    <div role="status" className="flex items-center gap-2 border-b border-slate-700/50 bg-slate-800/60 px-4 py-1.5 text-xs text-slate-400">
      <Loader2 size={12} className="animate-spin text-indigo-400" />
      {msgs.jumpingTo.kind === 'date' ? 'Loading messages from that date…' : 'Loading the message (fetching it if needed)…'}
    </div>
  )
}

/** Why a jump (search hit, reply quote, pinned banner, mention, date) didn't land */
export function jumpNoticeText(n: JumpNotice): string {
  const isMessage = n.target.kind !== 'date'
  const what = isMessage ? 'that message' : 'messages at that date'
  switch (n.reason) {
    case 'not_found':
      return isMessage
        ? 'That message isn’t available — it may have been deleted.'
        : 'No messages found at that date.'
    case 'unavailable': return `The channel is offline — couldn’t load ${what}.`
    case 'rate_limited': return `Rate limited by the channel — couldn’t load ${what} yet. Try again in a moment.`
    default: return `Couldn’t load ${what}${n.detail ? `: ${n.detail}` : '.'}`
  }
}

export function JumpNoticeBar({ notice, onRetry, onDismiss }: { notice: JumpNotice; onRetry: () => void; onDismiss: () => void }) {
  return (
    <div role="alert" className="flex items-center gap-2 border-b border-amber-500/20 bg-amber-500/10 px-4 py-1.5 text-xs text-amber-200">
      <AlertTriangle size={13} className="flex-shrink-0" />
      <span className="flex-1">{jumpNoticeText(notice)}</span>
      {notice.reason !== 'not_found' && (
        <button onClick={onRetry} className="underline hover:text-amber-50">Retry</button>
      )}
      <button onClick={onDismiss} aria-label="Dismiss" className="hover:text-amber-50"><X size={12} /></button>
    </div>
  )
}

/** The notice for the pane's last failed jump, with a retry of the same target */
export function PaneJumpNotice({ msgs }: { msgs: ChatMessages }) {
  const notice = msgs.jumpNotice
  if (!notice) return null
  const t = notice.target
  const retry = () => void (t.kind === 'message' ? msgs.jumpTo(t.messageId) : t.kind === 'external' ? msgs.jumpToExternal(t.externalId) : msgs.jumpToDate(t.date))
  return <JumpNoticeBar notice={notice} onRetry={retry} onDismiss={msgs.dismissJumpNotice} />
}
