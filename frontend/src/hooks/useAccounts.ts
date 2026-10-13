import { useCallback, useEffect, useRef, useState } from 'react'
import { getAccounts, SIGN_IN_POLL_MS } from '../api/accounts'
import type { Account, ConnectableChannel } from '../api/accounts'
import { useWsEvent } from './useWebSocket'
import { useWebSocketClient } from '../context/WebSocketContext'
import { isAbortError } from '../lib/http'

/** Connector states after which an account may no longer hold credentials (logout, revoked session) */
const CREDENTIALS_MAY_CHANGE: ReadonlySet<string> = new Set(['unauthorized', 'stopped'])

export interface AccountsState {
  accounts: Account[]
  channels: ConnectableChannel[]
  /** First load finished (successfully or not) */
  loaded: boolean
  error: unknown
  refresh: () => Promise<void>
}

/**
 * GET /api/accounts, kept live: `connector_status` events update an account's
 * state in place (an unknown or unconfigured account → refetch), and
 * a reconnect (`resync`) refetches. While a channel's setup runs, it is
 * re-read every SIGN_IN_POLL_MS (setup progress has no event). Works outside
 * <WebSocketProvider> too (onboarding), with polling only.
 */
export function useAccounts(): AccountsState {
  const [accounts, setAccounts] = useState<Account[]>([])
  const [channels, setChannels] = useState<ConnectableChannel[]>([])
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const ctrlRef = useRef<AbortController | null>(null)

  const refresh = useCallback(async () => {
    ctrlRef.current?.abort()
    const ctrl = new AbortController()
    ctrlRef.current = ctrl
    try {
      const res = await getAccounts(ctrl.signal)
      if (ctrl.signal.aborted) return
      setAccounts(res.accounts)
      setChannels(res.channels)
      setError(null)
    } catch (e) {
      if (ctrl.signal.aborted || isAbortError(e)) return
      setError(e)
    }
    setLoaded(true)
  }, [])

  useEffect(() => {
    void refresh()
    return () => ctrlRef.current?.abort()
  }, [refresh])

  const setupRunning = channels.some((c) => c.setup?.state === 'running' || c.setup?.state === 'cancelling')
  useEffect(() => {
    if (!setupRunning) return
    const timer = setInterval(() => { void refresh() }, SIGN_IN_POLL_MS)
    return () => clearInterval(timer)
  }, [setupRunning, refresh])

  const live = useWebSocketClient() != null
  useWsEvent('connector_status', (e) => {
    const current = accounts.find((a) => a.account_key === e.account_key)
    // New account, or one that may just have gained credentials: re-read it all
    if (!current || !current.configured) { void refresh(); return }
    setAccounts((prev) => prev.map((a) => (
      a.account_key === e.account_key ? { ...a, status: { state: e.state, error: e.error } } : a
    )))
    // Signing out (or credentials revoked) ends in one of these states and may
    // drop the credentials: `configured` only comes from the server, re-read it
    if (CREDENTIALS_MAY_CHANGE.has(e.state) && current.status.state !== e.state) void refresh()
  }, live)
  useWsEvent('resync', () => { void refresh() }, live)

  return { accounts, channels, loaded, error, refresh }
}
