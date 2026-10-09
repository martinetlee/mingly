import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Plug } from 'lucide-react'
import type { Account } from '../api/accounts'
import { useAccounts } from '../hooks/useAccounts'
import type { AccountsState } from '../hooks/useAccounts'

/** Path of Settings → Connections */
export const CONNECTIONS_PATH = '/settings?tab=connections'

export interface NoAccountsProps {
  /** Only accounts on these channels count (e.g. ['gmail'] for Mail); default: any channel */
  channels?: readonly string[]
  title?: string
  body?: string
  className?: string
  /** Rendered instead while a matching account exists, or while accounts are loading / failed to load */
  fallback?: ReactNode
}

/** A configured account on one of `channels` (any channel when omitted) */
export function hasAccountOn(accounts: Pick<Account, 'channel' | 'configured'>[], channels?: readonly string[]): boolean {
  return accounts.some((a) => a.configured && (!channels || channels.includes(a.channel)))
}

/**
 * The same notice, for a page that already holds the accounts state (one
 * GET /api/accounts per page). A failed accounts request is never shown as
 * "nothing connected": the fallback renders instead.
 */
export function NoAccountsView({
  state, channels, className = '', fallback = null,
  title = 'No accounts connected',
  body = 'Connect a messaging account to start syncing your conversations.',
}: NoAccountsProps & { state: Pick<AccountsState, 'accounts' | 'loaded' | 'error'> }) {
  const { accounts, loaded, error } = state
  if (!loaded || error != null || hasAccountOn(accounts, channels)) return <>{fallback}</>
  return (
    <div className={`rounded-xl border border-indigo-500/30 bg-indigo-500/5 p-5 text-center ${className}`} data-testid="no-accounts">
      <Plug size={20} className="mx-auto mb-2 text-indigo-400" aria-hidden />
      <p className="text-sm font-medium text-slate-200">{title}</p>
      <p className="mt-1 text-xs text-slate-400">{body}</p>
      <Link
        to={CONNECTIONS_PATH}
        className="mt-4 inline-block rounded-lg bg-indigo-500 px-4 py-2 text-xs font-medium text-white transition-colors hover:bg-indigo-400"
      >
        Open Settings → Connections
      </Link>
    </div>
  )
}

/**
 * Empty state shown while no account is connected (on `channels`, or on any
 * channel): there is nothing to sync yet, so point to Settings → Connections.
 * Renders `fallback` instead once a matching account exists (or while the
 * list is loading / failed to load).
 */
export default function NoAccountsNotice(props: NoAccountsProps) {
  const state = useAccounts()
  return <NoAccountsView {...props} state={state} />
}
