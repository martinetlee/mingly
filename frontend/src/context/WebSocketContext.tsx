import { createContext, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { WebSocketClient, defaultWebSocketUrl } from '../lib/ws'
import { getSessionToken, notifySessionExpired, onSessionExpired, onSessionTokenChanged } from '../api/session'
import { validateSession } from '../api/client'

const WebSocketContext = createContext<WebSocketClient | null>(null)

async function checkSession(): Promise<'valid' | 'expired' | 'unknown'> {
  try {
    const v = await validateSession()
    if (!v) return 'unknown' // endpoint missing: keep retrying rather than guess
    return v.valid ? 'valid' : 'expired'
  } catch {
    return 'unknown' // backend unreachable — not evidence the session ended
  }
}

export function createAppWebSocketClient(): WebSocketClient {
  return new WebSocketClient({
    url: () => defaultWebSocketUrl(getSessionToken()),
    checkSession,
    onSessionExpired: notifySessionExpired,
  })
}

/**
 * Owns the app's single WebSocket. Mount once, inside the authenticated part
 * of the tree; unmounting (logout/lock) closes the socket for good.
 */
export function WebSocketProvider({ children, client: injected }: { children: ReactNode; client?: WebSocketClient }) {
  // Created once without side effects; the socket opens in the effect so
  // StrictMode's mount → unmount → mount cycle ends with exactly one socket.
  const [client] = useState(() => injected ?? createAppWebSocketClient())

  useEffect(() => {
    client.start()
    const offExpired = onSessionExpired(() => client.stop())
    const offToken = onSessionTokenChanged(() => {
      if (getSessionToken()) {
        client.start()
        client.reconnectNow()
      } else {
        client.stop()
      }
    })
    const onOnline = () => client.reconnectNow()
    window.addEventListener('online', onOnline)
    return () => {
      offExpired()
      offToken()
      window.removeEventListener('online', onOnline)
      client.stop()
    }
  }, [client])

  return <WebSocketContext.Provider value={client}>{children}</WebSocketContext.Provider>
}

/** The shared client, or null outside a WebSocketProvider. */
export function useWebSocketClient(): WebSocketClient | null {
  return useContext(WebSocketContext)
}
