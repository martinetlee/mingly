import type { ReactNode } from 'react'
import { Cloud, CloudOff, ShieldAlert, ShieldCheck } from 'lucide-react'
import type { ScamFields, ScannedMessage } from '../../api/types'

/** Stable row id for a scanned message (selection, spinners, trace toggles). */
export const messageKey = (m: { chat_id: number; tg_message_id: number }) => `${m.chat_id}-${m.tg_message_id}`

export function stripFormatting(text: string): string {
  return text
    .replace(/\[(\*{0,2})(.*?)\1\]\([^)]*\)/g, '$2')
    .replace(/\*\*(.*?)\*\*/g, '$1')
    .replace(/__(.*?)__/g, '$1')
    .replace(/`([^`]*)`/g, '$1')
    .replace(/[*_~`]/g, '')
}

export function previewText(text: string | null | undefined, max = 80): string {
  if (!text) return '—'
  const t = stripFormatting(text)
  return t.length > max ? t.slice(0, max) + '…' : t
}

/** A scan produced a usable score (completed or short-circuited). */
export const hasResult = (m: Pick<ScamFields, 'scam_status'>) =>
  m.scam_status === 'completed' || m.scam_status === 'short_circuited'

export type ParsedTrace = Record<string, any> | null

export function parseTrace(m: Pick<ScannedMessage, 'trace_json'>): ParsedTrace {
  if (!m.trace_json) return null
  try { return JSON.parse(m.trace_json) } catch { return null }
}

export function ConfidencePill({ value }: { value: number | null | undefined }) {
  if (value == null) return <span className="text-slate-500">—</span>
  const pct = Math.round(value * 100)
  const cls = value > 0.7 ? 'text-red-400' : value > 0.4 ? 'text-amber-400' : 'text-yellow-400'
  return (
    <div className="flex items-center gap-1.5">
      <div className="h-1 w-12 rounded-full bg-slate-700 overflow-hidden">
        <div
          className={`h-full rounded-full ${value > 0.7 ? 'bg-red-500' : value > 0.4 ? 'bg-amber-500' : 'bg-yellow-500'}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className={`text-[10px] font-medium ${cls}`}>{pct}%</span>
    </div>
  )
}

export function SyncBadge({ synced }: { synced: boolean }) {
  return synced ? (
    <span className="inline-flex items-center gap-0.5 text-[10px] text-emerald-400" title="Synced to cloud">
      <Cloud size={10} />
    </span>
  ) : (
    <span className="inline-flex items-center gap-0.5 text-[10px] text-slate-600" title="Not synced">
      <CloudOff size={10} />
    </span>
  )
}

const pillBase = 'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium'

export function Pill({ className, title, children }: { className: string; title?: string; children: ReactNode }) {
  return <span className={`${pillBase} ${className}`} title={title}>{children}</span>
}

/**
 * Verdict pills rendered purely from the server's scam fields — the UI never
 * re-derives a verdict from scam_score. A failed scan is shown as failed,
 * never as safe.
 */
export function VerdictPills({ m, shortCircuitNode }: { m: ScamFields; shortCircuitNode?: string | null }) {
  const manual = m.scam_manual_verdict
  let main: ReactNode
  if (!manual && m.scam_status === 'failed') {
    main = <Pill className="bg-orange-500/20 text-orange-300" title={m.scam_error || undefined}>Scan failed</Pill>
  } else if (!manual && m.scam_status === 'pending') {
    main = <Pill className="bg-slate-700 text-slate-300">Pending</Pill>
  } else if (!manual && !m.scam_status) {
    main = <Pill className="bg-slate-700 text-slate-400">Not scanned</Pill>
  } else if (m.effective_is_scam) {
    main = <Pill className="bg-red-500/20 text-red-300"><ShieldAlert size={10} /> Scam</Pill>
  } else {
    main = <Pill className="bg-emerald-500/20 text-emerald-300"><ShieldCheck size={10} /> Safe</Pill>
  }
  return (
    <div className="flex items-center gap-1 flex-wrap">
      {main}
      {m.scam_status === 'short_circuited' && (
        <Pill className="bg-amber-500/20 text-amber-300">⚡ {shortCircuitNode || 'short-circuit'}</Pill>
      )}
      {manual === 'not_scam' && <Pill className="bg-blue-500/20 text-blue-300">Marked safe</Pill>}
      {manual === 'scam' && <Pill className="bg-red-500/20 text-red-300 border border-red-500/30">Marked scam</Pill>}
      {manual && m.scam_status === 'failed' && (
        <Pill className="bg-orange-500/10 text-orange-300" title={m.scam_error || undefined}>Scan failed</Pill>
      )}
      {hasResult(m) && m.scam_error && (
        <Pill className="bg-orange-500/10 text-orange-300" title={`Last re-check failed: ${m.scam_error}`}>Re-check failed</Pill>
      )}
    </div>
  )
}

export function EmptyState({ icon, title, hint }: { icon: ReactNode; title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-20">
      <div className="mb-3 text-slate-600">{icon}</div>
      <p className="text-sm font-medium text-slate-400">{title}</p>
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  )
}

export function LoadingRow() {
  return <div className="p-6 text-xs text-slate-500">Loading…</div>
}

export function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-16">
      <p className="text-xs text-red-300">{message}</p>
      <button onClick={onRetry} className="rounded bg-slate-700 px-2 py-1 text-[10px] text-slate-300 hover:bg-slate-600">Retry</button>
    </div>
  )
}
