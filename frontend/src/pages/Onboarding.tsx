import { useState } from 'react'
import { Check } from 'lucide-react'
import logo from '../assets/logo.png'
import { initVault } from '../api/client'
import type { ConnectableChannel } from '../api/accounts'
import AuthFlow from '../components/channels/AuthFlow'
import ChannelIcon from '../components/channels/ChannelIcon'
import ChannelSetupNote from '../components/channels/ChannelSetupNote'
import { useAccounts } from '../hooks/useAccounts'
import { useAsyncAction } from '../hooks/useAsyncAction'
import { errorMessage } from '../lib/toast'

type Step = 'set-password' | 'connect'

interface Props {
  /** Vault created (and accounts connected, or skipped) */
  onComplete: () => void
}

/**
 * First run: set the master password that creates the vault, then optionally
 * connect accounts through the same generic sign-in as Settings →
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

          {step === 'connect' && <ConnectAccounts onComplete={onComplete} />}
        </div>

        <p className="mt-4 text-center text-xs text-slate-600">
          Account credentials and sessions are encrypted with your master password. Messages are stored unencrypted in a local database.
        </p>
      </div>
    </div>
  )
}

/**
 * Connect one or more accounts, or skip; more live in Settings → Connections.
 * A channel's setup (Signal's download) runs in the background, so another
 * account can be connected meanwhile; the list shows how far it got.
 */
function ConnectAccounts({ onComplete }: { onComplete: () => void }) {
  const { accounts, channels, loaded, error, refresh } = useAccounts()
  const [chosen, setChosen] = useState<ConnectableChannel | null>(null)

  if (chosen) {
    return (
      <AuthFlow
        key={chosen.channel}
        accountKey={chosen.channel}
        onDone={() => void refresh()}
        // Cancel, the done screen and leaving a background setup all go back to the list
        onClose={() => { setChosen(null); void refresh() }}
        doneLabel="Done"
        backgroundLabel="Connect another account"
      />
    )
  }

  const connected = accounts.filter((a) => a.configured)
  const connectedOn = (channel: string) => connected.filter((a) => a.channel === channel)

  return (
    <>
      <h2 className="mb-0.5 text-sm font-semibold text-slate-200">Connect your accounts</h2>
      <p className="mb-5 text-xs text-slate-500">
        Choose the messaging accounts to sync — as many as you like. You can add more, or do this later, in Settings → Connections.
      </p>
      {error != null && channels.length === 0 ? (
        <div className="mb-4">
          <p role="alert" className="text-xs text-red-400">Couldn't load the available channels: {errorMessage(error)}</p>
          <button
            onClick={() => void refresh()}
            className="mt-2 rounded-lg border border-slate-700 px-3 py-1.5 text-xs text-slate-300 transition hover:bg-slate-800"
          >
            Retry
          </button>
        </div>
      ) : !loaded ? (
        <p className="mb-4 text-xs text-slate-500">Loading…</p>
      ) : (
        <div className="mb-4 flex flex-col gap-2">
          {channels.map((c) => {
            const on = connectedOn(c.channel)
            // A single-account channel is done once connected; a multi-account one can add another
            const full = on.length > 0 && !c.multi_account
            return (
              <button
                key={c.channel}
                onClick={() => setChosen(c)}
                disabled={full}
                className="flex items-center gap-3 rounded-lg border border-slate-700 bg-slate-800 px-3 py-2.5 text-left text-sm text-slate-200 transition enabled:hover:border-indigo-500/60 enabled:hover:bg-slate-800/60 disabled:cursor-default"
              >
                <span aria-hidden><ChannelIcon channel={c.channel} size={16} className="text-slate-300" /></span>
                <span className="flex-shrink-0">{c.display_name}</span>
                <span className="ml-auto flex min-w-0 items-center justify-end">
                  {on.length > 0 ? (
                    <span className="flex items-center gap-1 truncate text-[11px] text-emerald-400">
                      <Check size={12} aria-hidden />
                      {on.length === 1 ? on[0].label || 'Connected' : `${on.length} connected`}
                    </span>
                  ) : (
                    <ChannelSetupNote setup={c.setup} />
                  )}
                </span>
              </button>
            )
          })}
        </div>
      )}
      {connected.length > 0 ? (
        <button
          onClick={onComplete}
          className="w-full rounded-lg bg-gradient-to-r from-indigo-500 to-purple-500 py-2.5 text-sm font-medium text-white shadow-lg shadow-indigo-500/25 transition hover:from-indigo-400 hover:to-purple-400"
        >
          Continue
        </button>
      ) : (
        <button onClick={onComplete} className="w-full text-xs text-slate-500 transition hover:text-slate-300">
          Skip for now
        </button>
      )}
    </>
  )
}
