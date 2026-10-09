import { Link, useNavigate } from 'react-router-dom'
import { Cloud, ShieldAlert, ShieldCheck, X } from 'lucide-react'
import type { ScamFlag } from '../../api/types'
import { errorMessage } from '../../lib/toast'
import { chatUrl, personUrl } from '../../lib/routes'
import { useVerdictActions } from './useVerdictActions'
import { ConfidencePill, EmptyState, ErrorState, LoadingRow, SyncBadge } from './shared'

const SOURCE_STYLE: Record<ScamFlag['source'], { label: string; cls: string }> = {
  cloud: { label: 'Cloud', cls: 'bg-purple-500/20 text-purple-300' },
  manual: { label: 'Manual', cls: 'bg-amber-500/20 text-amber-300' },
  local: { label: 'Local', cls: 'bg-blue-500/20 text-blue-300' },
}

/** Active sender flags. "Unflag user" is the sender-scope not_scam verdict. */
export default function FlaggedTab({ flags, loading, error, onRetry }: {
  flags: ScamFlag[]
  loading: boolean
  error: unknown
  onRetry: () => void
}) {
  const navigate = useNavigate()
  const { dismissSender } = useVerdictActions()

  if (loading) return <LoadingRow />
  if (error && !flags.length) return <ErrorState message={`Couldn't load flagged scammers: ${errorMessage(error)}`} onRetry={onRetry} />
  if (!flags.length) {
    return <EmptyState icon={<ShieldCheck size={32} />} title="No flagged scammers" hint="Messages are being monitored for suspicious activity." />
  }

  return (
    <table className="w-full text-left">
      <thead className="sticky top-0 z-10 bg-slate-900 border-b border-slate-700">
        <tr className="text-[10px] uppercase tracking-wider text-slate-500">
          <th className="px-6 py-2 font-medium">User</th>
          <th className="px-3 py-2 font-medium">Source</th>
          <th className="px-3 py-2 font-medium">Flagged Message</th>
          <th className="px-3 py-2 font-medium">Reason</th>
          <th className="px-3 py-2 font-medium">Confidence</th>
          <th className="px-2 py-2 font-medium w-8" title="Cloud sync status"><Cloud size={10} /></th>
          <th className="px-3 py-2 font-medium w-8"></th>
        </tr>
      </thead>
      <tbody className="divide-y divide-slate-800">
        {flags.map((flag) => {
          const source = SOURCE_STYLE[flag.source] ?? SOURCE_STYLE.local
          const conversationId = flag.flagged_conversation_id
          const msgId = flag.flagged_message_id
          return (
            <tr key={flag.identity_id} className="hover:bg-slate-800/50 transition-colors">
              <td className="px-6 py-2.5">
                <div className="flex items-center gap-2">
                  <ShieldAlert size={12} className="flex-shrink-0 text-red-400" />
                  {flag.person_id != null ? (
                    <Link to={personUrl(flag.person_id)} className="text-xs font-medium text-slate-200 hover:text-indigo-300 truncate max-w-[140px]">
                      {flag.sender_name || `ID: ${flag.identity_id}`}
                    </Link>
                  ) : (
                    <span className="text-xs font-medium text-slate-200 truncate max-w-[140px]">
                      {flag.sender_name || `ID: ${flag.identity_id}`}
                    </span>
                  )}
                </div>
              </td>
              <td className="px-3 py-2.5">
                <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium ${source.cls}`}>{source.label}</span>
              </td>
              <td className="px-3 py-2.5">
                {flag.flagged_message_text ? (
                  <button
                    disabled={!conversationId || !msgId}
                    onClick={() => conversationId && msgId && navigate(chatUrl(conversationId, msgId))}
                    className="text-xs text-slate-400 hover:text-slate-200 truncate max-w-[220px] block text-left transition-colors"
                    title={flag.flagged_message_text}
                  >
                    {flag.flagged_message_text}
                  </button>
                ) : (
                  <span className="text-xs text-slate-500">—</span>
                )}
              </td>
              <td className="px-3 py-2.5">
                <span className="text-xs text-slate-400 truncate max-w-[200px] block" title={flag.reason || undefined}>
                  {flag.reason || '—'}
                </span>
              </td>
              <td className="px-3 py-2.5">
                <ConfidencePill value={flag.confidence} />
              </td>
              <td className="px-2 py-2.5 text-center">
                <SyncBadge synced={!!flag.synced_to_cloud} />
              </td>
              <td className="px-3 py-2.5">
                <button
                  onClick={() => { void dismissSender(flag.identity_id, flag.sender_name) }}
                  title="Unflag user"
                  className="rounded p-1 text-slate-500 hover:bg-slate-700 hover:text-slate-300 transition-colors"
                >
                  <X size={13} />
                </button>
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
