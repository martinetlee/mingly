import { useState } from 'react'
import { sendCode, verifyCode, verify2FA } from '../api/client'

type Step = 'phone' | 'code' | 'twofa' | 'done'

interface Props {
  onComplete: () => void
}

export default function Onboarding({ onComplete }: Props) {
  const [step, setStep] = useState<Step>('phone')
  const [phone, setPhone] = useState('')
  const [phoneCodeHash, setPhoneCodeHash] = useState('')
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

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

  return (
    <div className="flex h-screen items-center justify-center bg-gray-50">
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="mb-8 text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-2xl bg-blue-500 text-white font-bold text-xl">
            M
          </div>
          <h1 className="text-2xl font-semibold text-gray-900">Mingly</h1>
          <p className="mt-1 text-sm text-gray-500">Your Telegram, intelligently organised</p>
        </div>

        {/* Card */}
        <div className="rounded-2xl bg-white p-8 shadow-sm border border-gray-100">
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
            </>
          )}

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

          {step === 'done' && (
            <div className="py-4 text-center">
              <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-full bg-green-100 text-green-500">
                ✓
              </div>
              <p className="text-sm font-medium text-gray-900">Signed in successfully</p>
              <p className="mt-1 text-xs text-gray-400">Loading your chats…</p>
            </div>
          )}
        </div>

        <p className="mt-4 text-center text-xs text-gray-400">
          Your session is stored securely in the OS Keychain
        </p>
      </div>
    </div>
  )
}
