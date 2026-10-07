import { useState, useEffect, useCallback } from 'react'
import logo from '../assets/logo.png'
import {
  hasCredentials, setupCredentials, resolve1Password,
  sendCode, verifyCode, verify2FA,
  initVault,
} from '../api/client'
import { useAsyncAction } from '../hooks/useAsyncAction'
import { errorMessage } from '../lib/toast'

type Step = 'set-password' | 'credentials' | 'phone' | 'code' | 'twofa' | 'done'

interface Props {
  onComplete: () => void
  /** If true, vault doesn't exist yet — show password setup as first step */
  needsVaultSetup?: boolean
}

function is1PasswordRef(value: string) {
  return value.trim().startsWith('op://')
}

interface SecretFieldProps {
  label: string
  hint: string
  value: string
  onChange: (v: string) => void
  type?: 'text' | 'password'
  placeholder?: string
}

function SecretField({ label, hint, value, onChange, type = 'text', placeholder }: SecretFieldProps) {
  const [resolved, setResolved] = useState(false)
  const isOpRef = is1PasswordRef(value)

  const resolve = useAsyncAction(async () => {
    setResolved(false)
    const res = await resolve1Password(value.trim())
    onChange(res.value)
    setResolved(true)
  })
  const resolving = resolve.pending
  const resolveError = resolve.error ? errorMessage(resolve.error) : ''
  const handleResolve = () => { void resolve.run() }

  return (
    <div className="mb-4">
      <label className="mb-1 block text-xs font-medium text-slate-400">{label}</label>
      <div className="flex gap-2">
        <input
          type={type}
          value={value}
          onChange={(e) => { onChange(e.target.value); setResolved(false); resolve.reset() }}
          placeholder={placeholder}
          className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2.5 text-sm text-slate-100 placeholder-slate-500 outline-none transition focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20 font-mono"
          spellCheck={false}
          autoComplete="off"
        />
        {isOpRef && (
          <button
            onClick={handleResolve}
            disabled={resolving}
            className="shrink-0 rounded-lg border border-indigo-500/40 bg-indigo-500/10 px-3 py-2 text-xs font-medium text-indigo-400 hover:bg-indigo-500/20 disabled:opacity-50 transition"
          >
            {resolving ? '…' : resolved ? '✓' : 'Resolve'}
          </button>
        )}
      </div>
      {resolveError && <p className="mt-1 text-xs text-red-400">{resolveError}</p>}
      {resolved && <p className="mt-1 text-xs text-emerald-400">Resolved from 1Password</p>}
      <p className="mt-1 text-xs text-slate-600">{hint}</p>
    </div>
  )
}

