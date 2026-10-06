import { Cloud, FileText, Flag } from 'lucide-react'
import type { ScamReport } from '../../api/types'
import { fmtRelative } from '../../utils/time'
import { errorMessage } from '../../lib/toast'
import { EmptyState, ErrorState, LoadingRow, SyncBadge } from './shared'

export default function ReportsTab({ reports, loading, error, onRetry }: {
  reports: ScamReport[]
  loading: boolean
  error: unknown
  onRetry: () => void
}) {
  if (loading) return <LoadingRow />
  if (error && !reports.length) return <ErrorState message={`Couldn't load reports: ${errorMessage(error)}`} onRetry={onRetry} />
  if (!reports.length) {
    return <EmptyState icon={<FileText size={32} />} title="No reports submitted" hint={'Use "Report scam" on messages in Chats to submit reports.'} />
  }
  return (
    <table className="w-full text-left">
      <thead className="sticky top-0 z-10 bg-slate-900 border-b border-slate-700">
        <tr className="text-[10px] uppercase tracking-wider text-slate-500">
          <th className="px-6 py-2 font-medium">Reported User</th>
          <th className="px-3 py-2 font-medium">Chat</th>
          <th className="px-3 py-2 font-medium">Category</th>
          <th className="px-3 py-2 font-medium">Status</th>
          <th className="px-2 py-2 font-medium w-8" title="Cloud sync status"><Cloud size={10} /></th>
          <th className="px-3 py-2 font-medium">Date</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-slate-800">
        {reports.map((r) => (
          <tr key={r.report_id} className="hover:bg-slate-800/50 transition-colors">
            <td className="px-6 py-2.5">
              <div className="flex items-center gap-2">
                <Flag size={12} className="flex-shrink-0 text-red-400" />
                <span className="text-xs font-medium text-slate-200 truncate max-w-[140px]">
                  {r.reported_sender_name || `ID: ${r.reported_telegram_id}`}
                </span>
              </div>
            </td>
            <td className="px-3 py-2.5">
              <span className="text-xs text-slate-400 truncate max-w-[160px] block">{r.chat_name}</span>
            </td>
            <td className="px-3 py-2.5">
              <span className="text-xs text-slate-400">{r.custom_category || r.scam_category || '—'}</span>
            </td>
            <td className="px-3 py-2.5">
              <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium ${
                r.status === 'confirmed' ? 'bg-red-500/20 text-red-300' :
                r.status === 'false_positive' ? 'bg-emerald-500/20 text-emerald-300' :
                'bg-amber-500/20 text-amber-300'
              }`}>
                {r.status}
              </span>
            </td>
            <td className="px-2 py-2.5 text-center">
              <SyncBadge synced={!!r.synced_at} />
            </td>
            <td className="px-3 py-2.5 text-xs text-slate-400 whitespace-nowrap">
              {fmtRelative(r.created_at) || '—'}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
