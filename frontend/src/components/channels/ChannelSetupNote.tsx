import type { ChannelSetup } from '../../api/accounts'

/**
 * A channel's one-time setup next to its connect button (onboarding and
 * Settings → Connections): progress while it runs in the background, then
 * "Ready to link" or why it failed. Renders nothing without a setup.
 */
export default function ChannelSetupNote({ setup }: { setup?: ChannelSetup | null }) {
  if (!setup) return null
  if (setup.state === 'running' || setup.state === 'cancelling') {
    const pct = Math.round(Math.min(Math.max(setup.progress ?? 0, 0), 1) * 100)
    return (
      <span className="flex min-w-0 flex-1 items-center gap-2 text-[11px] text-slate-400">
        <span className="truncate">{setup.state === 'cancelling' ? 'Cancelling setup…' : `Setting up… ${pct}%`}</span>
        <span
          role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}
          className="h-1 w-16 flex-shrink-0 overflow-hidden rounded-full bg-slate-700"
        >
          <span className="block h-full rounded-full bg-indigo-500 transition-all duration-300" style={{ width: `${pct}%` }} />
        </span>
      </span>
    )
  }
  if (setup.state === 'ready') return <span className="text-[11px] text-emerald-400">Ready to link</span>
  if (setup.state === 'failed') {
    return <span className="truncate text-[11px] text-red-400" title={setup.message ?? undefined}>Setup failed</span>
  }
  return null
}
