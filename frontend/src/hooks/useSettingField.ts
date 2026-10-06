import { useCallback, useEffect, useRef, useState } from 'react'
import { reportError } from '../lib/toast'

export interface SettingFieldOptions<T> {
  /** Value shown before the server value is known */
  initial: T
  /** Load the server value on mount (alternative to `serverValue`) */
  get?: () => Promise<T>
  /**
   * Server value supplied by the caller (e.g. from one shared getPrefs()).
   * Adopted whenever it changes, unless a local edit is pending or saving.
   */
  serverValue?: T
  /** Persist a value. Resolves with the server response (see `echo`). */
  set: (value: T) => Promise<unknown>
  /** Extract the server-normalised value from `set`'s response (e.g. clamped) */
  echo?: (response: unknown, sent: T) => T | undefined
  /** Debounce before writing, in ms (default 500; 0 writes immediately) */
  debounceMs?: number
  /** Return an error message to block the write (value stays editable; flush() reverts it) */
  validate?: (value: T) => string | null
  /** Human name used in the failure toast: "Couldn't save <label>" */
  label?: string
  equals?: (a: T, b: T) => boolean
}

export interface SettingField<T> {
  /** What the control should display */
  value: T
  /** Local edit; schedules a debounced write. `{immediate: true}` writes now (selects, toggles). */
  setValue: (value: T, opts?: { immediate?: boolean }) => void
  /** Write any pending edit now (call on blur); an invalid edit is reverted */
  flush: () => void
  saving: boolean
  /** Validation message for the current (unsent) value */
  invalid: string | null
  /** Last save error (cleared by the next successful save) */
  error: unknown
  /** The server value has been loaded (always true with neither get nor serverValue) */
  loaded: boolean
}

/**
 * A settings control bound to a server value: debounced, serialized writes
 * (never two in flight; the latest value wins), revert + error toast on
 * failure, and adoption of the server's echo.
 *
 *   const maxTokens = useSettingField({
 *     initial: 2000, serverValue: prefs?.briefing_max_tokens,
 *     set: setBriefingMaxTokens, echo: (r) => (r as { max_tokens?: number }).max_tokens,
 *     validate: (v) => (v < 256 || v > 32000 ? 'Between 256 and 32000' : null),
 *     label: 'briefing length',
 *   })
 *   <input value={maxTokens.value} onChange={(e) => maxTokens.setValue(Number(e.target.value))} onBlur={maxTokens.flush} />
 */
export function useSettingField<T>(options: SettingFieldOptions<T>): SettingField<T> {
  const optsRef = useRef(options)
  optsRef.current = options
  const eq = (a: T, b: T) => (optsRef.current.equals ?? Object.is)(a, b)

  const [value, setDraft] = useState<T>(options.serverValue !== undefined ? options.serverValue : options.initial)
  const [saving, setSaving] = useState(false)
  const [invalid, setInvalid] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loaded, setLoaded] = useState(options.get == null || options.serverValue !== undefined)

  const latestRef = useRef<T>(value)          // newest local value
  const confirmedRef = useRef<T>(value)       // last value the server accepted
  const dirtyRef = useRef(false)              // local edit not yet confirmed
  const writingRef = useRef(false)
  const queuedRef = useRef(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const mountedRef = useRef(true)

  const show = (v: T) => {
    latestRef.current = v
    if (mountedRef.current) setDraft(v)
  }

  const adoptServer = useCallback((v: T) => {
    if (dirtyRef.current || writingRef.current) return
    confirmedRef.current = v
    show(v)
    if (mountedRef.current) setLoaded(true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const commit = useCallback(async () => {
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
    if (writingRef.current) { queuedRef.current = true; return }
    const v = latestRef.current
    if (optsRef.current.validate?.(v)) return
    if (eq(v, confirmedRef.current)) { dirtyRef.current = false; return }
    writingRef.current = true
    if (mountedRef.current) setSaving(true)
    try {
      const res = await optsRef.current.set(v)
      const echoed = optsRef.current.echo?.(res, v)
      confirmedRef.current = echoed !== undefined ? echoed : v
      if (eq(latestRef.current, v)) {
        // No newer edit: show what the server actually stored
        dirtyRef.current = false
        show(confirmedRef.current)
      }
      if (mountedRef.current) setError(null)
    } catch (e) {
      if (eq(latestRef.current, v)) {
        // Revert to the last value the server accepted
        dirtyRef.current = false
        queuedRef.current = false
        show(confirmedRef.current)
      }
      if (mountedRef.current) setError(e)
      const label = optsRef.current.label
      reportError(e, label ? `Couldn't save ${label}` : "Couldn't save setting")
    } finally {
      writingRef.current = false
      if (mountedRef.current) setSaving(false)
      if (queuedRef.current) {
        queuedRef.current = false
        void commit()
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const setValue = useCallback((v: T, opts?: { immediate?: boolean }) => {
    dirtyRef.current = true
    show(v)
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
    const msg = optsRef.current.validate?.(v) ?? null
    setInvalid(msg)
    if (msg) return
    const delay = opts?.immediate ? 0 : (optsRef.current.debounceMs ?? 500)
    if (delay === 0) void commit()
    else timerRef.current = setTimeout(() => { void commit() }, delay)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [commit])

  const flush = useCallback(() => {
    if (optsRef.current.validate?.(latestRef.current)) {
      // Invalid edit on blur: snap back to the saved value
      dirtyRef.current = false
      setInvalid(null)
      show(confirmedRef.current)
      return
    }
    if (dirtyRef.current) void commit()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [commit])

  // External server value
  useEffect(() => {
    if (options.serverValue !== undefined) adoptServer(options.serverValue)
  }, [options.serverValue, adoptServer])

  // Self-loading
  useEffect(() => {
    const get = optsRef.current.get
    if (!get) return
    let cancelled = false
    get().then((v) => { if (!cancelled) adoptServer(v) }, (e) => reportError(e, 'Failed to load setting'))
    return () => { cancelled = true }
  }, [adoptServer])

  // Unmount: don't lose an edit still waiting for its debounce
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      if (timerRef.current) {
        clearTimeout(timerRef.current)
        timerRef.current = null
        void commit()
      }
    }
  }, [commit])

  return { value, setValue, flush, saving, invalid, error, loaded }
}
