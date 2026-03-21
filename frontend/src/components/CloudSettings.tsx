import { useState, useEffect, useCallback, useRef } from 'react'
import { getCloudStatus, cloudConnect, cloudDisconnect, cloudCancelConnect } from '../api/client'
import { getSessionToken } from '../api/session'
import EmailManagement from './EmailManagement'
import OrgManagement from './OrgManagement'

type CloudStatus = 'connected' | 'disconnected' | 'connecting' | 'offline' | 'expired' | 'timeout' | 'error'

export default function CloudSettings() {
  const [status, setStatus] = useState<CloudStatus>('disconnected')
  const [email, setEmail] = useState('')
  const [connectedEmail, setConnectedEmail] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const wsRef = useRef<WebSocket | null>(null)

  // Fetch initial status
  useEffect(() => {
    getCloudStatus()
      .then((res) => {
        setStatus(res.status as CloudStatus)
        setConnectedEmail(res.email)
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  // Listen for WebSocket cloud_status events
  useEffect(() => {
    const token = getSessionToken()
    if (!token) return

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = `${protocol}//${window.location.host}/api/ws?token=${encodeURIComponent(token)}`

    // We re-use the existing WS if the Layout already opened one,
    // but for cloud status we add a listener via BroadcastChannel or
    // simply open a second lightweight connection.
    // For simplicity, listen on a custom event dispatched by a message handler.

    function handleCloudStatus(e: Event) {
      const detail = (e as CustomEvent).detail
      if (detail.type === 'cloud_status') {
        setStatus(detail.status as CloudStatus)
        if (detail.email) setConnectedEmail(detail.email)
        if (detail.error) setError(detail.error)
      }
    }

    window.addEventListener('cloud_status', handleCloudStatus)

    // Also set up a dedicated WS listener for cloud events
    try {
      const ws = new WebSocket(wsUrl)
      wsRef.current = ws
      ws.onmessage = (evt) => {
        try {
          const data = JSON.parse(evt.data)
          if (data.type === 'cloud_status') {
            setStatus(data.status as CloudStatus)
            if (data.email) setConnectedEmail(data.email)
            if (data.error) setError(data.error)
          }
        } catch { /* ignore */ }
      }
      ws.onerror = () => {}
      ws.onclose = () => {}
    } catch { /* ignore */ }

    return () => {
      window.removeEventListener('cloud_status', handleCloudStatus)
      if (wsRef.current) {
        wsRef.current.close()
        wsRef.current = null
      }
    }
  }, [])

  const handleConnect = useCallback(async () => {
    if (!email.trim()) return
    setError(null)
    setStatus('connecting')
    try {
      await cloudConnect(email.trim())
      // Polling happens server-side; we'll get WS updates
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Connection failed'
      setError(msg)
      setStatus('disconnected')
    }
  }, [email])

  const handleDisconnect = useCallback(async () => {
    setError(null)
    try {
      await cloudDisconnect()
      setStatus('disconnected')
      setConnectedEmail(null)
      setEmail('')
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Disconnect failed'
      setError(msg)
    }
  }, [])

  const handleCancelConnect = useCallback(async () => {
    try {
      await cloudCancelConnect()
      setStatus('disconnected')
      setError(null)
    } catch { /* ignore */ }
  }, [])

  if (loading) {
    return (
      <section className="mb-8 max-w-xl">
        <h2 className="mb-1 text-sm font-medium text-slate-300">Cloud Account</h2>
        <p className="text-xs text-slate-500">Loading...</p>
      </section>
    )
  }

  return (
    <section className="mb-8 max-w-xl">
      <h2 className="mb-1 text-sm font-medium text-slate-300">Cloud Account</h2>
      <p className="mb-4 text-xs text-slate-400">
        Connect to Mingly Cloud for encrypted backups and shared org directories.
      </p>

      <div className="overflow-hidden rounded-xl border border-slate-700 bg-slate-800 p-4">
        {/* ── Disconnected ── */}
        {(status === 'disconnected' || status === 'expired') && (
          <>
            {status === 'expired' && (
              <div className="mb-3 rounded-lg bg-amber-900/40 border border-amber-700/50 px-3 py-2 text-sm text-amber-300">
                Cloud session expired. Please reconnect.
              </div>
            )}
            <label className="mb-1.5 block text-xs font-medium text-slate-400">
              Email address
            </label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              className="mb-3 w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2.5 text-sm text-slate-100 outline-none placeholder:text-slate-500 focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
              onKeyDown={(e) => { if (e.key === 'Enter') handleConnect() }}
            />
            <button
              onClick={handleConnect}
              disabled={!email.trim()}
              className="w-full rounded-lg bg-blue-600 py-2.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Connect to Cloud
            </button>
          </>
        )}

        {/* ── Connecting ── */}
        {status === 'connecting' && (
          <div className="text-center">
            <div className="mx-auto mb-3 h-8 w-8 animate-spin rounded-full border-2 border-blue-400 border-t-transparent" />
            <p className="mb-1 text-sm font-medium text-slate-200">
              Check your email
            </p>
            <p className="mb-4 text-xs text-slate-400">
              We sent a magic link to your email. Click it to connect.
            </p>
            <button
              onClick={handleCancelConnect}
              className="rounded-lg border border-slate-600 px-4 py-2 text-xs font-medium text-slate-400 hover:bg-slate-700 hover:text-slate-200"
            >
              Cancel
            </button>
          </div>
        )}

        {/* ── Connected ── */}
        {status === 'connected' && (
          <div className="flex items-center justify-between">
            <div>
              <div className="flex items-center gap-2">
                <span className="inline-block h-2 w-2 rounded-full bg-green-400" />
                <span className="text-sm font-medium text-slate-200">Connected</span>
              </div>
              <p className="mt-1 text-xs text-slate-400">
                {connectedEmail || 'Cloud account linked'}
              </p>
            </div>
            <button
              onClick={handleDisconnect}
              className="rounded-lg border border-red-700/50 bg-red-900/30 px-4 py-2 text-xs font-medium text-red-300 hover:bg-red-900/50"
            >
              Disconnect
            </button>
          </div>
        )}

        {/* ── Offline ── */}
        {status === 'offline' && (
          <div className="flex items-center justify-between">
            <div>
              <div className="flex items-center gap-2">
                <span className="inline-block h-2 w-2 rounded-full bg-yellow-400" />
                <span className="text-sm font-medium text-slate-200">Offline</span>
              </div>
              <p className="mt-1 text-xs text-slate-400">
                Cloud server unreachable. Local features work normally.
              </p>
            </div>
            <button
              onClick={handleDisconnect}
              className="rounded-lg border border-slate-600 px-4 py-2 text-xs font-medium text-slate-400 hover:bg-slate-700 hover:text-slate-200"
            >
              Disconnect
            </button>
          </div>
        )}

        {/* ── Error / Timeout ── */}
        {(status === 'timeout' || status === 'error') && (
          <>
            <div className="mb-3 rounded-lg bg-red-900/40 border border-red-700/50 px-3 py-2 text-sm text-red-300">
              {status === 'timeout'
                ? 'Link expired. The magic link was not clicked in time.'
                : error || 'An error occurred.'}
            </div>
            <button
              onClick={() => { setStatus('disconnected'); setError(null) }}
              className="w-full rounded-lg bg-blue-600 py-2.5 text-sm font-medium text-white hover:bg-blue-500"
            >
              Try Again
            </button>
          </>
        )}
      </div>

      {/* General error banner (for non-status errors) */}
      {error && status !== 'timeout' && status !== 'error' && (
        <div className="mt-3 rounded-lg bg-red-900/40 border border-red-700/50 px-3 py-2 text-xs text-red-300">
          {error}
        </div>
      )}

      {/* Email & Org management — only visible when connected */}
      {status === 'connected' && (
        <div className="mt-6 space-y-2">
          <EmailManagement />
          <OrgManagement />
        </div>
      )}
    </section>
  )
}
