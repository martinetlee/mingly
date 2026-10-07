import { useCallback, useEffect, useState } from 'react'
import { getSyncHistory, triggerSync } from '../../api/client'
import type { CloudSyncResult } from '../../api/types'
import { useLatestQuery } from '../../hooks/useLatestQuery'
import { useAsyncAction } from '../../hooks/useAsyncAction'
import { refreshFlaggedSenders } from '../../hooks/useFlaggedSenders'
import { errorMessage } from '../../lib/toast'
import { invalidateShieldData } from './invalidate'

export type SyncOutcome = { kind: 'success' | 'partial' | 'error'; text: string }

const POLL_MS = 2000

export function describeSyncResult(r: CloudSyncResult): SyncOutcome {
  switch (r.status) {
    case 'success': {
      const parts = [
        r.flags_submitted && `${r.flags_submitted} flags`,
        r.reports_submitted && `${r.reports_submitted} reports`,
        r.scan_results_submitted && `${r.scan_results_submitted} scans`,
        r.dismissals_submitted && `${r.dismissals_submitted} dismissals`,
      ].filter(Boolean)
      const files = r.files_updated?.length ?? 0
      const detail = [parts.length ? `uploaded ${parts.join(', ')}` : '', files ? `${files} files updated` : ''].filter(Boolean).join('; ')
      return { kind: 'success', text: detail ? `Synced — ${detail}` : 'Synced — nothing new' }
    }
    case 'partial':
      return { kind: 'partial', text: `Partial sync — ${r.error || (r.errors ?? []).join('; ') || 'some uploads failed'}` }
    case 'reloaded':
      return { kind: 'success', text: 'Pipeline reloaded from disk' }
    case 'not_configured':
      return { kind: 'error', text: 'Cloud sync is not configured' }
    default:
      return { kind: 'error', text: r.error || `Sync ${r.status}` }
  }
}

/**
 * The single Cloud "Sync Now" for the whole Shield page (settings panel and
 * Sync History tab share this instance). The server runs one sync at a time
 * and a second POST joins it; here the button is disabled while either our
 * request or a server-side sync (e.g. the startup sync) is running, and the
 * history is polled until that finishes.
 */
export function useCloudSync() {
  const history = useLatestQuery('scam-sync-history', () => getSyncHistory())
  const [outcome, setOutcome] = useState<SyncOutcome | null>(null)
  const { refetch } = history

  const action = useAsyncAction(async () => {
    setOutcome(null)
    try {
      const r = await triggerSync()
      setOutcome(describeSyncResult(r))
      if (r.status === 'success' || r.status === 'partial') {
        // Cloud flags/verdict uploads change what Shield shows
        void refreshFlaggedSenders()
        invalidateShieldData()
      }
      return r
    } catch (e) {
      setOutcome({ kind: 'error', text: errorMessage(e) })
      throw e
    } finally {
      void refetch()
    }
  })

  const serverRunning = !!history.data?.running
  const running = action.pending || serverRunning

  // A sync we didn't start (startup / other window): follow it to completion
  useEffect(() => {
    if (!serverRunning || action.pending) return
    const t = setInterval(() => { void refetch() }, POLL_MS)
    return () => clearInterval(t)
  }, [serverRunning, action.pending, refetch])

  const run = action.run
  const sync = useCallback(() => { void run() }, [run])

  return {
    sync,
    running,
    outcome,
    history: history.data ?? null,
    historyLoading: history.loading,
    historyError: history.error,
    refetchHistory: refetch,
    lastSyncedAt: history.data?.last_synced_at ?? null,
  }
}

export type CloudSync = ReturnType<typeof useCloudSync>
