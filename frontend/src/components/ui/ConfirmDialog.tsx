import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { AlertTriangle, HelpCircle, Loader2 } from 'lucide-react'
import Modal, { modalButton } from './Modal'
import { errorMessage } from '../../lib/toast'

export interface ConfirmOptions {
  title: ReactNode
  /** What will happen / what is affected (e.g. "Deletes 3 messages in Acme <> Us") */
  message?: ReactNode
  confirmLabel?: string
  cancelLabel?: string
  /** Red styling + warning icon */
  destructive?: boolean
  /** Optional checkbox (e.g. "Delete for everyone"); starts at `defaultChecked` on every open */
  checkbox?: { label: ReactNode; defaultChecked?: boolean }
  /**
   * Optional async action run on confirm. The dialog stays open with a
   * spinner (single-flight: the button is disabled), closes on success, and
   * shows the error inline on failure so the user can retry or cancel.
   */
  onConfirm?: (checked: boolean) => Promise<unknown> | unknown
}

export interface ConfirmResult {
  confirmed: boolean
  checked: boolean
}

export interface ConfirmFn {
  /** Resolves true when confirmed (and `onConfirm`, if given, succeeded). */
  (opts: ConfirmOptions & { checkbox?: undefined }): Promise<boolean>
  /** With a checkbox: resolves `{confirmed, checked}`. */
  (opts: ConfirmOptions & { checkbox: NonNullable<ConfirmOptions['checkbox']> }): Promise<ConfirmResult>
}

interface Pending {
  id: number
  opts: ConfirmOptions
  resolve: (r: ConfirmResult) => void
}

const ConfirmContext = createContext<((opts: ConfirmOptions) => Promise<ConfirmResult>) | null>(null)

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<Pending | null>(null)
  const pendingRef = useRef<Pending | null>(null)
  const idRef = useRef(0)

  const open = useCallback((opts: ConfirmOptions) => new Promise<ConfirmResult>((resolve) => {
    // A new confirm supersedes an unanswered one: that one resolves as cancelled
    pendingRef.current?.resolve({ confirmed: false, checked: false })
    const p = { id: ++idRef.current, opts, resolve }
    pendingRef.current = p
    setPending(p)
  }), [])

  const settle = useCallback((p: Pending, result: ConfirmResult) => {
    if (pendingRef.current !== p) return
    pendingRef.current = null
    setPending(null)
    p.resolve(result)
  }, [])

  // Unmount (e.g. logout) resolves any open confirm as cancelled
  useEffect(() => () => pendingRef.current?.resolve({ confirmed: false, checked: false }), [])

  return (
    <ConfirmContext.Provider value={open}>
      {children}
      {pending && (
        // key: per-open state (checkbox, error, busy) always starts fresh
        <ConfirmDialogView key={pending.id} pending={pending} onSettle={settle} />
      )}
    </ConfirmContext.Provider>
  )
}

function ConfirmDialogView({ pending, onSettle }: { pending: Pending; onSettle: (p: Pending, r: ConfirmResult) => void }) {
  const { opts } = pending
  const [checked, setChecked] = useState(!!opts.checkbox?.defaultChecked)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const busyRef = useRef(false)
  const confirmRef = useRef<HTMLButtonElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)

  const cancel = () => {
    if (busyRef.current) return
    onSettle(pending, { confirmed: false, checked })
  }

  const confirm = async () => {
    if (busyRef.current) return
    if (!opts.onConfirm) {
      onSettle(pending, { confirmed: true, checked })
      return
    }
    busyRef.current = true
    setBusy(true)
    setError(null)
    try {
      await opts.onConfirm(checked)
      onSettle(pending, { confirmed: true, checked })
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      busyRef.current = false
      setBusy(false)
    }
  }

  const destructive = !!opts.destructive
  return (
    <Modal
      open
      onClose={cancel}
      size="sm"
      closeOnBackdrop={!busy}
      closeOnEscape={!busy}
      // Destructive: focus Cancel so a stray Enter doesn't destroy anything
      initialFocusRef={destructive ? cancelRef : confirmRef}
      ariaLabel={typeof opts.title === 'string' ? opts.title : 'Confirm'}
      footer={
        <>
          <button ref={cancelRef} type="button" onClick={cancel} disabled={busy} className={modalButton.secondary}>
            {opts.cancelLabel ?? 'Cancel'}
          </button>
          <button
            ref={confirmRef}
            type="button"
            onClick={confirm}
            disabled={busy}
            className={`${destructive ? modalButton.danger : modalButton.primary} inline-flex items-center gap-1.5`}
          >
            {busy && <Loader2 size={12} className="animate-spin" />}
            {opts.confirmLabel ?? (destructive ? 'Delete' : 'Confirm')}
          </button>
        </>
      }
    >
      <div className="px-5 py-4">
        <div className="flex items-start gap-3">
          <div className={`flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full ${destructive ? 'bg-red-500/20' : 'bg-indigo-500/20'}`}>
            {destructive
              ? <AlertTriangle size={16} className="text-red-400" />
              : <HelpCircle size={16} className="text-indigo-400" />}
          </div>
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-gray-100">{opts.title}</h3>
            {opts.message != null && (
              <div className="mt-1 text-xs text-slate-400">{opts.message}</div>
            )}
          </div>
        </div>
        {opts.checkbox && (
          <label className="mt-4 flex cursor-pointer items-center gap-2">
            <input
              type="checkbox"
              checked={checked}
              disabled={busy}
              onChange={(e) => setChecked(e.target.checked)}
              className="h-3.5 w-3.5 rounded border-slate-500 bg-slate-700 text-indigo-500 focus:ring-indigo-500/30"
            />
            <span className="text-xs text-slate-300">{opts.checkbox.label}</span>
          </label>
        )}
        {error && (
          <p role="alert" className="mt-3 rounded-lg bg-red-500/10 px-3 py-2 text-xs text-red-400">{error}</p>
        )}
      </div>
    </Modal>
  )
}

/**
 * Promise-based confirm dialog (replaces window.confirm and ad-hoc dialogs).
 *
 *   const confirm = useConfirm()
 *   if (!(await confirm({ title: 'Delete org?', message: 'Removes 4 positions', destructive: true }))) return
 */
export function useConfirm(): ConfirmFn {
  const open = useContext(ConfirmContext)
  if (!open) throw new Error('useConfirm must be used inside <ConfirmProvider>')
  return useCallback(((opts: ConfirmOptions) =>
    open(opts).then((r) => (opts.checkbox ? r : r.confirmed))) as ConfirmFn, [open])
}
