import { useState } from 'react'
import { unlockVault, resetVault } from '../api/client'
import logo from '../assets/logo.png'

interface Props {
  onUnlock: () => void
}

export default function Unlock({ onUnlock }: Props) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [showReset, setShowReset] = useState(false)
  const [resetting, setResetting] = useState(false)

  const handleUnlock = async () => {
    if (!password) return
    setError('')
    setLoading(true)
    try {
      await unlockVault(password)
      onUnlock()
    } catch {
      setError('Incorrect password')
    } finally {
      setLoading(false)
    }
  }

  const handleReset = async () => {
    setResetting(true)
    try {
      await resetVault()
      window.location.reload()
    } catch {
      setResetting(false)
    }
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
            <img src={logo} alt="Mingly" className="h-10 w-10 object-contain" />
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-100">Mingly</h1>
          <p className="mt-1.5 text-sm text-slate-400">Enter your password to unlock</p>
        </div>

        {/* Card */}
        <div className="rounded-2xl border border-slate-700/60 bg-slate-900/80 p-7 shadow-2xl backdrop-blur-sm">
          <h2 className="mb-0.5 text-sm font-semibold text-slate-200">Unlock Mingly</h2>
          <p className="mb-5 text-xs text-slate-500">Your credentials are encrypted with your master password.</p>

          <input
            type="password"
            value={password}
            onChange={(e) => { setPassword(e.target.value); setError('') }}
            onKeyDown={(e) => e.key === 'Enter' && handleUnlock()}
            placeholder="Master password"
            autoFocus
            className="mb-4 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2.5 text-sm text-slate-100 placeholder-slate-500 outline-none transition focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20"
          />

          {error && (
            <p className="mb-3 flex items-center gap-1.5 text-xs text-red-400">
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-red-400" />
              {error}
            </p>
          )}

          <button
            onClick={handleUnlock}
            disabled={!password || loading}
            className="w-full rounded-lg bg-gradient-to-r from-indigo-500 to-purple-500 py-2.5 text-sm font-medium text-white shadow-lg shadow-indigo-500/25 transition hover:from-indigo-400 hover:to-purple-400 disabled:opacity-40 disabled:shadow-none"
          >
            {loading ? 'Unlocking…' : 'Unlock'}
          </button>

          <button
            onClick={() => setShowReset(true)}
            className="mt-3 w-full text-xs text-slate-600 transition hover:text-slate-400"
          >
            Forgot password?
          </button>
        </div>

        <p className="mt-4 text-center text-xs text-slate-600">
          Your data is encrypted at rest in the app data folder
        </p>
      </div>

      {/* Reset confirmation modal */}
      {showReset && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-4 backdrop-blur-sm">
          <div className="w-full max-w-sm rounded-2xl border border-slate-700/60 bg-slate-900 p-6 shadow-2xl">
            <h3 className="mb-2 text-base font-semibold text-slate-100">Reset app data?</h3>
            <p className="mb-5 text-sm text-slate-400">
              This will permanently delete your encrypted vault, including saved credentials
              and Telegram session. You will need to sign in again.
            </p>
            <div className="flex gap-3">
              <button
                onClick={() => setShowReset(false)}
                className="flex-1 rounded-lg border border-slate-700 py-2.5 text-sm text-slate-400 transition hover:bg-slate-800 hover:text-slate-200"
              >
                Cancel
              </button>
              <button
                onClick={handleReset}
                disabled={resetting}
                className="flex-1 rounded-lg bg-red-500/90 py-2.5 text-sm font-medium text-white transition hover:bg-red-500 disabled:opacity-50"
              >
                {resetting ? 'Resetting…' : 'Reset'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
