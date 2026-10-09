import { memo } from 'react'
import { Loader2, Paperclip } from 'lucide-react'
import type { MailThread } from '../../api/email'
import { fmtDate } from '../../utils/time'

/** "Ann, Bob, me" — the thread's senders in order of their first message */
export function sendersLabel(t: MailThread): string {
  const names = t.thread.senders.map((s) => (s.is_me ? 'me' : s.name || 'Unknown'))
  return names.length ? names.join(', ') : 'Unknown'
}

const ThreadRow = memo(function ThreadRow({ thread, selected, onOpen }: {
  thread: MailThread
  selected: boolean
  onOpen: (t: MailThread) => void
}) {
  const unread = thread.unread_count > 0
  return (
    <button
      type="button"
      onClick={() => onOpen(thread)}
      className={`flex w-full flex-col gap-0.5 border-b border-slate-700/40 px-3 py-2.5 text-left transition-colors ${
        selected ? 'bg-indigo-500/15' : 'hover:bg-slate-700/40'
      }`}
      aria-current={selected ? 'true' : undefined}
      data-testid="mail-thread-row"
    >
      <div className="flex items-center gap-1.5">
        {unread && <span className="h-2 w-2 flex-shrink-0 rounded-full bg-sky-400" aria-label="Unread" />}
        <span className={`min-w-0 flex-1 truncate text-sm ${unread ? 'font-semibold text-slate-100' : 'text-slate-300'}`}>
          {sendersLabel(thread)}
          {thread.thread.message_count > 1 && <span className="ml-1 text-xs font-normal text-slate-500">{thread.thread.message_count}</span>}
        </span>
        {thread.thread.has_attachments && <Paperclip size={12} className="flex-shrink-0 text-slate-500" aria-label="Has attachments" />}
        <span className="flex-shrink-0 text-[11px] text-slate-500">{fmtDate(thread.last_message?.date ?? thread.last_activity_at)}</span>
      </div>
      <p className={`truncate text-[13px] ${unread ? 'font-medium text-slate-200' : 'text-slate-300'}`}>{thread.title || '(no subject)'}</p>
      {thread.last_message?.text && <p className="truncate text-xs text-slate-500">{thread.last_message.text}</p>}
    </button>
  )
})

export default function ThreadList({ threads, selectedId, loading, hasMore, loadingMore, onOpen, onLoadMore, emptyText }: {
  threads: MailThread[]
  selectedId: number | null
  loading: boolean
  hasMore: boolean
  loadingMore: boolean
  onOpen: (t: MailThread) => void
  onLoadMore: () => void
  emptyText: string
}) {
  if (loading && threads.length === 0) {
    return <div className="flex flex-1 items-center justify-center text-slate-500"><Loader2 size={16} className="animate-spin" /></div>
  }
  if (threads.length === 0) {
    return <p className="px-4 py-8 text-center text-sm text-slate-500">{emptyText}</p>
  }
  return (
    <div className="flex-1 overflow-y-auto" data-testid="mail-thread-list">
      {threads.map((t) => <ThreadRow key={t.id} thread={t} selected={t.id === selectedId} onOpen={onOpen} />)}
      {hasMore && (
        <button
          type="button"
          onClick={onLoadMore}
          disabled={loadingMore}
          className="w-full py-3 text-center text-xs text-indigo-400 hover:text-indigo-300 disabled:text-slate-500"
        >
          {loadingMore ? 'Loading…' : 'Load more'}
        </button>
      )}
    </div>
  )
}
