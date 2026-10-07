import { Cloud, History } from 'lucide-react'
import { ageDays, fmtRelative, fmtShortDate } from '../../utils/time'
import { errorMessage } from '../../lib/toast'
import type { CloudSync } from './useCloudSync'
import type { CloudSettings } from './useCloudSettings'
import { SyncDismissalsToggle, SyncNowButton } from './SyncControls'
import { EmptyState, ErrorState, LoadingRow, Pill } from './shared'

function ProgressBar({ pct, color }: { pct: number; color: string }) {
  return (
    <div className="h-1.5 w-full rounded-full bg-slate-700 overflow-hidden">
      <div className={`h-full rounded-full transition-all ${color}`} style={{ width: `${pct}%` }} />
    </div>
  )
}

function StatCard({ title, synced, total, unit, pending, color, children }: {
  title: string; synced: number; total: number; unit?: string; pending: number; color: string; children?: React.ReactNode
}) {
  const pct = total > 0 ? Math.round((synced / total) * 100) : 0
  return (
    <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-4 space-y-3">
      <h3 className="text-xs font-medium text-slate-300">{title}</h3>
      <div className="flex items-end gap-2">
        <span className="text-2xl font-bold text-slate-200">{synced}</span>
        <span className="text-xs text-slate-500 mb-0.5">/ {total}{unit ? ` ${unit}` : ''}</span>
      </div>
      {children}
      <div className="space-y-1">
        <ProgressBar pct={pct} color={color} />
        <div className="flex justify-between text-[10px]">
          <span className="text-emerald-400">{pct}% synced</span>
          {pending > 0 && <span className="text-slate-500">{pending} pending</span>}
        </div>
      </div>
    </div>
  )
}

function Insight({ dot, children }: { dot: string; children: React.ReactNode }) {
  return (
    <li className="flex items-center gap-2">
      <span className={`h-1.5 w-1.5 rounded-full flex-shrink-0 ${dot}`} />
      {children}
    </li>
  )
}

