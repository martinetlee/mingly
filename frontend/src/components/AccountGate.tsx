import { Loader2 } from 'lucide-react'
import type { AccountsState } from '../hooks/useAccounts'
import { errorMessage } from '../lib/toast'
import { NoAccountsView } from './NoAccountsNotice'
import { LoadError } from './ui'

/**
 * What a channel space (Mail, Calendar) shows while it has no usable
 * account: a spinner while the accounts load, their load error with Retry
 * (a failed request is never "nothing connected"), else the connect notice.
 */
export default function AccountGate({ state, channels, title, body }: {
  state: AccountsState
  channels: readonly string[]
  title: string
  body: string
}) {
  return (
    <div className="flex flex-1 p-6">
      <NoAccountsView
        state={state}
        channels={channels}
        title={title}
        body={body}
        className="m-auto max-w-sm"
        fallback={state.error != null ? (
          <LoadError className="m-auto" message={`Couldn’t load your accounts: ${errorMessage(state.error)}`} onRetry={() => { void state.refresh() }} />
        ) : (
          <div className="m-auto text-slate-500"><Loader2 size={18} className="animate-spin" aria-label="Loading" /></div>
        )}
      />
    </div>
  )
}
