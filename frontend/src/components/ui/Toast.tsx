import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { AlertCircle, CheckCircle2, Info, X } from 'lucide-react'
import { dismissToast, subscribeToasts, toast } from '../../lib/toast'
import type { ToastItem, ToastKind } from '../../lib/toast'

const STYLE: Record<ToastKind, { icon: typeof Info; accent: string }> = {
  success: { icon: CheckCircle2, accent: 'text-emerald-400 border-emerald-500/40' },
  error: { icon: AlertCircle, accent: 'text-red-400 border-red-500/40' },
  info: { icon: Info, accent: 'text-indigo-400 border-indigo-500/40' },
}

function ToastViewport() {
  const [items, setItems] = useState<ToastItem[]>([])
  useEffect(() => subscribeToasts(setItems), [])
  if (items.length === 0) return null
  return createPortal(
    <div className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-80 flex-col gap-2" aria-live="polite">
      {items.map((t) => {
        const { icon: Icon, accent } = STYLE[t.kind]
        return (
          <div
            key={t.id}
            role={t.kind === 'error' ? 'alert' : 'status'}
            className={`pointer-events-auto flex items-start gap-2.5 rounded-xl border bg-slate-800 px-3.5 py-2.5 shadow-2xl ${accent}`}
          >
            <Icon size={16} className="mt-0.5 flex-shrink-0" />
            <p className="min-w-0 flex-1 break-words text-xs text-slate-200">{t.message}</p>
            <button
              type="button"
              aria-label="Dismiss"
              onClick={() => dismissToast(t.id)}
              className="flex-shrink-0 rounded p-0.5 text-slate-500 transition-colors hover:bg-slate-700 hover:text-slate-300"
            >
              <X size={12} />
            </button>
          </div>
        )
      })}
    </div>,
    document.body,
  )
}

/** Renders the global toast stack. Mount once near the root. */
export function ToastProvider({ children }: { children?: ReactNode }) {
  return (
    <>
      {children}
      <ToastViewport />
    </>
  )
}

/** `const t = useToast(); t.success('Saved'); t.error('Failed'); t.info('…')` */
export function useToast() {
  return toast
}
