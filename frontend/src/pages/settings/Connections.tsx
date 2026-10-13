import { useState } from 'react'
import { Loader2, Plus } from 'lucide-react'
import { logoutAccount, removeAccount } from '../../api/accounts'
import type { Account, ConnectableChannel } from '../../api/accounts'
import type { ConnectorState } from '../../api/types'
import AuthFlow from '../../components/channels/AuthFlow'
import ChannelIcon from '../../components/channels/ChannelIcon'
import ChannelSetupNote from '../../components/channels/ChannelSetupNote'
import { Modal, useConfirm } from '../../components/ui'
import { useAccounts } from '../../hooks/useAccounts'
import { errorMessage, toast } from '../../lib/toast'
import { Card, Section } from './shared'

const STATE_LABEL: Record<ConnectorState, string> = {
  starting: 'Connecting…',
  syncing: 'Syncing…',
  ready: 'Connected',
  reconnecting: 'Reconnecting…',
  unauthorized: 'Signed out',
  error: 'Error',
  stopped: 'Stopped',
}

const STATE_DOT: Record<ConnectorState, string> = {
  starting: 'bg-amber-400',
  syncing: 'bg-sky-400',
  ready: 'bg-emerald-400',
  reconnecting: 'bg-amber-400',
  unauthorized: 'bg-slate-500',
  error: 'bg-red-400',
  stopped: 'bg-slate-500',
}

const actionButton =
  'rounded-lg border border-slate-600 px-3 py-1.5 text-xs text-slate-300 transition-colors hover:bg-slate-700 disabled:opacity-50'
const dangerButton =
  'rounded-lg border border-red-500/30 px-3 py-1.5 text-xs text-red-400 transition-colors hover:bg-red-500/10 disabled:opacity-50'

interface FlowTarget {
  key: string
  title: string
  params?: Record<string, unknown>
}

/** Settings → Connections: connected accounts and the channels that can be added. */
export default function Connections() {
  const { accounts, channels, loaded, error, refresh } = useAccounts()
  const confirm = useConfirm()
  const [flow, setFlow] = useState<FlowTarget | null>(null)

  const connected = accounts.filter((a) => a.configured)
  const isMulti = (channel: string) => channels.some((c) => c.channel === channel && c.multi_account)
  // Multi-account channels can always add one more; single-account ones only until connected
  const addable = channels.filter((c) => c.multi_account || !connected.some((a) => a.channel === c.channel))

  const logOut = (a: Account) => confirm({
    title: `Log out of ${accountName(a)}?`,
    message: 'Syncing stops and the session is forgotten. Messages already synced stay. You can sign in again any time.',
    confirmLabel: 'Log out',
    destructive: true,
    onConfirm: async () => {
      await logoutAccount(a.account_key)
      await refresh()
      toast.success(`Logged out of ${a.display_name}`)
    },
  })

  const remove = (a: Account) => confirm({
    title: `Remove ${accountName(a)}?`,
    message: 'Disconnects the account and deletes everything synced from it (conversations, messages, attachments). This can\'t be undone.',
    confirmLabel: 'Remove',
    destructive: true,
    onConfirm: async () => {
      await removeAccount(a.account_key)
      await refresh()
      toast.success(`Removed ${accountName(a)}`)
    },
  })

  return (
    <>
      <Section
        className="mt-0"
        title="Connected accounts"
        description="The messaging accounts Mingly syncs. Sign-in sessions are stored encrypted in your vault."
      >
        {!loaded ? (
          <p className="flex items-center gap-2 text-xs text-slate-400"><Loader2 size={12} className="animate-spin" />Loading…</p>
        ) : error != null && accounts.length === 0 ? (
          <Card>
            <p role="alert" className="text-xs text-red-400">Couldn't load accounts: {errorMessage(error)}</p>
            <button type="button" onClick={() => void refresh()} className={`mt-3 ${actionButton}`}>Retry</button>
          </Card>
        ) : connected.length === 0 ? (
          <Card><p className="text-xs text-slate-400">No accounts connected yet. Add one below.</p></Card>
        ) : (
          <div className="flex flex-col gap-2">
            {connected.map((a) => (
              <AccountRow
                key={a.account_key}
                account={a}
                removable={isMulti(a.channel)}
                onReauth={() => setFlow({ key: a.account_key, title: `Sign in to ${accountName(a)}` })}
                // Telegram signs in with your own API id/hash; this re-enters them
                onChangeCredentials={a.channel === 'telegram'
                  ? () => setFlow({ key: a.account_key, title: 'Change Telegram API credentials', params: { reset_credentials: true } })
                  : undefined}
                onLogout={() => void logOut(a)}
                onRemove={() => void remove(a)}
              />
            ))}
          </div>
        )}
      </Section>

      {loaded && addable.length > 0 && (
        <Section title="Add an account" description="Connect another channel. Each one signs in its own way; follow the steps.">
          <div className="flex flex-wrap gap-2">
            {addable.map((c) => (
              <AddButton key={c.channel} channel={c} onClick={() => setFlow({ key: c.channel, title: `Connect ${c.display_name}` })} />
            ))}
          </div>
        </Section>
      )}

      {flow && (
        <Modal open onClose={() => setFlow(null)} title={flow.title} size="md" closeOnBackdrop={false}>
          <div className="p-5">
            <AuthFlow
              accountKey={flow.key}
              params={flow.params}
              onDone={() => void refresh()}
              // Leaving a background setup refreshes too: its button shows the progress
              onClose={() => { setFlow(null); void refresh() }}
              doneLabel="Done"
            />
          </div>
        </Modal>
      )}
    </>
  )
}

