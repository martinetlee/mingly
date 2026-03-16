import { useEffect, useRef, useCallback } from 'react'
import type { WSEvent } from '../api/types'
import { getSessionToken, notifySessionExpired } from '../api/session'

type Handler = (event: WSEvent) => void

export function useWebSocket(onEvent: Handler) {
  const wsRef = useRef<WebSocket | null>(null)
  const onEventRef = useRef<Handler>(onEvent)
  onEventRef.current = onEvent
  const consecutiveFailsRef = useRef(0)

  const connect = useCallback(() => {
    const token = getSessionToken()
    // Don't attempt to connect without a valid session token —
    // this avoids a tight reconnect loop that floods the server
    // with unauthenticated WebSocket attempts after session expiry.
    if (!token) return

    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${protocol}://${window.location.host}/api/ws?token=${encodeURIComponent(token)}`)
    let didOpen = false

    ws.onopen = () => {
      didOpen = true
      consecutiveFailsRef.current = 0
    }

    ws.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data) as WSEvent
        onEventRef.current(data)
      } catch {
        // ignore malformed messages
      }
    }

    ws.onclose = () => {
      if (!didOpen) {
        // Connection was rejected — could be an invalid token (403) or
        // the server being busy (e.g. during a long sync_messages call).
        // Don't assume session expiry — verify with a lightweight HTTP check.
        consecutiveFailsRef.current++
        if (consecutiveFailsRef.current >= 3) {
          // Verify the session is actually expired before clearing it.
          // Use a protected endpoint (chats list) — auth/status is unprotected.
          fetch('/api/chats?limit=1', {
            headers: { 'X-Session-Token': token },
          })
            .then((r) => {
              if (r.status === 401 || r.status === 403) {
                notifySessionExpired()
              } else {
                // Session is still valid — server was just busy.
                // Retry with longer backoff.
                consecutiveFailsRef.current = 0
                setTimeout(connect, 5000)
              }
            })
            .catch(() => {
              // Network error — retry with backoff, don't kill the session
              setTimeout(connect, 5000)
            })
          return
        }
      }
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
