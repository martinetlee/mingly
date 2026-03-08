import { useState } from 'react'
import { unlockVault, resetVault } from '../api/client'

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
    <div className="flex h-screen items-center justify-center bg-gray-50">
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="mb-8 text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-2xl bg-blue-500 text-white font-bold text-xl">
            M
          </div>
          <h1 className="text-2xl font-semibold text-gray-900">Mingly</h1>
          <p className="mt-1 text-sm text-gray-500">Enter your password to unlock</p>
        </div>

        <div className="rounded-2xl bg-white p-8 shadow-sm border border-gray-100">
          <h2 className="mb-1 text-base font-medium text-gray-900">Unlock Mingly</h2>
          <p className="mb-5 text-sm text-gray-500">Your credentials are encrypted with your master password.</p>

          <input
            type="password"
            value={password}
            onChange={(e) => { setPassword(e.target.value); setError('') }}
            onKeyDown={(e) => e.key === 'Enter' && handleUnlock()}
            placeholder="Master password"
            autoFocus
            className="mb-4 w-full rounded-lg border border-gray-200 px-3 py-2.5 text-sm outline-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100"
          />

          {error && <p className="mb-3 text-sm text-red-500">{error}</p>}

          <button
            onClick={handleUnlock}
            disabled={!password || loading}
            className="w-full rounded-lg bg-blue-500 py-2.5 text-sm font-medium text-white transition-colors hover:bg-blue-600 disabled:opacity-50"
          >
            {loading ? 'Unlocking…' : 'Unlock'}
          </button>

          <button
            onClick={() => setShowReset(true)}
            className="mt-3 w-full text-xs text-gray-400 hover:text-gray-600"
          >
            Forgot password?
          </button>
        </div>

        <p className="mt-4 text-center text-xs text-gray-400">
          Your data is encrypted at rest in the app data folder
        </p>
      </div>

      {/* Reset confirmation modal */}
      {showReset && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-sm rounded-2xl bg-white p-6 shadow-xl">
            <h3 className="mb-2 text-base font-semibold text-gray-900">Reset app data?</h3>
            <p className="mb-5 text-sm text-gray-500">
              This will permanently delete your encrypted vault, including saved credentials
              and Telegram session. You will need to sign in again.
            </p>
            <div className="flex gap-3">
              <button
                onClick={() => setShowReset(false)}
                className="flex-1 rounded-lg border border-gray-200 py-2.5 text-sm text-gray-600 hover:bg-gray-50"
              >
                Cancel
              </button>
              <button
                onClick={handleReset}
                disabled={resetting}
                className="flex-1 rounded-lg bg-red-500 py-2.5 text-sm font-medium text-white hover:bg-red-600 disabled:opacity-50"
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
