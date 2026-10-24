/**
 * The amber notice a screen shows for an account problem (sync errors,
 * signed out…). Long text wraps (URLs included) and web addresses in it are
 * links, so an error such as "enable it by visiting https://…" can be acted on.
 */
import type { ReactNode } from 'react'
import { AlertTriangle } from 'lucide-react'

const URL_RE = /https?:\/\/[^\s<>"')\]]+[^\s<>"')\].,;:!?]/g

/** `text` with its http(s) URLs as links opening outside the app */
export function LinkifiedText({ text, linkClassName = 'underline hover:text-amber-100' }: { text: string; linkClassName?: string }) {
  const out: ReactNode[] = []
  let last = 0
  for (const m of text.matchAll(URL_RE)) {
    const at = m.index ?? 0
    if (at > last) out.push(text.slice(last, at))
    out.push(
      <a key={at} href={m[0]} target="_blank" rel="noopener noreferrer" className={linkClassName}>{m[0]}</a>,
    )
    last = at + m[0].length
  }
  if (last < text.length) out.push(text.slice(last))
  return <>{out}</>
}

export default function StatusBanner({ children, className = '', testId }: {
  children: ReactNode
  className?: string
  testId?: string
}) {
  return (
    <div
      role="status"
      className={`flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-200 ${className}`}
      data-testid={testId}
    >
      <AlertTriangle size={13} className="mt-0.5 flex-shrink-0" />
      <div className="min-w-0 flex-1 [overflow-wrap:anywhere]">{children}</div>
    </div>
  )
}
