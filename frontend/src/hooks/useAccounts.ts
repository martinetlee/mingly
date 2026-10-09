import { useCallback, useEffect, useRef, useState } from 'react'
import { getAccounts } from '../api/accounts'
import type { Account, ConnectableChannel } from '../api/accounts'
import { useWsEvent } from './useWebSocket'
import { isAbortError } from '../lib/http'

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
 * a reconnect (`resync`) refetches.
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

  useWsEvent('connector_status', (e) => {
    const current = accounts.find((a) => a.account_key === e.account_key)
    // New account, or one that may just have gained credentials: re-read it all
    if (!current || !current.configured) { void refresh(); return }
    setAccounts((prev) => prev.map((a) => (
      a.account_key === e.account_key ? { ...a, status: { state: e.state, error: e.error } } : a
    )))
  })
  useWsEvent('resync', () => { void refresh() })

  return { accounts, channels, loaded, error, refresh }
}
