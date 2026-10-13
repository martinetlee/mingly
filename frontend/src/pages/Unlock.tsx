import { useRef, useState } from 'react'
import { AlertTriangle, Loader2 } from 'lucide-react'
import { unlockVault, resetVault, ApiError } from '../api/client'
import { Modal, modalButton } from '../components/ui'
import { useAsyncAction } from '../hooks/useAsyncAction'
import { errorMessage } from '../lib/toast'
import logo from '../assets/logo.png'

interface Props {
  onUnlock: () => void
  /** After a successful vault reset (default: reload, which lands on "create vault") */
  onReset?: () => void
}

type UnlockFailure =
  | { kind: 'wrong_password'; message: string }
  | { kind: 'corrupt'; message: string }
  | { kind: 'unreachable'; message: string }
  | { kind: 'other'; message: string }

/** Map an unlock failure to what the user can do about it. */
export function classifyUnlockError(e: unknown): UnlockFailure {
  if (e instanceof ApiError) {
    if (e.serverCode === 'vault_corrupt') {
      return { kind: 'corrupt', message: e.message || 'The vault file is damaged and cannot be opened.' }
    }
    if (e.serverCode === 'vault_wrong_password' || e.status === 400) {
      return { kind: 'wrong_password', message: 'Incorrect password' }
    }
    if (e.code === 'network' || e.code === 'html_response' || e.status >= 500) {
      return {
        kind: 'unreachable',
        message: "Can't reach the Mingly backend. Make sure the app is running, then try again.",
      }
    }
    return { kind: 'other', message: e.message }
  }
  return { kind: 'other', message: errorMessage(e) }
}

export const RESET_CONFIRM_WORD = 'RESET'

