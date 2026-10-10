import { useEffect, useState } from 'react'
import logo from '../assets/logo.png'
import { initVault } from '../api/client'
import { getAccounts } from '../api/accounts'
import type { ConnectableChannel } from '../api/accounts'
import AuthFlow from '../components/channels/AuthFlow'
import ChannelIcon from '../components/channels/ChannelIcon'
import { useAsyncAction } from '../hooks/useAsyncAction'
import { errorMessage } from '../lib/toast'

type Step = 'set-password' | 'connect'

interface Props {
  /** Vault created (and optionally a first account connected or skipped) */
  onComplete: () => void
}

/**
 * First run: set the master password that creates the vault, then optionally
 * connect a first account through the same generic sign-in as Settings →
 * Connections. Accounts can always be added later there.
 */
export default function Onboarding({ onComplete }: Props) {
  const [step, setStep] = useState<Step>('set-password')

  const [masterPwd, setMasterPwd] = useState('')
  const [masterPwdConfirm, setMasterPwdConfirm] = useState('')
  const [pwdValidation, setPwdValidation] = useState('')

  const setPasswordAction = useAsyncAction(async () => {
    await initVault(masterPwd)
    setStep('connect')
  })

  const pwdLoading = setPasswordAction.pending
  const pwdError = pwdValidation || (setPasswordAction.error ? errorMessage(setPasswordAction.error) : '')

  // Every submit path (button or Enter) goes through these guards
  const handleSetPassword = () => {
    if (!masterPwd || !masterPwdConfirm || pwdLoading) return
    if (masterPwd !== masterPwdConfirm) {
      setPwdValidation("Passwords don't match")
      return
    }
    if (masterPwd.length < 6) {
      setPwdValidation('Password must be at least 6 characters')
      return
    }
    setPwdValidation('')
    void setPasswordAction.run()
  }

  const indicatorSteps: Step[] = ['set-password', 'connect']

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
          <p className="mt-1.5 text-sm text-slate-400">Your conversations, intelligently organised</p>
        </div>

        {/* Step indicators */}
        <div className="mb-6 flex items-center justify-center gap-2">
          {indicatorSteps.map((s, i) => (
            <div key={s} className={`h-2 w-2 rounded-full transition-colors ${
              step === s ? 'bg-indigo-500' : indicatorSteps.indexOf(step) > i ? 'bg-indigo-500/40' : 'bg-slate-700'
            }`} />
          ))}
        </div>

        {/* Card */}
        <div className="rounded-2xl border border-slate-700/60 bg-slate-900/80 p-7 shadow-2xl backdrop-blur-sm">
          {step === 'set-password' && (
            <>
              <h2 className="mb-0.5 text-sm font-semibold text-slate-200">Protect your data</h2>
              <p className="mb-5 text-xs text-slate-500">
                Set a master password to encrypt your account credentials and sessions locally. You'll enter this each time you open Common Defense.
              </p>

              <div className="mb-4">
                <label className="mb-1 block text-xs font-medium text-slate-400">Master password</label>
                <input
                  type="password"
                  value={masterPwd}
                  onChange={(e) => { setMasterPwd(e.target.value); setPwdValidation(''); setPasswordAction.reset() }}
                  placeholder="At least 6 characters"
                  autoFocus
                  className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2.5 text-sm text-slate-100 placeholder-slate-500 outline-none transition focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20"
                />
              </div>
              <div className="mb-4">
                <label className="mb-1 block text-xs font-medium text-slate-400">Confirm password</label>
                <input
                  type="password"
                  value={masterPwdConfirm}
                  onChange={(e) => { setMasterPwdConfirm(e.target.value); setPwdValidation(''); setPasswordAction.reset() }}
                  onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); handleSetPassword() } }}
                  placeholder="Repeat password"
                  className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2.5 text-sm text-slate-100 placeholder-slate-500 outline-none transition focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20"
                />
              </div>

              {pwdError && (
                <p className="mb-3 flex items-center gap-1.5 text-xs text-red-400">
                  <span className="inline-block h-1.5 w-1.5 rounded-full bg-red-400" />
                  {pwdError}
                </p>
              )}
              <button
                onClick={handleSetPassword}
                disabled={!masterPwd || !masterPwdConfirm || pwdLoading}
                className="w-full rounded-lg bg-gradient-to-r from-indigo-500 to-purple-500 py-2.5 text-sm font-medium text-white shadow-lg shadow-indigo-500/25 transition hover:from-indigo-400 hover:to-purple-400 disabled:opacity-40 disabled:shadow-none"
              >
                {pwdLoading ? 'Setting up…' : 'Continue'}
              </button>
            </>
          )}

          {step === 'connect' && <ConnectFirstAccount onComplete={onComplete} />}
        </div>

        <p className="mt-4 text-center text-xs text-slate-600">
          Account credentials and sessions are encrypted with your master password. Messages are stored unencrypted in a local database.
        </p>
      </div>
    </div>
  )
}

/** Pick a channel and sign in, or skip; more accounts live in Settings → Connections. */
function ConnectFirstAccount({ onComplete }: { onComplete: () => void }) {
  const [channels, setChannels] = useState<ConnectableChannel[] | null>(null)
  const [loadError, setLoadError] = useState('')
  const [chosen, setChosen] = useState<ConnectableChannel | null>(null)
  const [connected, setConnected] = useState(false)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let cancelled = false
    setLoadError('')
    getAccounts()
      .then((r) => { if (!cancelled) setChannels(r.channels) })
      .catch((e) => { if (!cancelled) setLoadError(errorMessage(e)) })
    return () => { cancelled = true }
  }, [attempt])

  if (chosen) {
    return (
      <AuthFlow
        key={chosen.channel}
        accountKey={chosen.channel}
        onDone={() => setConnected(true)}
        // Cancel goes back to the channel list; the done screen's button continues
        onClose={() => (connected ? onComplete() : setChosen(null))}
        doneLabel="Continue"
      />
    )
  }

  return (
    <>
      <h2 className="mb-0.5 text-sm font-semibold text-slate-200">Connect an account</h2>
      <p className="mb-5 text-xs text-slate-500">
        Choose a messaging account to sync. You can add more, or do this later, in Settings → Connections.
      </p>
      {loadError ? (
        <div className="mb-4">
          <p role="alert" className="text-xs text-red-400">Couldn't load the available channels: {loadError}</p>
          <button
            onClick={() => setAttempt((n) => n + 1)}
            className="mt-2 rounded-lg border border-slate-700 px-3 py-1.5 text-xs text-slate-300 transition hover:bg-slate-800"
          >
            Retry
          </button>
        </div>
      ) : channels === null ? (
        <p className="mb-4 text-xs text-slate-500">Loading…</p>
      ) : (
        <div className="mb-4 flex flex-col gap-2">
          {channels.map((c) => (
            <button
              key={c.channel}
              onClick={() => setChosen(c)}
              className="flex items-center gap-3 rounded-lg border border-slate-700 bg-slate-800 px-3 py-2.5 text-sm text-slate-200 transition hover:border-indigo-500/60 hover:bg-slate-800/60"
            >
              <span aria-hidden><ChannelIcon channel={c.channel} size={16} className="text-slate-300" /></span>
              {c.display_name}
            </button>
          ))}
        </div>
      )}
      <button onClick={onComplete} className="w-full text-xs text-slate-500 transition hover:text-slate-300">
        Skip for now
      </button>
    </>
  )
}
