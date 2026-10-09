import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { MessageSquare, Settings, ShieldAlert } from 'lucide-react'
import type { ScamVerdictFilter } from '../api/types'
import type { ScannedMessageFilters } from '../api/client'
import { exportScannedMessages, getScamFlags, getScamReports, triggerMassScan } from '../api/client'
import { useLatestQuery } from '../hooks/useLatestQuery'
import { useAsyncAction } from '../hooks/useAsyncAction'
import { useCoalescedRefetch } from '../hooks/useCoalescedRefetch'
import { useWsEvent } from '../hooks/useWebSocket'
import { toast } from '../lib/toast'
import { useShieldInvalidation } from './shield/invalidate'
import { useScannedList } from './shield/useScannedList'
import { useRescan } from './shield/useRescan'
import { useCloudSync } from './shield/useCloudSync'
import { useCloudSettings } from './shield/useCloudSettings'
import ScannedTable from './shield/ScannedTable'
import FlaggedTab from './shield/FlaggedTab'
import ReportsTab from './shield/ReportsTab'
import SyncTab from './shield/SyncTab'
import PipelineTab from './shield/PipelineTab'
import SettingsPanel from './shield/SettingsPanel'
import FeedbackPanel from './shield/FeedbackPanel'
import { NO_DEV_FILTERS, buildScannedFilters } from './shield/filters'
import type { DevFilters } from './shield/filters'

type Tab = 'flagged' | 'scanned' | 'reports' | 'sync' | 'pipeline' | 'unmarked'

const VERDICT_FILTERS: { value: ScamVerdictFilter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'scam', label: 'Scam' },
  { value: 'not_scam', label: 'Safe' },
  { value: 'failed', label: 'Failed' },
  { value: 'pending', label: 'Pending' },
]

const EMPTY_SCANNED: Record<ScamVerdictFilter, string> = {
  all: 'No scanned messages yet',
  scam: 'No scam messages found',
  not_scam: 'No messages checked as safe',
  failed: 'No failed scans',
  pending: 'Nothing waiting to be scanned',
  manual: 'No manual verdicts',
  unmarked: 'No messages marked not scam',
}

const UNMARKED_FILTERS: ScannedMessageFilters = { verdict_filter: 'unmarked' }

function TabButton({ active, color, onClick, children, count }: { active: boolean; color: string; onClick: () => void; children: ReactNode; count?: number }) {
  return (
    <button
      onClick={onClick}
      className={`px-3 py-2 text-xs font-medium border-b-2 transition-colors ${active ? color : 'border-transparent text-slate-500 hover:text-slate-300'}`}
    >
      {children}
      {!!count && <span className="ml-1.5 rounded-full bg-slate-700 px-1.5 py-0.5 text-[10px]">{count}</span>}
    </button>
  )
}

function HeaderToggle({ active, title, onClick, children }: { active: boolean; title: string; onClick: () => void; children: ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={`rounded-lg p-1.5 transition-colors ${active ? 'bg-slate-700 text-slate-200' : 'text-slate-500 hover:bg-slate-800 hover:text-slate-300'}`}
      title={title}
    >
      {children}
    </button>
  )
}

const devInput = 'rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-300 border border-slate-700 placeholder:text-slate-600'