export default function Onboarding({ onComplete, needsVaultSetup = false }: Props) {
  const [step, setStep] = useState<Step | null>(null)

  // Master password setup
  const [masterPwd, setMasterPwd] = useState('')
  const [masterPwdConfirm, setMasterPwdConfirm] = useState('')
  const [pwdValidation, setPwdValidation] = useState('')

  // API credentials
  const [apiId, setApiId] = useState('')
  const [apiHash, setApiHash] = useState('')

  // Telegram login
  const [phone, setPhone] = useState('')
  const [phoneCodeHash, setPhoneCodeHash] = useState('')
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')

  // Initial "which step?" probe; a failure is shown with Retry instead of guessing a step
  const [probeError, setProbeError] = useState('')
  const probe = useCallback(() => {
    setProbeError('')
    hasCredentials()
      .then(({ has_credentials }) => setStep(has_credentials ? 'phone' : 'credentials'))
      .catch((e: unknown) => setProbeError(errorMessage(e)))
  }, [])

  useEffect(() => {
    if (needsVaultSetup) {
      setStep('set-password')
      return
    }
    probe()
  }, [needsVaultSetup, probe])

  // ── Step actions (single-flight: Enter and the button share one request) ──

  const setPasswordAction = useAsyncAction(async () => {
    await initVault(masterPwd)
    setStep('credentials')
  })
  const credentialsAction = useAsyncAction(async () => {
    await setupCredentials(apiId.trim(), apiHash.trim())
    setStep('phone')
  })
  const sendCodeAction = useAsyncAction(async () => {
    const res = await sendCode(phone.trim())
    setPhoneCodeHash(res.phone_code_hash)
    setStep('code')
  })
  const verifyCodeAction = useAsyncAction(async () => {
    const res = await verifyCode(phone.trim(), code.trim(), phoneCodeHash)
    if (res.requires_2fa) {
      setStep('twofa')
    } else {
      setStep('done')
      setTimeout(onComplete, 800)
    }
  })
  const verify2FAAction = useAsyncAction(async () => {
    await verify2FA(password)
    setStep('done')
    setTimeout(onComplete, 800)
  })

  const pwdLoading = setPasswordAction.pending
  const pwdError = pwdValidation || (setPasswordAction.error ? errorMessage(setPasswordAction.error) : '')
  const credLoading = credentialsAction.pending
  const credError = credentialsAction.error ? errorMessage(credentialsAction.error) : ''
  const loginActions = [sendCodeAction, verifyCodeAction, verify2FAAction]
  const loading = loginActions.some((a) => a.pending)
  const loginError = loginActions.find((a) => a.error)?.error
  const error = loginError ? errorMessage(loginError) : ''
  const clearLoginErrors = () => loginActions.forEach((a) => a.reset())

  const credsBlocked = !apiId.trim() || !apiHash.trim() || is1PasswordRef(apiId) || is1PasswordRef(apiHash)

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

  const handleSaveCredentials = () => {
    if (credsBlocked || credLoading) return
    void credentialsAction.run()
  }

  const handleSendCode = () => {
    if (!phone.trim() || loading) return
    clearLoginErrors()
    void sendCodeAction.run()
  }

  const handleVerifyCode = () => {
    if (!code.trim() || loading) return
    clearLoginErrors()
    void verifyCodeAction.run()
  }

  const handleVerify2FA = () => {
    if (!password || loading) return
    clearLoginErrors()
    void verify2FAAction.run()
  }

  // ── Step indicator ────────────────────────────────────────────────────────

  const indicatorSteps: Step[] = needsVaultSetup
    ? ['set-password', 'credentials', 'phone', 'code']
    : ['credentials', 'phone', 'code']

  const stepOrder: Step[] = ['set-password', 'credentials', 'phone', 'code', 'twofa', 'done']

  if (step === null) {
    return (
      <div className="flex h-screen items-center justify-center bg-slate-950">
        {probeError ? (
          <div className="max-w-sm px-4 text-center">
            <p role="alert" className="text-sm text-red-400">Couldn't check your Telegram setup: {probeError}</p>
            <button
              onClick={probe}
              className="mt-3 rounded-lg border border-slate-700 px-4 py-2 text-xs text-slate-300 transition hover:bg-slate-800"
            >
              Retry
            </button>
          </div>
        ) : (
          <div className="text-slate-500 text-sm">Loading…</div>
        )}
      </div>
    )
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
          <p className="mt-1.5 text-sm text-slate-400">Your Telegram, intelligently organised</p>
        </div>

        {/* Step indicators */}
        <div className="mb-6 flex items-center justify-center gap-2">
          {indicatorSteps.map((s) => (
            <div key={s} className="flex items-center gap-2">
              <div className={`h-2 w-2 rounded-full transition-colors ${
                step === s ? 'bg-indigo-500' :
                stepOrder.indexOf(step) > stepOrder.indexOf(s)
                  ? 'bg-indigo-500/40' : 'bg-slate-700'
              }`} />
            </div>
          ))}
        </div>

        {/* Card */}
        <div className="rounded-2xl border border-slate-700/60 bg-slate-900/80 p-7 shadow-2xl backdrop-blur-sm">

          {/* Step 0: Set master password */}
          {step === 'set-password' && (
            <>
              <h2 className="mb-0.5 text-sm font-semibold text-slate-200">Protect your data</h2>
              <p className="mb-5 text-xs text-slate-500">
                Set a master password to encrypt your Telegram credentials and session locally. You'll enter this each time you open Common Defense.
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

          {/* Step 1: Telegram API credentials */}
          {step === 'credentials' && (
            <>
              <h2 className="mb-0.5 text-sm font-semibold text-slate-200">Connect to Telegram</h2>
              <p className="mb-5 text-xs text-slate-500">
                Get your free API credentials at{' '}
                <span className="font-mono text-indigo-400">my.telegram.org</span>{' '}
                → API development tools.
              </p>

              <SecretField
                label="API ID"
                hint='Numeric ID, e.g. 12345678. Supports 1Password references (op://…)'
                value={apiId}
                onChange={setApiId}
                placeholder="12345678 or op://vault/item/field"
              />
              <SecretField
                label="API Hash"
                hint='32-character hex string. Supports 1Password references (op://…)'
                value={apiHash}
                onChange={setApiHash}
                type="password"
                placeholder="a1b2c3… or op://vault/item/field"
              />

              {credError && (
                <p className="mb-3 flex items-center gap-1.5 text-xs text-red-400">
                  <span className="inline-block h-1.5 w-1.5 rounded-full bg-red-400" />
                  {credError}
                </p>
              )}
              <button
                onClick={handleSaveCredentials}
                disabled={credsBlocked || credLoading}
                className="w-full rounded-lg bg-gradient-to-r from-indigo-500 to-purple-500 py-2.5 text-sm font-medium text-white shadow-lg shadow-indigo-500/25 transition hover:from-indigo-400 hover:to-purple-400 disabled:opacity-40 disabled:shadow-none"
              >
                {credLoading ? 'Saving…' : 'Continue'}
              </button>
              {(is1PasswordRef(apiId) || is1PasswordRef(apiHash)) && (
                <p className="mt-2 text-center text-xs text-amber-400">
                  Resolve all 1Password references before continuing
                </p>
              )}
            </>
          )}

          {/* Step 2: Phone number */}
          {step === 'phone' && (
            <>
              <h2 className="mb-0.5 text-sm font-semibold text-slate-200">Sign in with Telegram</h2>
              <p className="mb-5 text-xs text-slate-500">Enter your phone number with country code</p>
              <input
                type="tel"
                value={phone}
                onChange={(e) => { setPhone(e.target.value); clearLoginErrors() }}
                onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); handleSendCode() } }}
                placeholder="+1 234 567 8900"
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
                onClick={handleSendCode}
                disabled={!phone.trim() || loading}
                className="w-full rounded-lg bg-gradient-to-r from-indigo-500 to-purple-500 py-2.5 text-sm font-medium text-white shadow-lg shadow-indigo-500/25 transition hover:from-indigo-400 hover:to-purple-400 disabled:opacity-40 disabled:shadow-none"
              >
                {loading ? 'Sending…' : 'Send Code'}
              </button>
              <button
                onClick={() => setStep('credentials')}
                className="mt-3 w-full text-xs text-slate-600 transition hover:text-slate-400"
              >
                ← Change API credentials
              </button>
            </>
          )}

          {/* Step 3: OTP */}
          {step === 'code' && (
            <>
              <h2 className="mb-0.5 text-sm font-semibold text-slate-200">Enter the code</h2>
              <p className="mb-5 text-xs text-slate-500">
                We sent a code to <span className="text-slate-300 font-medium">{phone}</span> via Telegram
              </p>
              <input
                type="text"
                inputMode="numeric"
                value={code}
                onChange={(e) => { setCode(e.target.value); clearLoginErrors() }}
                onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); handleVerifyCode() } }}
                placeholder="12345"
                autoFocus
                maxLength={6}
                className="mb-4 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2.5 text-sm text-slate-100 placeholder-slate-500 tracking-widest outline-none transition focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20"
              />
              {error && (
                <p className="mb-3 flex items-center gap-1.5 text-xs text-red-400">
                  <span className="inline-block h-1.5 w-1.5 rounded-full bg-red-400" />
                  {error}
                </p>
              )}
              <button
                onClick={handleVerifyCode}
                disabled={!code.trim() || loading}
                className="w-full rounded-lg bg-gradient-to-r from-indigo-500 to-purple-500 py-2.5 text-sm font-medium text-white shadow-lg shadow-indigo-500/25 transition hover:from-indigo-400 hover:to-purple-400 disabled:opacity-40 disabled:shadow-none"
              >
                {loading ? 'Verifying…' : 'Verify Code'}
              </button>
              <button
                onClick={() => { setStep('phone'); setCode(''); clearLoginErrors() }}
                className="mt-3 w-full text-xs text-slate-600 transition hover:text-slate-400"
              >
                ← Change number
              </button>
            </>
          )}

          {/* Step 4: 2FA (conditional) */}
          {step === 'twofa' && (
            <>
              <h2 className="mb-0.5 text-sm font-semibold text-slate-200">Two-step verification</h2>
              <p className="mb-5 text-xs text-slate-500">Enter your Telegram 2FA password</p>
              <input
                type="password"
                value={password}
                onChange={(e) => { setPassword(e.target.value); clearLoginErrors() }}
                onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); handleVerify2FA() } }}
                placeholder="Password"
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
                onClick={handleVerify2FA}
                disabled={!password || loading}
                className="w-full rounded-lg bg-gradient-to-r from-indigo-500 to-purple-500 py-2.5 text-sm font-medium text-white shadow-lg shadow-indigo-500/25 transition hover:from-indigo-400 hover:to-purple-400 disabled:opacity-40 disabled:shadow-none"
              >
                {loading ? 'Signing in…' : 'Sign In'}
              </button>
            </>
          )}

          {/* Done */}
          {step === 'done' && (
            <div className="py-4 text-center">
              <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-full bg-emerald-500/20 text-emerald-400 text-lg ring-1 ring-emerald-500/30">
                ✓
              </div>
              <p className="text-sm font-medium text-slate-200">Signed in successfully</p>
              <p className="mt-1 text-xs text-slate-500">Loading your chats…</p>
            </div>
          )}
        </div>

        <p className="mt-4 text-center text-xs text-slate-600">
          Your Telegram session and credentials are encrypted with your master password. Messages are stored unencrypted in a local database.
        </p>
      </div>
    </div>
  )
}
