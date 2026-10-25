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
import { beginAccountAuth, cancelAccountAuth, continueAccountAuth, SIGN_IN_POLL_MS } from '../../api/accounts'
import type { AuthField, AuthStep } from '../../api/accounts'
import { openExternalUrl, resolve1Password } from '../../api/client'
import { errorMessage } from '../../lib/toast'

export const POLL_MS = SIGN_IN_POLL_MS

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
  /**
   * Leaving a `progress` step (its background work keeps going, e.g. "Connect
   * another account"); the step's `stop_label` button stops the work instead
   */
  backgroundLabel?: string
  /** Parameters for begin_auth */
  params?: Record<string, unknown>
}

export default function AuthFlow({
  accountKey, onDone, onClose, doneLabel = 'Close', backgroundLabel = 'Close', params,
}: AuthFlowProps) {
  const [step, setStep] = useState<AuthStep | null>(null)
  // The step's message is an error when the connector re-asked the same step
  const [messageIsError, setMessageIsError] = useState(false)
  // A request failed (network, unknown flow) — distinct from an `error` step
  const [requestError, setRequestError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [stopping, setStopping] = useState(false)
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

  // The server-side flow this component holds open. Leaving it (Cancel,
  // unmount, "Try again", another account) cancels it, so the connector can
  // stop what it holds — Signal keeps a `signal-cli link` process per flow.
  const openFlow = useRef<{ key: string; flowId: string } | null>(null)
  const release = useCallback(() => {
    const f = openFlow.current
    openFlow.current = null
    if (f) cancelAccountAuth(f.key, f.flowId).catch(() => { /* already gone */ })
  }, [])
  const isOpen = (s: AuthStep) => s.kind !== 'done' && s.kind !== 'error'

  const adopt = useCallback((key: string, next: AuthStep, prev: AuthStep | null) => {
    if (!mounted.current) {
      // Answered after the flow was left: let it go
      if (isOpen(next)) cancelAccountAuth(key, next.flow_id).catch(() => {})
      return
    }
    openFlow.current = isOpen(next) ? { key, flowId: next.flow_id } : null
    setMessageIsError(!!prev && !!next.message && next.kind === 'fields' && sameStep(prev, next))
    setStep(next)
    if (next.kind === 'done') onDoneRef.current?.(next)
  }, [])

  // Begin (again on "Try again")
  useEffect(() => {
    let cancelled = false
    release()
    setStep(null)
    setRequestError('')
    setValues({})
    beginAccountAuth(accountKey, paramsRef.current ?? {})
      .then((s) => {
        // A begin that lost the race (StrictMode re-run, account switch) still started a flow
        if (cancelled) { if (isOpen(s)) cancelAccountAuth(accountKey, s.flow_id).catch(() => {}); return }
        adopt(accountKey, s, null)
      })
      .catch((e) => { if (!cancelled) setRequestError(errorMessage(e)) })
    return () => { cancelled = true }
  }, [accountKey, attempt, adopt, release])

  // Leaving the component abandons the flow
  useEffect(() => () => release(), [release])

  // Poll while the step asks for it
  useEffect(() => {
    if (!step?.poll || requestError) return
    let cancelled = false
    const timer = setTimeout(() => {
      continueAccountAuth(accountKey, step.flow_id, {})
        .then((s) => { if (!cancelled) adopt(accountKey, s, step) })
        .catch((e) => { if (!cancelled) setRequestError(errorMessage(e)) })
    }, POLL_MS)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [step, accountKey, requestError, adopt])

  const submit = async () => {
    if (!step || submitting) return
    const data: Record<string, string> = {}
    for (const f of step.fields) data[f.name] = fieldValue(f, values).trim()
    setSubmitting(true)
    setRequestError('')
    try {
      adopt(accountKey, await continueAccountAuth(accountKey, step.flow_id, data), step)
    } catch (e) {
      if (mounted.current) setRequestError(errorMessage(e))
    } finally {
      if (mounted.current) setSubmitting(false)
    }
  }

  const retry = () => setAttempt((n) => n + 1)
  const close = onClose ? () => { release(); onClose() } : undefined
  // Leave and stop the step's background work (e.g. cancel Signal's setup download)
  const stopAndClose = onClose ? async () => {
    const f = openFlow.current
    if (!f || stopping) return
    setStopping(true)
    setRequestError('')
    openFlow.current = null
    try {
      await cancelAccountAuth(f.key, f.flowId, { stop: true })
      if (mounted.current) onClose()
    } catch (e) {
      // Not stopped: the flow is still open
      openFlow.current = f
      if (mounted.current) { setRequestError(errorMessage(e)); setStopping(false) }
    }
  } : undefined

  if (!step) {
    return (
      <div className="py-2">
        {requestError ? (
          <ErrorBlock message={requestError} onRetry={retry} onClose={close} />
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
        {step.message && <p className="mt-1 whitespace-pre-line text-xs text-slate-400">{step.message}</p>}
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
        <ErrorBlock message={step.message || 'Sign-in failed'} onRetry={retry} onClose={close} />
      </div>
    )
  }

  const blockedByOpRef = step.fields.some((f) => isOpRef(values[f.name] ?? ''))
  const missing = step.fields.some((f) => f.required && !fieldValue(f, values).trim())

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
              value={fieldValue(f, values)}
              autoFocus={i === 0}
              onChange={(v) => setValues((prev) => ({ ...prev, [f.name]: v }))}
            />
          ))}
          {requestError && <p role="alert" className="mb-3 text-xs text-red-400">{requestError}</p>}
          {blockedByOpRef && (
            <p className="mb-3 text-xs text-amber-400">Resolve all 1Password references before continuing</p>
          )}
          <div className="flex items-center justify-end gap-2">
            {close && <button type="button" onClick={close} className={secondaryButton}>{step.cancel_label || 'Cancel'}</button>}
            <button type="submit" disabled={submitting || missing || blockedByOpRef} className={primaryButton}>
              {submitting ? 'Working…' : (step.submit_label || 'Continue')}
            </button>
          </div>
        </form>
      )}

      {step.kind === 'qr' && step.qr_payload && <QrCode payload={step.qr_payload} />}

      {step.kind === 'oauth_browser' && step.auth_url && <BrowserConsent url={step.auth_url} />}

      {step.kind === 'progress' && <ProgressBar value={step.progress ?? 0} />}

      {(step.kind === 'qr' || step.kind === 'oauth_browser' || step.kind === 'progress') && (
        <>
          {requestError && (
            <div className="mt-3">
              <ErrorBlock message={requestError} onRetry={retry} onClose={close} />
            </div>
          )}
          {!requestError && step.kind === 'progress' && (
            <div className="mt-4 flex items-center justify-between gap-2">
              {step.stop_label && stopAndClose
                ? (
                  <button type="button" onClick={() => void stopAndClose()} disabled={stopping} className={secondaryButton}>
                    {stopping ? 'Stopping…' : step.stop_label}
                  </button>
                )
                : <span />}
              {close && <button type="button" onClick={close} className={primaryButton}>{backgroundLabel}</button>}
            </div>
          )}
          {!requestError && step.kind !== 'progress' && (
            <div className="mt-4 flex items-center justify-between gap-2">
              {step.poll
                ? <span className="flex items-center gap-2 text-xs text-slate-400"><Loader2 size={12} className="animate-spin" />Waiting…</span>
                : <span />}
              {close && <button type="button" onClick={close} className={secondaryButton}>Cancel</button>}
            </div>
          )}
        </>
      )}

      {!['fields', 'qr', 'oauth_browser', 'progress'].includes(step.kind) && (
        <ErrorBlock message={`Unsupported sign-in step "${step.kind}"`} onRetry={retry} onClose={close} />
      )}
    </div>
  )
}

