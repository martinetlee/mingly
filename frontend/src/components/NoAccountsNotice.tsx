import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Plug } from 'lucide-react'
import { hasConnectedAccount } from '../api/accounts'
import { useAccounts } from '../hooks/useAccounts'

/** Path of Settings → Connections */
export const CONNECTIONS_PATH = '/settings?tab=connections'

/**
 * Empty state shown while no account is connected on any channel: there is
 * nothing to sync yet, so point to Settings → Connections. Renders
 * `fallback` instead once an account exists (or while the list is loading /
 * failed to load).
 */
export default function NoAccountsNotice({ className = '', fallback = null }: { className?: string; fallback?: ReactNode }) {
  const { accounts, loaded, error } = useAccounts()
  if (!loaded || error != null || hasConnectedAccount(accounts)) return <>{fallback}</>
  return (
    <div className={`rounded-xl border border-indigo-500/30 bg-indigo-500/5 p-5 text-center ${className}`}>
      <Plug size={20} className="mx-auto mb-2 text-indigo-400" aria-hidden />
      <p className="text-sm font-medium text-slate-200">No accounts connected</p>
      <p className="mt-1 text-xs text-slate-400">
        Connect a messaging account to start syncing your conversations.
      </p>
      <Link
        to={CONNECTIONS_PATH}
        className="mt-4 inline-block rounded-lg bg-indigo-500 px-4 py-2 text-xs font-medium text-white transition-colors hover:bg-indigo-400"
      >
        Open Settings → Connections
      </Link>
    </div>
  )
}
