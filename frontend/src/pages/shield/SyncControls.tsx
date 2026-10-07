import type { CloudSync } from './useCloudSync'
import type { CloudSettings } from './useCloudSettings'

/** "Sync Now" button + result, identical wherever it appears on the Shield page. */
export function SyncNowButton({ cloudSync, configured, size = 'sm' }: { cloudSync: CloudSync; configured: boolean; size?: 'sm' | 'xs' }) {
  const { sync, running, outcome } = cloudSync
  const pad = size === 'sm' ? 'px-3 py-1.5 text-xs' : 'px-3 py-1 text-[10px]'
  return (
    <>
      <button
        disabled={running || !configured}
        onClick={sync}
        title={configured ? undefined : 'Set a cloud URL and API key first'}
        className={`rounded-lg bg-indigo-600 font-medium text-white hover:bg-indigo-500 disabled:opacity-40 ${pad}`}
      >
        {running ? 'Syncing…' : 'Sync Now'}
      </button>
      {!running && outcome && (
        <span className={`text-[10px] ${outcome.kind === 'success' ? 'text-emerald-400' : outcome.kind === 'partial' ? 'text-amber-400' : 'text-red-400'}`}>
          {outcome.text}
        </span>
      )}
    </>
  )
}

export function SyncDismissalsToggle({ settings, compact }: { settings: CloudSettings; compact?: boolean }) {
  const help = 'When you dismiss a scam flag or mark a scanned message not scam, send that to the cloud so the detection model can learn from false positives.'
  return (
    <label className={`flex items-center cursor-pointer select-none ${compact ? 'gap-2 mt-2' : 'gap-3'}`}>
      <input
        type="checkbox"
        disabled={!settings.loaded}
        checked={settings.syncDismissals}
        onChange={(e) => { void settings.setSyncDismissals(e.target.checked) }}
        className={`rounded border-slate-600 bg-slate-700 text-emerald-500 focus:ring-0 focus:ring-offset-0 ${compact ? 'h-3 w-3' : 'h-3.5 w-3.5'}`}
      />
      {compact ? (
        <>
          <span className="text-[11px] text-slate-400">Sync dismissed scam flags to cloud</span>
          <span className="text-[10px] text-slate-600" title={help}>?</span>
        </>
      ) : (
        <div>
          <p className="text-xs font-medium text-slate-300">Sync dismissed scam flags</p>
          <p className="text-[10px] text-slate-500">{help}</p>
        </div>
      )}
    </label>
  )
}