function accountName(a: Account) {
  return a.label ? `${a.display_name} (${a.label})` : a.display_name
}

function AccountRow({ account: a, removable, onReauth, onChangeCredentials, onLogout, onRemove }: {
  account: Account
  removable: boolean
  onReauth: () => void
  onChangeCredentials?: () => void
  onLogout: () => void
  onRemove: () => void
}) {
  const state = a.status.state
  const signedOut = state === 'unauthorized'
  return (
    <Card>
      <div className="flex items-center gap-3">
        <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg bg-slate-700">
          <ChannelIcon channel={a.channel} size={16} className="text-slate-300" />
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm text-slate-200">{a.display_name}</p>
          <p className="flex items-center gap-1.5 text-xs text-slate-400">
            {a.label && <span className="truncate">{a.label} ·</span>}
            <span className={`inline-block h-1.5 w-1.5 flex-shrink-0 rounded-full ${STATE_DOT[state] ?? 'bg-slate-500'}`} />
            <span data-testid="account-state">{STATE_LABEL[state] ?? state}</span>
          </p>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <button type="button" onClick={onReauth} className={actionButton}>
            {signedOut ? 'Sign in' : 'Re-authorize'}
          </button>
          {onChangeCredentials && (
            <button type="button" onClick={onChangeCredentials} className={actionButton}>Change API credentials</button>
          )}
          {!signedOut && <button type="button" onClick={onLogout} className={actionButton}>Log out</button>}
          {removable && <button type="button" onClick={onRemove} className={dangerButton}>Remove</button>}
        </div>
      </div>
      {a.status.error && (state === 'error' || state === 'unauthorized' || state === 'reconnecting') && (
        <p role="alert" className="mt-2 text-xs text-red-400">{a.status.error}</p>
      )}
    </Card>
  )
}

function AddButton({ channel: c, onClick }: { channel: ConnectableChannel; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex items-center gap-2 rounded-lg border border-slate-600 bg-slate-800 px-3 py-2 text-xs text-slate-200 transition-colors hover:bg-slate-700"
    >
      <span aria-hidden><ChannelIcon channel={c.channel} size={14} className="text-slate-300" /></span>
      {c.multi_account ? `Add ${c.display_name} account` : `Connect ${c.display_name}`}
      <ChannelSetupNote setup={c.setup} />
      <Plus size={12} className="text-slate-500" />
    </button>
  )
}
