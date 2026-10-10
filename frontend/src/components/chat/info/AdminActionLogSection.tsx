import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ClipboardList, ChevronDown, ChevronUp } from 'lucide-react'
import type { AdminActionLog, Conversation } from '../../../api/types'
import { getAdminActionLog } from '../../../api/client'
import { personUrl } from '../../../lib/routes'
import { fmtDateTime } from '../../../utils/time'

export default function AdminActionLogSection({ chat }: { chat: Conversation }) {
  const [expanded, setExpanded] = useState(false)
  const [logs, setLogs] = useState<AdminActionLog[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (expanded) {
      setLoading(true)
      getAdminActionLog(chat.id)
        .then(setLogs)
        .catch(() => setLogs([]))
        .finally(() => setLoading(false))
    }
  }, [expanded, chat.id])

  const actionLabel = (action: string) => {
    const labels: Record<string, string> = {
      promote: 'Promoted user',
      demote: 'Demoted user',
      ban: 'Banned user',
      restrict: 'Restricted user',
      approve: 'Approved user',
      create_invite_link: 'Created invite link',
      revoke_invite_link: 'Revoked invite link',
      update_settings: 'Updated settings',
    }
    return labels[action] || action
  }

  const actionColor = (action: string) => {
    if (action === 'ban') return 'text-red-400'
    if (action === 'promote') return 'text-sky-400'
    if (action === 'demote') return 'text-amber-400'
    if (action === 'approve') return 'text-emerald-400'
    return 'text-slate-400'
  }

  return (
    <div className="px-4 py-4 border-t border-slate-700/50 flex flex-col gap-2">
      <button onClick={() => setExpanded((v) => !v)} className="flex items-center gap-1.5 text-xs font-medium text-slate-400 uppercase tracking-wider hover:text-slate-300 transition-colors">
        <ClipboardList size={11} />
        Admin Log
        {expanded ? <ChevronUp size={11} className="ml-auto" /> : <ChevronDown size={11} className="ml-auto" />}
      </button>
      {expanded && (
        <div className="flex flex-col gap-1 max-h-48 overflow-y-auto">
          {loading ? (
            <p className="text-xs text-slate-500">Loading...</p>
          ) : logs.length === 0 ? (
            <p className="text-xs text-slate-500">No admin actions recorded</p>
          ) : (
            logs.map((log) => (
              <div key={log.id} className="rounded bg-slate-700/30 px-2 py-1.5 text-[11px] flex flex-col gap-0.5">
                <span className={actionColor(log.action)}>{actionLabel(log.action)}</span>
                {log.target_person_id != null ? (
                  <Link to={personUrl(log.target_person_id)} className="text-slate-400 hover:text-indigo-300">View member</Link>
                ) : log.target_identity_id != null && (
                  <span className="text-slate-500">Member #{log.target_identity_id}</span>
                )}
                {log.details && (
                  <span className="text-slate-500 truncate">{log.details}</span>
                )}
                {log.created_at && (
                  <span className="text-[10px] text-slate-600">{fmtDateTime(log.created_at)}</span>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  )
}
