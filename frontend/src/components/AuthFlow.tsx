/**
 * One renderer for every channel's sign-in (backend/connectors/base.py):
 * `fields` (a form: text / password / phone / code), `qr` (a code to scan),
 * `oauth_browser` (a consent page opened in the system browser), `done` and
 * `error`. A step with `poll` is answered with {} every POLL_MS until it
 * changes (Signal waiting for the scan, Google waiting for the redirect).
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import QRCode from 'qrcode'
import { AlertCircle, CheckCircle2, ExternalLink, Loader2 } from 'lucide-react'
import { beginAccountAuth, continueAccountAuth } from '../api/accounts'
import type { AuthField, AuthStep } from '../api/accounts'
import { openExternalUrl, resolve1Password } from '../api/client'
import { errorMessage } from '../lib/toast'

export const POLL_MS = 2000

const inputClass =
  'w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2.5 text-sm text-slate-100 placeholder-slate-500 outline-none transition focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20'
const primaryButton =
  'rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-400 disabled:opacity-40'
const secondaryButton =
  'rounded-lg px-3 py-2 text-sm text-slate-400 transition-colors hover:bg-slate-700 hover:text-slate-200 disabled:opacity-50'

const isOpRef = (v: string) => v.trim().startsWith('op://')

/** Same step asked again (the connector put its error in `message`). */
const sameStep = (a: AuthStep, b: AuthStep) =>
  a.kind === b.kind && a.title === b.title &&
  a.fields.map((f) => f.name).join() === b.fields.map((f) => f.name).join()

export interface AuthFlowProps {
  /** Account key to re-authorize, single-account channel, or multi-account channel to add */
  accountKey: string
  /** Fired once when sign-in finished */
  onDone?: (step: AuthStep) => void
  /** Leave the flow (Cancel, or the button on the done screen) */
  onClose?: () => void
  doneLabel?: string
  /** Parameters for begin_auth */
  params?: Record<string, unknown>
}