export default function SyncTab({ cloudSync, settings }: { cloudSync: CloudSync; settings: CloudSettings }) {
  const { history: s, historyLoading, historyError, refetchHistory } = cloudSync
  if (historyLoading) return <LoadingRow />
  if (!s) {
    return historyError
      ? <ErrorState message={`Couldn't load sync history: ${errorMessage(historyError)}`} onRetry={() => { void refetchHistory() }} />
      : <EmptyState icon={<Cloud size={32} />} title="Sync data unavailable" />
  }

  const d = s.last_sync_details
  const staleDays = ageDays(s.last_synced_at)
  const configured = !!settings.cloudUrl && settings.keySet

  return (
    <div className="p-6 max-w-2xl space-y-6">
      <div className="space-y-2">
        <div className="flex items-center gap-3 flex-wrap">
          <History size={14} className={s.last_sync_status === 'failed' ? 'text-red-400' : 'text-slate-500'} />
          <span className="text-xs text-slate-400">
            Last synced: {s.last_synced_at ? fmtRelative(s.last_synced_at) : 'Never'}
          </span>
          {s.last_sync_status === 'success' && <Pill className="bg-emerald-500/20 text-emerald-300">Success</Pill>}
          {s.last_sync_status === 'partial' && <Pill className="bg-amber-500/20 text-amber-300">Partial</Pill>}
          {s.last_sync_status === 'failed' && <Pill className="bg-red-500/20 text-red-300">Failed</Pill>}
          <SyncNowButton cloudSync={cloudSync} configured={configured} size="xs" />
        </div>

        {s.last_sync_status === 'failed' && s.last_sync_error && (
          <div className="rounded-lg bg-red-500/10 border border-red-500/30 px-3 py-2">
            <p className="text-xs text-red-300">Sync failed: {s.last_sync_error}</p>
          </div>
        )}
        {s.last_sync_status === 'partial' && s.last_sync_error && (
          <div className="rounded-lg bg-amber-500/10 border border-amber-500/30 px-3 py-2">
            <p className="text-xs text-amber-300">Some uploads failed: {s.last_sync_error}</p>
          </div>
        )}

        {(s.last_sync_status === 'success' || s.last_sync_status === 'partial') && d && (
          <div className="text-[10px] text-slate-500 flex items-center gap-3 flex-wrap">
            {d.flags_submitted > 0 && <span>{d.flags_submitted} flags uploaded</span>}
            {d.reports_submitted > 0 && <span>{d.reports_submitted} reports uploaded</span>}
            {d.scan_results_submitted > 0 && <span>{d.scan_results_submitted} scans uploaded</span>}
            {(d.dismissals_submitted ?? 0) > 0 && <span>{d.dismissals_submitted} dismissals uploaded</span>}
            {(d.files_updated?.length ?? 0) > 0 && <span>{d.files_updated.length} files downloaded</span>}
            {!d.flags_submitted && !d.reports_submitted && !d.scan_results_submitted && !d.dismissals_submitted && !d.files_updated?.length && (
              <span>Nothing new to sync</span>
            )}
          </div>
        )}
      </div>

      <div className="grid grid-cols-2 gap-4">
        <StatCard title="Flagged Scammers" synced={s.flags.synced} total={s.flags.total} pending={s.flags.pending} color="bg-red-500" />
        <StatCard title="Scam Reports" synced={s.reports.synced} total={s.reports.total} pending={s.reports.pending} color="bg-amber-500" />
        <StatCard title="Scan Results" synced={s.scans.synced} total={s.scans.total} unit="to sync" pending={s.scans.pending} color="bg-indigo-500">
          <div className="flex flex-col gap-0.5 text-[10px]">
            <span className="text-red-400">{s.scans.scam_detected} scam detected</span>
            {s.scans.failed > 0 && <span className="text-orange-400">{s.scans.failed} scan failed</span>}
            {(s.scans.queued ?? 0) > 0 && <span className="text-slate-400">{s.scans.queued} queued for scanning</span>}
            {s.dismissals.total > 0 && (
              <span className="flex items-center gap-1 text-slate-400">
                {s.dismissals.synced}/{s.dismissals.total} dismissed
                {settings.syncDismissals ? (
                  <span className="text-emerald-400" title="Dismissals are being synced">✓</span>
                ) : (
                  <span className="text-slate-600" title="Enable 'Sync dismissed scam flags' to upload these">✗</span>
                )}
                {s.dismissals.pending > 0 && settings.syncDismissals && (
                  <span className="text-slate-500">({s.dismissals.pending} pending)</span>
                )}
              </span>
            )}
            <span className="text-slate-600">{s.scans.total_scanned} scanned total</span>
          </div>
        </StatCard>
        <StatCard title="Analytics" synced={s.analytics.synced} total={s.analytics.total} unit={`period${s.analytics.total !== 1 ? 's' : ''}`} pending={s.analytics.pending} color="bg-cyan-500">
          {s.analytics.latest && (
            <div className="flex flex-col gap-0.5 text-[10px]">
              <span className="text-slate-500">Latest: {fmtShortDate(s.analytics.latest.period_start)}</span>
              <span className="text-slate-400">{s.analytics.latest.total_scanned} scanned · {s.analytics.latest.scam_detected} scam · {s.analytics.latest.pipeline_errors} errors</span>
              <span className="text-slate-500">avg {s.analytics.latest.avg_pipeline_ms}ms/scan</span>
            </div>
          )}
        </StatCard>
      </div>

      <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-4 space-y-2">
        <SyncDismissalsToggle settings={settings} />
      </div>

      <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-4 space-y-2">
        <h3 className="text-xs font-medium text-slate-300">Insights</h3>
        <ul className="space-y-1.5 text-xs text-slate-400">
          {s.flags.pending > 0 && (
            <Insight dot="bg-red-400">{s.flags.pending} flagged scammer{s.flags.pending !== 1 ? 's' : ''} not yet synced to cloud</Insight>
          )}
          {s.reports.pending > 0 && (
            <Insight dot="bg-amber-400">{s.reports.pending} report{s.reports.pending !== 1 ? 's' : ''} awaiting upload</Insight>
          )}
          {s.scans.pending > 0 && (
            <Insight dot="bg-indigo-400">{s.scans.pending} scan result{s.scans.pending !== 1 ? 's' : ''} pending sync</Insight>
          )}
          {s.flags.pending === 0 && s.reports.pending === 0 && s.scans.pending === 0 && (
            <Insight dot="bg-emerald-400">All data synced to cloud — nothing pending</Insight>
          )}
          {s.last_sync_status === 'failed' && (
            <Insight dot="bg-red-400">Last sync failed — pending items will be retried on next sync attempt</Insight>
          )}
          {!s.last_synced_at && (
            <Insight dot="bg-slate-500">Cloud sync has never run. Configure cloud settings and sync to start sharing data.</Insight>
          )}
          {staleDays != null && staleDays >= 2 && (
            <Insight dot="bg-amber-400">Last sync was {Math.floor(staleDays)} days ago — consider syncing to keep cloud data up to date</Insight>
          )}
        </ul>
      </div>
    </div>
  )
}
