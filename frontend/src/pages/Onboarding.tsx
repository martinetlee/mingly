import { useState, useEffect } from 'react'
import {
  hasCredentials, setupCredentials, resolve1Password,
  sendCode, verifyCode, verify2FA,
  initVault,
} from '../api/client'

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
  const [resolving, setResolving] = useState(false)
  const [resolveError, setResolveError] = useState('')
  const [resolved, setResolved] = useState(false)
  const isOpRef = is1PasswordRef(value)

  const handleResolve = async () => {
    setResolving(true)
    setResolveError('')
    setResolved(false)
    try {
      const res = await resolve1Password(value.trim())
      onChange(res.value)
      setResolved(true)
    } catch (e: unknown) {
      setResolveError(e instanceof Error ? e.message : 'Failed to resolve')
    } finally {
      setResolving(false)
    }
  }

  return (
    <div className="mb-4">
      <label className="mb-1 block text-xs font-medium text-gray-600">{label}</label>
      <div className="flex gap-2">
        <input
          type={type}
          value={value}
          onChange={(e) => { onChange(e.target.value); setResolved(false); setResolveError('') }}
          placeholder={placeholder}
          className="w-full rounded-lg border border-gray-200 px-3 py-2.5 text-sm outline-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100 font-mono"
          spellCheck={false}
          autoComplete="off"
        />
        {isOpRef && (
          <button
            onClick={handleResolve}
            disabled={resolving}
            className="shrink-0 rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-xs font-medium text-blue-600 hover:bg-blue-100 disabled:opacity-50"
          >
            {resolving ? '…' : resolved ? '✓' : 'Resolve'}
          </button>
        )}
      </div>
      {resolveError && <p className="mt-1 text-xs text-red-500">{resolveError}</p>}
      {resolved && <p className="mt-1 text-xs text-green-600">Resolved from 1Password</p>}
      <p className="mt-1 text-xs text-gray-400">{hint}</p>
    </div>
  )
}

