import { useCallback, useEffect, useRef, useState } from 'react'
import { checkMessagesForScam } from '../../api/client'
import { useWsEvent } from '../../hooks/useWebSocket'
import { reportError, toast } from '../../lib/toast'
import { messageKey } from './shared'

/** If no terminal WS event ever arrives (backend died mid-scan), stop spinning. */
const SPINNER_TIMEOUT_MS = 90_000

/**
 * Per-message "Rescan" state. A spinner ends on the first terminal event
 * (scam_detected / scam_check_safe / scam_check_error) whose message_ids
 * include the message, as the contract specifies.
 */
export function useRescan() {
  const [running, setRunning] = useState<Set<string>>(() => new Set())
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>())

  const finish = useCallback((keys: string[]) => {
    if (!keys.length) return
    keys.forEach((k) => {
      const t = timers.current.get(k)
      if (t) clearTimeout(t)
      timers.current.delete(k)
    })
    setRunning((prev) => {
      if (!keys.some((k) => prev.has(k))) return prev
      const next = new Set(prev)
      keys.forEach((k) => next.delete(k))
      return next
    })
  }, [])

  useEffect(() => {
    const t = timers.current
    return () => { t.forEach(clearTimeout); t.clear() }
  }, [])

  const onTerminal = useCallback((e: { message_ids: number[] }) => {
    finish(e.message_ids.map((id) => messageKey({ message_id: id })))
  }, [finish])
  useWsEvent('scam_detected', onTerminal)
  useWsEvent('scam_check_safe', onTerminal)
  useWsEvent('scam_check_error', (e) => {
    // Only report failures of scans the user started here (not background ones)
    const mine = e.message_ids.some((id) => timers.current.has(messageKey({ message_id: id })))
    onTerminal(e)
    if (mine) toast.error(`Scan failed: ${e.error}`)
  })

  const rescan = useCallback(async (conversationId: number, messageId: number) => {
    const key = messageKey({ message_id: messageId })
    if (timers.current.has(key)) return
    setRunning((prev) => new Set(prev).add(key))
    timers.current.set(key, setTimeout(() => finish([key]), SPINNER_TIMEOUT_MS))
    try {
      const r = await checkMessagesForScam(conversationId, [messageId])
      if (r.missing_ids?.includes(messageId)) {
        finish([key])
        toast.error('That message is no longer stored locally')
      }
    } catch (e) {
      finish([key])
      reportError(e, "Couldn't start scan")
    }
  }, [finish])

  return { isRescanning: (key: string) => running.has(key), rescan }
}
