import { useEffect, useRef, useCallback } from 'react'
import type { WSEvent } from '../api/types'

type Handler = (event: WSEvent) => void

export function useWebSocket(onEvent: Handler) {
  const wsRef = useRef<WebSocket | null>(null)
  const onEventRef = useRef<Handler>(onEvent)
  onEventRef.current = onEvent

  const connect = useCallback(() => {
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${protocol}://${window.location.host}/api/ws`)

    ws.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data) as WSEvent
        onEventRef.current(data)
      } catch {
        // ignore malformed messages
      }
    }

    ws.onclose = () => {
      // Reconnect after 3s
      setTimeout(connect, 3000)
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
