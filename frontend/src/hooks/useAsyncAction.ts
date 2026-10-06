import { useCallback, useEffect, useRef, useState } from 'react'
import { isAbortError } from '../lib/http'
import { reportError } from '../lib/toast'

export interface AsyncActionOptions {
  /** Show an error toast "<errorContext>: <message>" on failure */
  errorContext?: string
  onError?: (e: unknown) => void
}

export interface AsyncAction<A extends unknown[], R> {
  /**
   * Run the action. Single-flight: while one run is pending, further calls
   * return that same promise and do NOT start another (double-click safe).
   * Never rejects — resolves to undefined on failure (see `error`).
   */
  run: (...args: A) => Promise<R | undefined>
  pending: boolean
  error: unknown
  reset: () => void
}

/**
 * Wrap a mutation so it can't run twice concurrently and exposes
 * pending/error for the UI.
 *
 *   const forward = useAsyncAction((to: number) => forwardMessages(chatId, ids, to), { errorContext: 'Forward failed' })
 *   <button disabled={forward.pending} onClick={() => forward.run(target)}>
 */
export function useAsyncAction<A extends unknown[], R>(
  fn: (...args: A) => Promise<R>,
  options: AsyncActionOptions = {},
): AsyncAction<A, R> {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const fnRef = useRef(fn)
  fnRef.current = fn
  const optsRef = useRef(options)
  optsRef.current = options
  const inflightRef = useRef<Promise<R | undefined> | null>(null)
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  const run = useCallback((...args: A): Promise<R | undefined> => {
    if (inflightRef.current) return inflightRef.current
    setPending(true)
    setError(null)
    const p = (async () => {
      try {
        return await fnRef.current(...args)
      } catch (e) {
        if (!isAbortError(e)) {
          if (mountedRef.current) setError(e)
          const { errorContext, onError } = optsRef.current
          onError?.(e)
          if (errorContext) reportError(e, errorContext)
        }
        return undefined
      } finally {
        inflightRef.current = null
        if (mountedRef.current) setPending(false)
      }
    })()
    inflightRef.current = p
    return p
  }, [])

  const reset = useCallback(() => setError(null), [])

  return { run, pending, error, reset }
}