export default function Onboarding({ onComplete, needsVaultSetup = false }: Props) {
  const [step, setStep] = useState<Step | null>(null)

  // Master password setup
  const [masterPwd, setMasterPwd] = useState('')
  const [masterPwdConfirm, setMasterPwdConfirm] = useState('')
  const [pwdError, setPwdError] = useState('')
  const [pwdLoading, setPwdLoading] = useState(false)

  // API credentials
  const [apiId, setApiId] = useState('')
  const [apiHash, setApiHash] = useState('')
  const [credError, setCredError] = useState('')
  const [credLoading, setCredLoading] = useState(false)

  // Telegram login
  const [phone, setPhone] = useState('')
  const [phoneCodeHash, setPhoneCodeHash] = useState('')
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (needsVaultSetup) {
      setStep('set-password')
      return
    }
    hasCredentials()
      .then(({ has_credentials }) => setStep(has_credentials ? 'phone' : 'credentials'))
      .catch(() => setStep('credentials'))
  }, [needsVaultSetup])

  // ── Step handlers ──────────────────────────────────────────────────────────

  const handleSetPassword = async () => {
    if (masterPwd !== masterPwdConfirm) {
      setPwdError("Passwords don't match")
      return
    }
    if (masterPwd.length < 6) {
      setPwdError('Password must be at least 6 characters')
      return
    }
    setPwdError('')
    setPwdLoading(true)
    try {
      await initVault(masterPwd)
      setStep('credentials')
    } catch (e: unknown) {
      setPwdError(e instanceof Error ? e.message : 'Failed to set password')
    } finally {
      setPwdLoading(false)
    }
  }

  const handleSaveCredentials = async () => {
    setCredError('')
    setCredLoading(true)
    try {
      await setupCredentials(apiId.trim(), apiHash.trim())
      setStep('phone')
    } catch (e: unknown) {
      setCredError(e instanceof Error ? e.message : 'Failed to save credentials')
    } finally {
      setCredLoading(false)
    }
  }

  const handleSendCode = async () => {
    setError('')
    setLoading(true)
    try {
      const res = await sendCode(phone.trim())
      setPhoneCodeHash(res.phone_code_hash)
      setStep('code')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to send code')
    } finally {
      setLoading(false)
    }
  }

  const handleVerifyCode = async () => {
    setError('')
    setLoading(true)
    try {
      const res = await verifyCode(phone.trim(), code.trim(), phoneCodeHash)
      if (res.requires_2fa) {
        setStep('twofa')
      } else {
        setStep('done')
        setTimeout(onComplete, 800)
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Invalid code')
    } finally {
      setLoading(false)
    }
  }

  const handleVerify2FA = async () => {
    setError('')
    setLoading(true)
    try {
      await verify2FA(password)
      setStep('done')
      setTimeout(onComplete, 800)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Incorrect password')
    } finally {
      setLoading(false)
    }
  }

  // ── Step indicator ────────────────────────────────────────────────────────

  const indicatorSteps: Step[] = needsVaultSetup
    ? ['set-password', 'credentials', 'phone', 'code']
    : ['credentials', 'phone', 'code']

  const stepOrder: Step[] = ['set-password', 'credentials', 'phone', 'code', 'twofa', 'done']

  if (step === null) {
    return (
      <div className="flex h-screen items-center justify-center bg-gray-50">
        <div className="text-gray-400 text-sm">Loading…</div>
      </div>
    )
  }

  return (
    <div className="flex h-screen items-center justify-center bg-gray-50">
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="mb-8 text-center">
          <img src="/logo.png" alt="Mingly" className="mx-auto mb-3 h-12 w-12 object-contain" />
          <h1 className="text-2xl font-semibold text-gray-900">Mingly</h1>
          <p className="mt-1 text-sm text-gray-500">Your Telegram, intelligently organised</p>
        </div>

        {/* Step indicators */}
        <div className="mb-6 flex items-center justify-center gap-2">
          {indicatorSteps.map((s, i) => (
            <div key={s} className="flex items-center gap-2">
              <div className={`h-2 w-2 rounded-full transition-colors ${
                step === s ? 'bg-blue-500' :
                stepOrder.indexOf(step) > stepOrder.indexOf(s)
                  ? 'bg-blue-200' : 'bg-gray-200'
              }`} />
            </div>
          ))}
        </div>

        {/* Card */}
        <div className="rounded-2xl bg-white p-8 shadow-sm border border-gray-100">

          {/* Step 0: Set master password */}
          {step === 'set-password' && (
            <>
              <h2 className="mb-1 text-base font-medium text-gray-900">Protect your data</h2>
              <p className="mb-5 text-sm text-gray-500">
                Set a master password to encrypt your credentials locally. You'll enter this each time you open Mingly.
              </p>

              <div className="mb-4">
                <label className="mb-1 block text-xs font-medium text-gray-600">Master password</label>
                <input
                  type="password"
                  value={masterPwd}
                  onChange={(e) => { setMasterPwd(e.target.value); setPwdError('') }}
                  placeholder="At least 6 characters"
                  autoFocus
                  className="w-full rounded-lg border border-gray-200 px-3 py-2.5 text-sm outline-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100"
                />
              </div>
              <div className="mb-4">
                <label className="mb-1 block text-xs font-medium text-gray-600">Confirm password</label>
                <input
                  type="password"
                  value={masterPwdConfirm}
                  onChange={(e) => { setMasterPwdConfirm(e.target.value); setPwdError('') }}
                  onKeyDown={(e) => e.key === 'Enter' && handleSetPassword()}
                  placeholder="Repeat password"
                  className="w-full rounded-lg border border-gray-200 px-3 py-2.5 text-sm outline-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100"
                />
              </div>

              {pwdError && <p className="mb-3 text-sm text-red-500">{pwdError}</p>}
              <button
                onClick={handleSetPassword}
                disabled={!masterPwd || !masterPwdConfirm || pwdLoading}
                className="w-full rounded-lg bg-blue-500 py-2.5 text-sm font-medium text-white transition-colors hover:bg-blue-600 disabled:opacity-50"
              >
                {pwdLoading ? 'Setting up…' : 'Continue'}
              </button>
            </>
          )}

          {/* Step 1: Telegram API credentials */}
          {step === 'credentials' && (
            <>
              <h2 className="mb-1 text-base font-medium text-gray-900">Connect to Telegram</h2>
              <p className="mb-5 text-sm text-gray-500">
                Get your free API credentials at{' '}
                <span className="font-mono text-blue-500">my.telegram.org</span>{' '}
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

              {credError && <p className="mb-3 text-sm text-red-500">{credError}</p>}
              <button
                onClick={handleSaveCredentials}
                disabled={!apiId.trim() || !apiHash.trim() || credLoading || is1PasswordRef(apiId) || is1PasswordRef(apiHash)}
                className="w-full rounded-lg bg-blue-500 py-2.5 text-sm font-medium text-white transition-colors hover:bg-blue-600 disabled:opacity-50"
              >
                {credLoading ? 'Saving…' : 'Continue'}
              </button>
              {(is1PasswordRef(apiId) || is1PasswordRef(apiHash)) && (
                <p className="mt-2 text-center text-xs text-amber-500">
                  Resolve all 1Password references before continuing
                </p>
              )}
            </>
          )}

          {/* Step 2: Phone number */}
          {step === 'phone' && (
            <>
              <h2 className="mb-1 text-base font-medium text-gray-900">Sign in with Telegram</h2>
              <p className="mb-5 text-sm text-gray-500">Enter your phone number with country code</p>
              <input
                type="tel"
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleSendCode()}
                placeholder="+1 234 567 8900"
                autoFocus
                className="mb-4 w-full rounded-lg border border-gray-200 px-3 py-2.5 text-sm outline-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100"
              />
              {error && <p className="mb-3 text-sm text-red-500">{error}</p>}
              <button
                onClick={handleSendCode}
                disabled={!phone.trim() || loading}
                className="w-full rounded-lg bg-blue-500 py-2.5 text-sm font-medium text-white transition-colors hover:bg-blue-600 disabled:opacity-50"
              >
                {loading ? 'Sending…' : 'Send Code'}
              </button>
              <button
                onClick={() => setStep('credentials')}
                className="mt-2 w-full text-sm text-gray-400 hover:text-gray-600"
              >
                ← Change API credentials
              </button>
            </>
          )}

          {/* Step 3: OTP */}
          {step === 'code' && (
            <>
              <h2 className="mb-1 text-base font-medium text-gray-900">Enter the code</h2>
              <p className="mb-5 text-sm text-gray-500">
                We sent a code to <strong>{phone}</strong> via Telegram
              </p>
              <input
                type="text"
                inputMode="numeric"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleVerifyCode()}
                placeholder="12345"
                autoFocus
                maxLength={6}
                className="mb-4 w-full rounded-lg border border-gray-200 px-3 py-2.5 text-sm tracking-widest outline-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100"
              />
              {error && <p className="mb-3 text-sm text-red-500">{error}</p>}
              <button
                onClick={handleVerifyCode}
                disabled={!code.trim() || loading}
                className="w-full rounded-lg bg-blue-500 py-2.5 text-sm font-medium text-white transition-colors hover:bg-blue-600 disabled:opacity-50"
              >
                {loading ? 'Verifying…' : 'Verify Code'}
              </button>
              <button
                onClick={() => { setStep('phone'); setCode(''); setError('') }}
                className="mt-2 w-full text-sm text-gray-400 hover:text-gray-600"
              >
                ← Change number
              </button>
            </>
          )}

          {/* Step 4: 2FA (conditional) */}
          {step === 'twofa' && (
            <>
              <h2 className="mb-1 text-base font-medium text-gray-900">Two-step verification</h2>
              <p className="mb-5 text-sm text-gray-500">Enter your Telegram 2FA password</p>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleVerify2FA()}
                placeholder="Password"
                autoFocus
                className="mb-4 w-full rounded-lg border border-gray-200 px-3 py-2.5 text-sm outline-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100"
              />
              {error && <p className="mb-3 text-sm text-red-500">{error}</p>}
              <button
                onClick={handleVerify2FA}
                disabled={!password || loading}
                className="w-full rounded-lg bg-blue-500 py-2.5 text-sm font-medium text-white transition-colors hover:bg-blue-600 disabled:opacity-50"
              >
                {loading ? 'Signing in…' : 'Sign In'}
              </button>
            </>
          )}

          {/* Done */}
          {step === 'done' && (
            <div className="py-4 text-center">
              <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-full bg-green-100 text-green-500 text-lg">
                ✓
              </div>
              <p className="text-sm font-medium text-gray-900">Signed in successfully</p>
              <p className="mt-1 text-xs text-gray-400">Loading your chats…</p>
            </div>
          )}
        </div>

        <p className="mt-4 text-center text-xs text-gray-400">
          Your session and credentials are encrypted with your master password
        </p>
      </div>
    </div>
  )
}
