import { Loader2, Pause, Play } from 'lucide-react'
import type { ArchiveState, ArchiveStatus } from '../../api/types'
import { getArchiveStatus, pauseArchive, startArchive } from '../../api/client'
import { useLatestQuery } from '../../hooks/useLatestQuery'
import { useAsyncAction } from '../../hooks/useAsyncAction'
import { useWsEvent } from '../../hooks/useWebSocket'
import { errorMessage } from '../../lib/toast'
import { fmtDateTime, fmtRelative, fmtTime } from '../../utils/time'
import { Card } from './shared'

const STATE_LABEL: Record<ArchiveState, string> = {
  idle: 'Idle',
  running: 'Archiving',
  paused: 'Paused',
  waiting_flood: 'Waiting on Telegram rate limit',
  stopped: 'Stopped',
}

const STATE_TONE: Record<ArchiveState, string> = {
  idle: 'bg-slate-600/40 text-slate-300',
  running: 'bg-indigo-500/20 text-indigo-300',
  paused: 'bg-amber-500/20 text-amber-300',
  waiting_flood: 'bg-amber-500/20 text-amber-300',
  stopped: 'bg-slate-600/40 text-slate-400',
}

/**
 * Background archive of every DM's full history: progress, pause/resume.
 * Live via `archive_progress`; refetches after a reconnect.
 */
export default function ArchiveCard() {
  const q = useLatestQuery(['archive-status'], (signal) => getArchiveStatus(signal))
  const status = q.data

  // The event carries the status flat
  useWsEvent('archive_progress', ({ type: _type, ...next }) => q.setData(next))
  useWsEvent('resync', () => { void q.refetch() })

  const start = useAsyncAction(async () => q.setData(await startArchive()), { errorContext: "Couldn't start the archive" })
  const pause = useAsyncAction(async () => q.setData(await pauseArchive()), { errorContext: "Couldn't pause the archive" })
  const busy = start.pending || pause.pending

  const active = status?.state === 'running' || status?.state === 'waiting_flood'
  const dms = status?.dms
  const pct = dms && dms.total > 0 ? Math.min(100, Math.round((dms.complete / dms.total) * 100)) : 0

  return (
    <Card className="mt-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm text-slate-200">DM archive</p>
          <p className="mt-0.5 text-xs text-slate-400">
            Fetches the full history of every direct message in the background, so agents (and search) see every message.
          </p>
        </div>
        {status && (
          <span data-testid="archive-state" className={`flex-shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${STATE_TONE[status.state] ?? STATE_TONE.idle}`}>
            {STATE_LABEL[status.state] ?? status.state}
          </span>
        )}
      </div>

      {!status && q.loading && (
        <div className="mt-4 flex items-center gap-2 text-xs text-slate-500"><Loader2 size={12} className="animate-spin" /> Loading archive status…</div>
      )}
      {!status && !q.loading && q.error != null && (
        <div className="mt-4 flex items-center gap-2 text-xs text-rose-400">
          <span className="flex-1">Couldn't load archive status: {errorMessage(q.error)}</span>
          <button type="button" onClick={() => void q.refetch()} className="text-slate-300 underline hover:text-slate-100">Retry</button>
        </div>
      )}

      {status && dms && (
        <div className="mt-4 space-y-3 border-t border-slate-700 pt-4">
          <div>
            <div className="mb-1 flex items-center justify-between text-xs">
              <span className="text-slate-300">
                <span className="font-medium text-slate-100">{dms.complete.toLocaleString()}</span> of {dms.total.toLocaleString()} DMs complete
              </span>
              <span className="text-slate-500">{pct}%</span>
            </div>
            <div
              role="progressbar"
              aria-label="DM archive progress"
              aria-valuemin={0}
              aria-valuemax={dms.total}
              aria-valuenow={dms.complete}
              className="h-1.5 w-full overflow-hidden rounded-full bg-slate-700"
            >
              <div className="h-full rounded-full bg-emerald-500 transition-all" style={{ width: `${pct}%` }} />
            </div>
          </div>

          <dl className="grid grid-cols-3 gap-2 text-xs">
            <Stat label="Pending" value={dms.pending} />
            <Stat label="Failed" value={dms.failed} tone={dms.failed > 0 ? 'text-rose-400' : undefined} />
            <Stat label="Bots skipped" value={dms.bots_skipped} />
          </dl>

          {status.state === 'waiting_flood' && (
            <p className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
              Telegram asked us to slow down.{' '}
              {status.flood_wait_until ? `Resuming at ${fmtTime(status.flood_wait_until)}.` : 'Resuming automatically.'}
            </p>
          )}
          {status.current_chat && active && (
            <p className="truncate text-xs text-slate-400">
              Now archiving: <span className="text-slate-200">{status.current_chat.title ?? `Conversation ${status.current_chat.conversation_id}`}</span>
            </p>
          )}
          {status.groups_queued > 0 && (
            <p className="text-xs text-slate-400">{status.groups_queued} group{status.groups_queued === 1 ? '' : 's'} queued for full history</p>
          )}
          <p className="text-[11px] text-slate-500">
            {status.last_topup_at
              ? <>Last top-up <span title={fmtDateTime(status.last_topup_at)}>{fmtRelative(status.last_topup_at)}</span></>
              : 'No top-up yet'}
          </p>

          <div className="flex gap-2">
            {active ? (
              <button
                type="button"
                onClick={() => void pause.run()}
                disabled={busy}
                className="flex items-center gap-1.5 rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-xs font-medium text-slate-300 hover:bg-slate-600 disabled:opacity-50"
              >
                {pause.pending ? <Loader2 size={12} className="animate-spin" /> : <Pause size={12} />} Pause
              </button>
            ) : (
              <button
                type="button"
                onClick={() => void start.run()}
                disabled={busy}
                className="flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-2 text-xs font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
              >
                {start.pending ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
                {status.state === 'paused' ? 'Resume' : 'Start'}
              </button>
            )}
          </div>
        </div>
      )}

      <ul className="mt-4 list-disc space-y-0.5 border-t border-slate-700 pl-4 pt-3 text-[11px] text-slate-500">
        <li>Chats deleted for both sides, or dialogs you deleted, no longer exist on Telegram and can't be recovered.</li>
        <li>Secret chats aren't available through the Telegram API.</li>
        <li>Groups and channels are archived on demand (Chat info › Archive full history).</li>
      </ul>
    </Card>
  )
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className="rounded-lg bg-slate-900/50 px-2.5 py-1.5">
      <dt className="text-[10px] uppercase tracking-wider text-slate-500">{label}</dt>
      <dd className={`text-sm font-medium ${tone ?? 'text-slate-200'}`}>{value.toLocaleString()}</dd>
    </div>
  )
}
