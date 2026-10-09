import { useEffect, useRef } from 'react'
import type { WSEvent } from '../api/types'
import type { WSEventType } from '../lib/ws'
import { useWebSocketClient } from '../context/WebSocketContext'

type Handler = (event: WSEvent) => void

let warned = false
function warnNoProvider() {
  if (warned || !import.meta.env.DEV) return
  warned = true
  console.warn('useWebSocket used outside <WebSocketProvider>; events will not be delivered')
}

/**
 * Receive every WebSocket event (including the synthetic `resync` after a
 * reconnect) on the app's single shared socket. The handler may change on
 * every render — it's read through a ref, and the subscription is removed on
 * unmount. Prefer useWsEvent for a single type.
 */
export function useWebSocket(onEvent: Handler) {
  const client = useWebSocketClient()
  const onEventRef = useRef<Handler>(onEvent)
  onEventRef.current = onEvent

  useEffect(() => {
    if (!client) { warnNoProvider(); return }
    return client.subscribe('*', (e) => onEventRef.current(e))
  }, [client])
}

/**
 * Subscribe to one event type with a narrowed payload type:
 *
 *   useWsEvent('new_message', (e) => e.conversation_id)
 *   useWsEvent('resync', () => refetch())
 */
export function useWsEvent<T extends WSEventType>(
  type: T,
  handler: (event: Extract<WSEvent, { type: T }>) => void,
  enabled = true,
) {
  const client = useWebSocketClient()
  const ref = useRef(handler)
  ref.current = handler

  useEffect(() => {
    if (!enabled) return
    if (!client) { warnNoProvider(); return }
    return client.subscribe(type, (e) => ref.current(e))
  }, [client, type, enabled])
}