function ProgressBar({ value }: { value: number }) {
  const pct = Math.round(Math.min(Math.max(value, 0), 1) * 100)
  return (
    <div
      role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}
      className="h-1.5 w-full overflow-hidden rounded-full bg-slate-700"
    >
      <div
        className="h-full rounded-full bg-gradient-to-r from-indigo-500 to-purple-500 transition-all duration-300"
        style={{ width: `${pct}%` }}
      />
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

/** What a field holds: typed text, or a choice's pick (its default, else the first option) */
function fieldValue(field: AuthField, values: Record<string, string>): string {
  if (values[field.name] != null) return values[field.name]
  if (field.kind === 'choice') return field.default ?? field.options?.[0]?.value ?? ''
  return ''
}

function FieldInput({ field, value, onChange, autoFocus }: {
  field: AuthField
  value: string
  onChange: (v: string) => void
  autoFocus: boolean
}) {
  if (field.kind === 'choice') return <ChoiceInput field={field} value={value} onChange={onChange} autoFocus={autoFocus} />
  return <TextInput field={field} value={value} onChange={onChange} autoFocus={autoFocus} />
}

function ChoiceInput({ field, value, onChange, autoFocus }: {
  field: AuthField
  value: string
  onChange: (v: string) => void
  autoFocus: boolean
}) {
  return (
    <fieldset className="mb-4">
      <legend className="mb-1.5 block text-xs font-medium text-slate-400">{field.label}</legend>
      <div className="flex flex-col gap-2">
        {(field.options ?? []).map((o) => {
          const checked = o.value === value
          return (
            <label
              key={o.value}
              className={`flex cursor-pointer items-start gap-2.5 rounded-lg border px-3 py-2 transition-colors ${
                checked ? 'border-indigo-500/60 bg-indigo-500/10' : 'border-slate-600 hover:bg-slate-700/40'
              }`}
            >
              <input
                type="radio"
                name={`auth-field-${field.name}`}
                value={o.value}
                checked={checked}
                autoFocus={autoFocus && checked}
                onChange={() => onChange(o.value)}
                className="mt-0.5 accent-indigo-500"
              />
              <span className="min-w-0">
                <span className="block break-all text-sm text-slate-200">{o.label}</span>
                {o.hint && <span className="block text-xs text-slate-400">{o.hint}</span>}
              </span>
            </label>
          )
        })}
      </div>
    </fieldset>
  )
}

function TextInput({ field, value, onChange, autoFocus }: {
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
