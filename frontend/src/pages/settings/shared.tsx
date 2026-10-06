import type { ReactNode } from 'react'
import { reportError, toast } from '../../lib/toast'

export const selectClass =
  'w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2.5 text-sm text-slate-100 outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 cursor-pointer disabled:cursor-default disabled:opacity-60'

export const inputClass =
  'rounded-lg border border-slate-600 bg-slate-700 px-3 py-2.5 text-sm text-slate-100 outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500'

export const labelClass = 'mb-1.5 block text-xs font-medium text-slate-400'

/** A titled settings block: heading, description, then the card body. */
export function Section({ title, description, children, className = 'mt-8' }: {
  title: string
  description?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section className={`${className} max-w-xl`}>
      <h2 className="mb-1 text-sm font-medium text-slate-300">{title}</h2>
      {description != null && <p className="mb-4 text-xs text-slate-400">{description}</p>}
      {children}
    </section>
  )
}

export function Card({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <div className={`overflow-hidden rounded-xl border border-slate-700 bg-slate-800 p-4 ${className}`}>
      {children}
    </div>
  )
}

export function Switch({ checked, onChange, disabled, label }: {
  checked: boolean
  onChange: (next: boolean) => void
  disabled?: boolean
  label: string
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-9 flex-shrink-0 rounded-full border-2 border-transparent transition-colors disabled:opacity-50 ${checked ? 'bg-emerald-500' : 'bg-slate-600'}`}
    >
      <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${checked ? 'translate-x-4' : 'translate-x-0'}`} />
    </button>
  )
}

/** Inline "Saving…" / validation hint shown under a setting control. */
export function FieldStatus({ saving, invalid }: { saving: boolean; invalid: string | null }) {
  if (invalid) return <p role="alert" className="mt-1.5 text-xs text-amber-400">{invalid}</p>
  if (saving) return <p className="mt-1.5 text-xs text-slate-500">Saving…</p>
  return null
}

export async function copyText(text: string, what = 'Copied') {
  try {
    await navigator.clipboard.writeText(text)
    toast.success(what)
  } catch (e) {
    reportError(e, "Couldn't copy to clipboard")
  }
}
