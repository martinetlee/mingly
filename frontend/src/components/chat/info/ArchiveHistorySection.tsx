import { useEffect, useState } from 'react'
import { History, CheckCircle2, Loader2 } from 'lucide-react'
import type { Conversation } from '../../../api/types'
import { archiveConversation } from '../../../api/client'
import { useAsyncAction } from '../../../hooks/useAsyncAction'
import { toast } from '../../../lib/toast'

/**
 * "Archive full history": queues a complete walk of this conversation's history
 * (DMs are also covered by the background archive). Complete once every
 * message from the first one is stored locally.
 */
export default function ArchiveHistorySection({ chat }: { chat: Conversation }) {
  const [queued, setQueued] = useState(false)
  const [complete, setComplete] = useState(!!chat.history_complete)
  const [count, setCount] = useState<number | null>(null)
  useEffect(() => { if (chat.history_complete) setComplete(true) }, [chat.history_complete])

  const archive = useAsyncAction(async () => {
    const res = await archiveConversation(chat.id)
    setCount(res.coverage.message_count)
    if (res.coverage.history_complete) {
      setComplete(true)
      toast.success('Full history is already stored')
    } else {
      setQueued(res.queued)
      toast.info('Archiving full history in the background')
    }
  }, { errorContext: 'Couldn’t start archiving' })

  return (
    <div className="flex flex-col gap-2 border-b border-slate-700/50 px-4 py-4" data-testid="archive-history">
      <div className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wider text-slate-400">
        <History size={11} /> History
      </div>
      {complete ? (
        <p className="flex items-center gap-1.5 text-xs text-emerald-400">
          <CheckCircle2 size={12} /> Full history archived{count != null ? ` · ${count.toLocaleString()} messages` : ''}
        </p>
      ) : queued ? (
        <p className="flex items-center gap-1.5 text-xs text-indigo-300">
          <Loader2 size={12} className="animate-spin" /> Archiving…{count != null ? ` ${count.toLocaleString()} messages so far` : ''}
        </p>
      ) : (
        <>
          <p className="text-[11px] text-slate-500">
            Only part of this chat's history is stored locally. Archive it so search and agents see every message.
          </p>
          <button
            type="button"
            onClick={() => void archive.run()}
            disabled={archive.pending}
            className="flex items-center justify-center gap-1.5 rounded-lg border border-slate-600 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-700 disabled:opacity-50 transition-colors"
          >
            {archive.pending ? <Loader2 size={12} className="animate-spin" /> : <History size={12} />}
            Archive full history
          </button>
        </>
      )}
    </div>
  )
}