export default function Unlock({ onUnlock, onReset = () => window.location.reload() }: Props) {
  const [password, setPassword] = useState('')
  const [showReset, setShowReset] = useState(false)

  // Single-flight: Enter and the button share one in-flight unlock
  const unlock = useAsyncAction(async () => {
    await unlockVault(password)
    onUnlock()
  })
  const failure = unlock.error ? classifyUnlockError(unlock.error) : null

  const submit = () => {
    if (!password || unlock.pending) return
    void unlock.run()
  }

  return (
    <div className="flex h-screen items-center justify-center bg-slate-950 overflow-hidden relative">
      {/* Ambient glow */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="absolute -top-32 left-1/2 -translate-x-1/2 h-96 w-96 rounded-full bg-indigo-600/20 blur-3xl" />
        <div className="absolute -bottom-32 left-1/2 -translate-x-1/2 h-96 w-96 rounded-full bg-purple-600/15 blur-3xl" />
      </div>

      <div className="relative w-full max-w-sm px-4">
        {/* Logo + title */}
        <div className="mb-8 text-center">
          <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-2xl bg-gradient-to-br from-indigo-500/20 to-purple-500/20 ring-1 ring-indigo-500/30">
            <img src={logo} alt="Common Defense" className="h-10 w-10 object-contain" />
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-100">Common Defense</h1>
          <p className="mt-1.5 text-sm text-slate-400">Enter your password to unlock</p>
        </div>

        {/* Card */}
        <div className="rounded-2xl border border-slate-700/60 bg-slate-900/80 p-7 shadow-2xl backdrop-blur-sm">
          <h2 className="mb-0.5 text-sm font-semibold text-slate-200">Unlock Common Defense</h2>
          <p className="mb-5 text-xs text-slate-500">Your account sign-ins and API keys are encrypted with your master password.</p>

          <form onSubmit={(e) => { e.preventDefault(); submit() }}>
            <input
              type="password"
              value={password}
              onChange={(e) => { setPassword(e.target.value); unlock.reset() }}
              placeholder="Master password"
              aria-label="Master password"
              autoFocus
              className="mb-4 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2.5 text-sm text-slate-100 placeholder-slate-500 outline-none transition focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20"
            />

            {failure && failure.kind !== 'corrupt' && (
              <p role="alert" className="mb-3 flex items-center gap-1.5 text-xs text-red-400">
                <span className="inline-block h-1.5 w-1.5 flex-shrink-0 rounded-full bg-red-400" />
                {failure.message}
              </p>
            )}

            {failure?.kind === 'corrupt' && (
              <div role="alert" className="mb-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2.5 text-xs text-red-300">
                <p className="flex items-start gap-1.5">
                  <AlertTriangle size={14} className="mt-px flex-shrink-0 text-red-400" />
                  <span>{failure.message}</span>
                </p>
                <button
                  type="button"
                  onClick={() => setShowReset(true)}
                  className="mt-2 rounded-md bg-red-500/20 px-2.5 py-1 font-medium text-red-300 transition hover:bg-red-500/30"
                >
                  Reset vault…
                </button>
              </div>
            )}

            <button
              type="submit"
              disabled={!password || unlock.pending}
              className="w-full rounded-lg bg-gradient-to-r from-indigo-500 to-purple-500 py-2.5 text-sm font-medium text-white shadow-lg shadow-indigo-500/25 transition hover:from-indigo-400 hover:to-purple-400 disabled:opacity-40 disabled:shadow-none"
            >
              {unlock.pending ? 'Unlocking…' : 'Unlock'}
            </button>
          </form>

          <button
            type="button"
            onClick={() => setShowReset(true)}
            className="mt-3 w-full text-xs text-slate-600 transition hover:text-slate-400"
          >
            Forgot password?
          </button>
        </div>

        <p className="mt-4 text-center text-xs text-slate-600">
          Only credentials are encrypted. Messages and other app data are stored unencrypted in a local database in the app data folder.
        </p>
      </div>

      {showReset && <ResetVaultDialog onClose={() => setShowReset(false)} onReset={onReset} />}
    </div>
  )
}

/** Typed-confirmation dialog for destroying the vault (backend requires `{confirm: "RESET"}`). */
function ResetVaultDialog({ onClose, onReset }: { onClose: () => void; onReset: () => void }) {
  const [typed, setTyped] = useState('')
  const cancelRef = useRef<HTMLButtonElement>(null)
  const reset = useAsyncAction(async () => {
    await resetVault(typed)
    onReset()
  })
  const armed = typed === RESET_CONFIRM_WORD

  const submit = () => {
    if (!armed || reset.pending) return
    void reset.run()
  }

  const close = () => { if (!reset.pending) onClose() }

  return (
    <Modal
      open
      onClose={close}
      size="sm"
      closeOnBackdrop={!reset.pending}
      closeOnEscape={!reset.pending}
      initialFocusRef={cancelRef}
      ariaLabel="Reset vault"
      footer={
        <>
          <button ref={cancelRef} type="button" onClick={close} disabled={reset.pending} className={modalButton.secondary}>
            Cancel
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={!armed || reset.pending}
            className={`${modalButton.danger} inline-flex items-center gap-1.5`}
          >
            {reset.pending && <Loader2 size={12} className="animate-spin" />}
            Reset vault
          </button>
        </>
      }
    >
      <div className="px-5 py-4">
        <div className="flex items-start gap-3">
          <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-red-500/20">
            <AlertTriangle size={16} className="text-red-400" />
          </div>
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-gray-100">Reset the vault?</h3>
            <div className="mt-1 text-xs text-slate-400">
              This permanently deletes your encrypted vault: the master password, account sign-ins (such as
              Telegram sessions and Gmail tokens) and stored API keys. You will set a new password and sign in
              to those accounts again.
            </div>
          </div>
        </div>
        <label className="mt-4 block text-xs text-slate-300">
          Type <span className="font-mono font-semibold text-red-300">{RESET_CONFIRM_WORD}</span> to confirm
          <input
            value={typed}
            onChange={(e) => { setTyped(e.target.value); reset.reset() }}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); submit() } }}
            disabled={reset.pending}
            autoComplete="off"
            spellCheck={false}
            aria-label={`Type ${RESET_CONFIRM_WORD} to confirm`}
            className="mt-1.5 w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 font-mono text-sm text-slate-100 outline-none focus:border-red-500 focus:ring-1 focus:ring-red-500/40"
          />
        </label>
        {reset.error != null && (
          <p role="alert" className="mt-3 rounded-lg bg-red-500/10 px-3 py-2 text-xs text-red-400">
            {classifyUnlockError(reset.error).kind === 'unreachable'
              ? classifyUnlockError(reset.error).message
              : `Reset failed: ${errorMessage(reset.error)}`}
          </p>
        )}
      </div>
    </Modal>
  )
}