export default function Shield() {
  const [tab, setTab] = useState<Tab>('flagged')
  const [devMode, setDevMode] = useState(() => {
    try { return localStorage.getItem('developer_mode') === 'true' } catch { return false }
  })
  const [panel, setPanel] = useState<'settings' | 'feedback' | null>(null)

  // Developer mode is toggled from Settings
  useEffect(() => {
    const handler = () => {
      try { setDevMode(localStorage.getItem('developer_mode') === 'true') } catch { /* storage unavailable */ }
    }
    window.addEventListener('developer_mode_changed', handler)
    return () => window.removeEventListener('developer_mode_changed', handler)
  }, [])
  useEffect(() => {
    if (!devMode && (tab === 'pipeline' || tab === 'unmarked')) setTab('flagged')
  }, [devMode, tab])

  // ── Scanned messages: verdict tab + applied developer filters ──
  const [verdictFilter, setVerdictFilter] = useState<ScamVerdictFilter>('all')
  const [devDraft, setDevDraft] = useState<DevFilters>(NO_DEV_FILTERS)
  const [devApplied, setDevApplied] = useState<DevFilters>(NO_DEV_FILTERS)
  const filters = useMemo(
    () => buildScannedFilters(devMode ? devApplied : NO_DEV_FILTERS, verdictFilter),
    [devMode, devApplied, verdictFilter],
  )
  const scanned = useScannedList(filters)
  const unmarked = useScannedList(UNMARKED_FILTERS, { enabled: devMode && tab === 'unmarked' })
  const rescan = useRescan()

  // ── Flags: refetched on mutations and on scam WS events ──
  const flagsQuery = useLatestQuery('scam-flags', () => getScamFlags())
  const refreshFlags = useCoalescedRefetch(flagsQuery.refetch, 1000)
  useShieldInvalidation(refreshFlags)
  useWsEvent('scam_users_flagged', refreshFlags)
  useWsEvent('scam_verdict_changed', refreshFlags)
  useWsEvent('scam_detected', refreshFlags)
  useWsEvent('resync', refreshFlags)
  const flags = flagsQuery.data?.flags ?? []

  const reportsQuery = useLatestQuery('scam-reports', () => getScamReports())
  useWsEvent('resync', () => { void reportsQuery.refetch() })
  const reports = reportsQuery.data?.reports ?? []

  const cloudSync = useCloudSync()
  const cloudSettings = useCloudSettings()

  const massScan = useAsyncAction(async () => {
    const r = await triggerMassScan()
    toast.info(r.messages_queued ? `${r.messages_queued} messages queued for scanning` : 'Nothing new to scan')
    scanned.refresh()
  }, { errorContext: 'Mass scan failed' })

  const exportAction = useAsyncAction(async () => {
    const r = await exportScannedMessages(filters)
    if (r.saved) toast.success(`Exported ${r.count} message${r.count === 1 ? '' : 's'}`)
    else toast.info('Export cancelled')
  }, { errorContext: 'Export failed' })

  const devFiltersDirty = JSON.stringify(devDraft) !== JSON.stringify(devApplied)
  const devFiltersActive = !!(devApplied.senderId || devApplied.conversationId || devApplied.minMembers)

  return (
    <div className="flex h-full flex-1 flex-col overflow-hidden">
      <header className="flex items-center gap-3 border-b border-slate-700 bg-slate-900 px-6 py-3">
        <ShieldAlert size={18} className="text-red-400" />
        <h1 className="text-sm font-semibold text-slate-200">Shield</h1>
        <div className="ml-auto flex items-center gap-2">
          <HeaderToggle active={panel === 'feedback'} title="Send feedback" onClick={() => setPanel((p) => (p === 'feedback' ? null : 'feedback'))}>
            <MessageSquare size={15} />
          </HeaderToggle>
          <HeaderToggle active={panel === 'settings'} title="Detection settings" onClick={() => setPanel((p) => (p === 'settings' ? null : 'settings'))}>
            <Settings size={15} />
          </HeaderToggle>
        </div>
      </header>

      {panel === 'settings' && <SettingsPanel settings={cloudSettings} cloudSync={cloudSync} />}
      {panel === 'feedback' && <FeedbackPanel />}

      <div className="flex items-center gap-1 border-b border-slate-700 bg-slate-900 px-6">
        <TabButton active={tab === 'flagged'} color="border-red-400 text-red-400" onClick={() => setTab('flagged')} count={flags.length}>
          Flagged Scammers
        </TabButton>
        <TabButton active={tab === 'scanned'} color="border-indigo-400 text-indigo-400" onClick={() => setTab('scanned')} count={scanned.total}>
          Scanned Messages
        </TabButton>
        <TabButton active={tab === 'reports'} color="border-amber-400 text-amber-400" onClick={() => setTab('reports')} count={reports.length}>
          Reports
        </TabButton>
        <TabButton active={tab === 'sync'} color="border-emerald-400 text-emerald-400" onClick={() => setTab('sync')}>
          Sync History
        </TabButton>
        {devMode && (
          <>
            <TabButton active={tab === 'pipeline'} color="border-cyan-400 text-cyan-400" onClick={() => setTab('pipeline')}>
              Pipeline
            </TabButton>
            <TabButton active={tab === 'unmarked'} color="border-blue-400 text-blue-400" onClick={() => setTab('unmarked')}>
              Unmarked
            </TabButton>
          </>
        )}

        {tab === 'scanned' && (
          <div className="ml-auto flex items-center gap-3">
            {devMode && (
              <>
                <button
                  disabled={massScan.pending}
                  onClick={() => { void massScan.run() }}
                  className="rounded-lg bg-indigo-600 px-2.5 py-1 text-[10px] font-medium text-white hover:bg-indigo-500 disabled:opacity-40"
                >
                  {massScan.pending ? 'Queuing…' : 'Mass Scan'}
                </button>
                <span className="text-slate-700">|</span>
                <form
                  className="flex items-center gap-2"
                  onSubmit={(e) => { e.preventDefault(); setDevApplied(devDraft) }}
                >
                  <input placeholder="Sender identity ID" value={devDraft.senderId} onChange={(e) => setDevDraft((d) => ({ ...d, senderId: e.target.value }))} className={`w-20 ${devInput}`} />
                  <input placeholder="Conversation ID" value={devDraft.conversationId} onChange={(e) => setDevDraft((d) => ({ ...d, conversationId: e.target.value }))} className={`w-20 ${devInput}`} />
                  <input placeholder="Min members" value={devDraft.minMembers} onChange={(e) => setDevDraft((d) => ({ ...d, minMembers: e.target.value }))} className={`w-24 ${devInput}`} />
                  <button type="submit" disabled={!devFiltersDirty} className="rounded bg-slate-700 px-2 py-0.5 text-[10px] text-slate-300 hover:bg-slate-600 disabled:opacity-40">Apply</button>
                  {(devFiltersActive || devFiltersDirty) && (
                    <button type="button" onClick={() => { setDevDraft(NO_DEV_FILTERS); setDevApplied(NO_DEV_FILTERS) }} className="text-[10px] text-slate-500 hover:text-slate-300">Clear</button>
                  )}
                </form>
                <span className="text-slate-700">|</span>
                <button
                  disabled={exportAction.pending || !scanned.total}
                  onClick={() => { void exportAction.run() }}
                  title={`Exports all ${scanned.total} messages matching the current filters`}
                  className="rounded bg-emerald-600/80 px-2 py-0.5 text-[10px] text-white hover:bg-emerald-500 disabled:opacity-40"
                >
                  {exportAction.pending ? 'Exporting…' : `Export JSON (${scanned.total})`}
                </button>
              </>
            )}
            <div className="flex items-center gap-1 rounded-lg bg-slate-800 p-0.5">
              {VERDICT_FILTERS.map(({ value, label }) => (
                <button
                  key={value}
                  onClick={() => setVerdictFilter(value)}
                  className={`rounded-md px-2 py-0.5 text-[10px] font-medium transition-colors ${
                    verdictFilter === value ? 'bg-slate-700 text-slate-200' : 'text-slate-500 hover:text-slate-400'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="flex-1 overflow-y-auto">
        {tab === 'flagged' && (
          <FlaggedTab flags={flags} loading={flagsQuery.loading} error={flagsQuery.error} onRetry={() => { void flagsQuery.refetch() }} />
        )}
        {tab === 'scanned' && (
          <ScannedTable
            list={scanned}
            devMode={devMode}
            rescan={rescan}
            emptyTitle={EMPTY_SCANNED[verdictFilter]}
            emptyHint={verdictFilter !== 'all' || devFiltersActive
              ? 'Try switching the filter to "All" (or clearing filters) to see every scanned message.'
              : 'Use "Check for scam" on messages in Chats to start scanning.'}
          />
        )}
        {tab === 'reports' && (
          <ReportsTab reports={reports} loading={reportsQuery.loading} error={reportsQuery.error} onRetry={() => { void reportsQuery.refetch() }} />
        )}
        {tab === 'sync' && <SyncTab cloudSync={cloudSync} settings={cloudSettings} />}
        {tab === 'unmarked' && devMode && (
          <ScannedTable
            list={unmarked}
            devMode={devMode}
            rescan={rescan}
            emptyTitle={EMPTY_SCANNED.unmarked}
            emptyHint={'Right-click scanned messages and choose "Mark as not scam" to see them here.'}
          />
        )}
        {tab === 'pipeline' && devMode && <PipelineTab />}
      </div>
    </div>
  )
}
