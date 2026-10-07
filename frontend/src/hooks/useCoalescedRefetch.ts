import { useCallback, useEffect, useRef } from 'react'

/**
 * Turn a burst of triggers (e.g. WebSocket `new_message` storms) into at most
 * one call per `intervalMs`: the first trigger runs immediately (leading
 * edge), triggers during the window collapse into one trailing call, and an
 * async `fn` never overlaps itself (a trigger while it runs schedules exactly
 * one follow-up).
 *
 *   const refresh = useCoalescedRefetch(refetch, 2000)
 *   useWsEvent('new_message', refresh)
 */
export function useCoalescedRefetch(fn: () => unknown, intervalMs: number): () => void {
  const fnRef = useRef(fn)
  fnRef.current = fn
  const lastRunRef = useRef(0)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const runningRef = useRef(false)
  const dirtyRef = useRef(false)
  const disposedRef = useRef(false)

  const fire = useCallback(function fire() {
    if (disposedRef.current) return
    timerRef.current = null
    dirtyRef.current = false
    runningRef.current = true
    lastRunRef.current = Date.now()
    Promise.resolve()
      .then(() => fnRef.current())
      .catch(() => { /* callers own error reporting */ })
      .finally(() => {
        runningRef.current = false
        if (dirtyRef.current && !disposedRef.current && timerRef.current == null) {
          const wait = Math.max(0, lastRunRef.current + intervalMs - Date.now())
          timerRef.current = setTimeout(fire, wait)
        }
      })
  }, [intervalMs])

  useEffect(() => {
    disposedRef.current = false
    return () => {
      disposedRef.current = true
      if (timerRef.current) clearTimeout(timerRef.current)
      timerRef.current = null
    }
  }, [])

  return useCallback(() => {
    if (disposedRef.current) return
    if (runningRef.current || timerRef.current != null) {
      dirtyRef.current = true
      return
    }
    const wait = lastRunRef.current + intervalMs - Date.now()
    if (wait <= 0) {
      fire()
    } else {
      dirtyRef.current = true
      timerRef.current = setTimeout(fire, wait)
    }
  }, [fire, intervalMs])
}
