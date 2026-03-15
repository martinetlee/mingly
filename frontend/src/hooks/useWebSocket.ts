import { useEffect, useRef, useCallback } from 'react'
import type { WSEvent } from '../api/types'
import { getSessionToken } from '../api/session'

type Handler = (event: WSEvent) => void

export function useWebSocket(onEvent: Handler) {
  const wsRef = useRef<WebSocket | null>(null)
  const onEventRef = useRef<Handler>(onEvent)
  onEventRef.current = onEvent

  const connect = useCallback(() => {
    const token = getSessionToken()
    // Don't attempt to connect without a valid session token —
    // this avoids a tight reconnect loop that floods the server
    // with unauthenticated WebSocket attempts after session expiry.
    if (!token) return

    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${protocol}://${window.location.host}/api/ws?token=${encodeURIComponent(token)}`)

    ws.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data) as WSEvent
        onEventRef.current(data)
      } catch {
        // ignore malformed messages
      }
    }

    ws.onclose = () => {
      // Only reconnect if we still have a valid session token.
      // If the session expired the app will navigate to the unlock
      // screen and this component will unmount.
      if (getSessionToken()) {
        setTimeout(connect, 3000)
      }
    }

    wsRef.current = ws
  }, [])

  useEffect(() => {
    connect()
    return () => {
      wsRef.current?.close()
    }
  }, [connect])
}