export default function AuthFlow({ accountKey, onDone, onClose, doneLabel = 'Close', params }: AuthFlowProps) {
  const [step, setStep] = useState<AuthStep | null>(null)
  // The step's message is an error when the connector re-asked the same step
  const [messageIsError, setMessageIsError] = useState(false)
  // A request failed (network, unknown flow) — distinct from an `error` step
  const [requestError, setRequestError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [values, setValues] = useState<Record<string, string>>({})
  const [attempt, setAttempt] = useState(0)

  const onDoneRef = useRef(onDone)
  onDoneRef.current = onDone
  const paramsRef = useRef(params)
  paramsRef.current = params
  const mounted = useRef(true)
  // Set on every mount: StrictMode (and any remount) unmounts and mounts again
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const adopt = useCallback((next: AuthStep, prev: AuthStep | null) => {
    if (!mounted.current) return
    setMessageIsError(!!prev && !!next.message && next.kind === 'fields' && sameStep(prev, next))
    setStep(next)
    if (next.kind === 'done') onDoneRef.current?.(next)
  }, [])

  // Begin (again on "Try again")
  useEffect(() => {
    let cancelled = false
    setStep(null)
    setRequestError('')
    setValues({})
    beginAccountAuth(accountKey, paramsRef.current ?? {})
      .then((s) => { if (!cancelled) adopt(s, null) })
      .catch((e) => { if (!cancelled) setRequestError(errorMessage(e)) })
    return () => { cancelled = true }
  }, [accountKey, attempt, adopt])

  // Poll while the step asks for it
  useEffect(() => {
    if (!step?.poll || requestError) return
    let cancelled = false
    const timer = setTimeout(() => {
      continueAccountAuth(accountKey, step.flow_id, {})
        .then((s) => { if (!cancelled) adopt(s, step) })
        .catch((e) => { if (!cancelled) setRequestError(errorMessage(e)) })
    }, POLL_MS)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [step, accountKey, requestError, adopt])

  const submit = async () => {
    if (!step || submitting) return
    const data: Record<string, string> = {}
    for (const f of step.fields) data[f.name] = (values[f.name] ?? '').trim()
    setSubmitting(true)
    setRequestError('')
    try {
      adopt(await continueAccountAuth(accountKey, step.flow_id, data), step)
    } catch (e) {
      if (mounted.current) setRequestError(errorMessage(e))
    } finally {
      if (mounted.current) setSubmitting(false)
    }
  }

  const retry = () => setAttempt((n) => n + 1)

  if (!step) {
    return (
      <div className="py-2">
        {requestError ? (
          <ErrorBlock message={requestError} onRetry={retry} onClose={onClose} />
        ) : (
          <p className="flex items-center gap-2 text-sm text-slate-400">
            <Loader2 size={14} className="animate-spin" /> Starting sign-in…
          </p>
        )}
      </div>
    )
  }

  if (step.kind === 'done') {
    return (
      <div className="py-2 text-center">
        <CheckCircle2 size={32} className="mx-auto mb-2 text-emerald-400" aria-hidden />
        <p className="text-sm font-medium text-slate-200">{step.title || 'Connected'}</p>
        {step.message && <p className="mt-1 text-xs text-slate-400">{step.message}</p>}
        {onClose && (
          <button type="button" onClick={onClose} className={`mt-4 ${primaryButton}`}>{doneLabel}</button>
        )}
      </div>
    )
  }

  if (step.kind === 'error') {
    return (
      <div className="py-2">
        {step.title && <h3 className="mb-1 text-sm font-semibold text-slate-200">{step.title}</h3>}
        <ErrorBlock message={step.message || 'Sign-in failed'} onRetry={retry} onClose={onClose} />
      </div>
    )
  }

  const blockedByOpRef = step.fields.some((f) => isOpRef(values[f.name] ?? ''))
  const missing = step.fields.some((f) => f.required && !(values[f.name] ?? '').trim())

  return (
    <div>
      {step.title && <h3 className="mb-1 text-sm font-semibold text-slate-200">{step.title}</h3>}
      {step.message && (
        messageIsError
          ? <p role="alert" className="mb-3 flex items-center gap-1.5 text-xs text-red-400"><AlertCircle size={12} />{step.message}</p>
          : <p className="mb-3 whitespace-pre-line text-xs text-slate-400">{step.message}</p>
      )}

      {step.kind === 'fields' && (
        <form
          onSubmit={(e) => { e.preventDefault(); if (!missing && !blockedByOpRef) void submit() }}
          className="mt-3"
        >
          {step.fields.map((f, i) => (
            <FieldInput
              key={`${step.flow_id}:${f.name}`}
              field={f}
              value={values[f.name] ?? ''}
              autoFocus={i === 0}
              onChange={(v) => setValues((prev) => ({ ...prev, [f.name]: v }))}
            />
          ))}
          {requestError && <p role="alert" className="mb-3 text-xs text-red-400">{requestError}</p>}
          {blockedByOpRef && (
            <p className="mb-3 text-xs text-amber-400">Resolve all 1Password references before continuing</p>
          )}
          <div className="flex items-center justify-end gap-2">
            {onClose && <button type="button" onClick={onClose} className={secondaryButton}>Cancel</button>}
            <button type="submit" disabled={submitting || missing || blockedByOpRef} className={primaryButton}>
              {submitting ? 'Working…' : 'Continue'}
            </button>
          </div>
        </form>
      )}

      {step.kind === 'qr' && step.qr_payload && <QrCode payload={step.qr_payload} />}

      {step.kind === 'oauth_browser' && step.auth_url && <BrowserConsent url={step.auth_url} />}

      {(step.kind === 'qr' || step.kind === 'oauth_browser') && (
        <>
          {requestError && (
            <div className="mt-3">
              <ErrorBlock message={requestError} onRetry={retry} onClose={onClose} />
            </div>
          )}
          {!requestError && (
            <div className="mt-4 flex items-center justify-between gap-2">
              {step.poll
                ? <span className="flex items-center gap-2 text-xs text-slate-400"><Loader2 size={12} className="animate-spin" />Waiting…</span>
                : <span />}
              {onClose && <button type="button" onClick={onClose} className={secondaryButton}>Cancel</button>}
            </div>
          )}
        </>
      )}

      {!['fields', 'qr', 'oauth_browser'].includes(step.kind) && (
        <ErrorBlock message={`Unsupported sign-in step "${step.kind}"`} onRetry={retry} onClose={onClose} />
      )}
    </div>
  )
}

function ErrorBlock({ message, onRetry, onClose }: { message: string; onRetry: () => void; onClose?: () => void }) {
  return (
    <>
      <p role="alert" className="flex items-start gap-1.5 whitespace-pre-line text-xs text-red-400">
        <AlertCircle size={12} className="mt-0.5 flex-shrink-0" />{message}
      </p>
      <div className="mt-4 flex items-center justify-end gap-2">
        {onClose && <button type="button" onClick={onClose} className={secondaryButton}>Cancel</button>}
        <button type="button" onClick={onRetry} className={primaryButton}>Try again</button>
      </div>
    </>
  )
}

function FieldInput({ field, value, onChange, autoFocus }: {
  field: AuthField
  value: string
  onChange: (v: string) => void
  autoFocus: boolean
}) {
  const [resolving, setResolving] = useState(false)
  const [resolveError, setResolveError] = useState('')
  const [resolved, setResolved] = useState(false)
  const id = `auth-field-${field.name}`
  const secretLike = field.kind === 'text' || field.kind === 'password'

  const resolve = async () => {
    if (resolving) return
    setResolving(true)
    setResolveError('')
    try {
      onChange((await resolve1Password(value.trim())).value)
      setResolved(true)
    } catch (e) {
      setResolveError(errorMessage(e))
    } finally {
      setResolving(false)
    }
  }

  const type = field.kind === 'password' ? 'password' : field.kind === 'phone' ? 'tel' : 'text'
  return (
    <div className="mb-4">
      <label htmlFor={id} className="mb-1.5 block text-xs font-medium text-slate-400">{field.label}</label>
      <div className="flex gap-2">
        <input
          id={id}
          type={type}
          inputMode={field.kind === 'code' ? 'numeric' : undefined}
          autoComplete={field.kind === 'code' ? 'one-time-code' : 'off'}
          spellCheck={false}
          autoFocus={autoFocus}
          value={value}
          placeholder={field.placeholder ?? undefined}
          onChange={(e) => { onChange(e.target.value); setResolved(false); setResolveError('') }}
          className={`${inputClass} ${field.kind === 'code' ? 'tracking-widest' : ''} ${secretLike ? 'font-mono' : ''}`}
        />
        {secretLike && isOpRef(value) && (
          <button
            type="button"
            onClick={() => void resolve()}
            disabled={resolving}
            className="shrink-0 rounded-lg border border-indigo-500/40 bg-indigo-500/10 px-3 py-2 text-xs font-medium text-indigo-400 transition hover:bg-indigo-500/20 disabled:opacity-50"
          >
            {resolving ? '…' : 'Resolve'}
          </button>
        )}
      </div>
      {resolveError && <p className="mt-1 text-xs text-red-400">{resolveError}</p>}
      {resolved && <p className="mt-1 text-xs text-emerald-400">Resolved from 1Password</p>}
      {secretLike && !resolved && !resolveError && (
        <p className="mt-1 text-[11px] text-slate-500">Supports 1Password references (op://…)</p>
      )}
    </div>
  )
}

function QrCode({ payload }: { payload: string }) {
  const [svg, setSvg] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let cancelled = false
    QRCode.toString(payload, { type: 'svg', margin: 1, errorCorrectionLevel: 'M' })
      .then((s) => { if (!cancelled) { setSvg(s); setFailed(false) } })
      .catch(() => { if (!cancelled) setFailed(true) })
    return () => { cancelled = true }
  }, [payload])

  if (failed) return <p role="alert" className="text-xs text-red-400">Couldn't draw the QR code</p>
  return (
    <div className="mx-auto mt-2 flex h-56 w-56 items-center justify-center rounded-xl bg-white p-3">
      {svg
        ? <img alt="QR code to scan" className="h-full w-full" src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`} />
        : <Loader2 size={20} className="animate-spin text-slate-400" />}
    </div>
  )
}

function BrowserConsent({ url }: { url: string }) {
  // Open the consent page once per URL; the button re-opens it
  const opened = useRef<string | null>(null)
  useEffect(() => {
    if (opened.current === url) return
    opened.current = url
    void openExternalUrl(url)
  }, [url])

  return (
    <div className="mt-2">
      <p className="mb-3 text-xs text-slate-400">
        A sign-in page opened in your browser. Finish there; this window continues on its own.
      </p>
      <button
        type="button"
        onClick={() => void openExternalUrl(url)}
        className="flex items-center gap-1.5 rounded-lg border border-slate-600 px-3 py-2 text-xs text-slate-300 transition-colors hover:bg-slate-700"
      >
        <ExternalLink size={12} /> Open the sign-in page again
      </button>
    </div>
  )
}
