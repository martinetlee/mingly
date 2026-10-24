/**
 * Mail (/mail): the email client space. Account switcher, folders and labels
 * on the left, the thread list, and the reading pane (every message of the
 * thread, reply / reply all / archive / read state) or a new-mail composer.
 *
 * URL: ?conversationId=…&messageId=… opens a thread (links from search,
 * Home, catch-up, briefings use lib/routes mailUrl / conversationUrl).
 */
import { useCallback, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Archive, Inbox, Layers, Mail as MailIcon, PenSquare, Search, Send, Tag } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import type { Conversation } from '../api/types'
import { getMailLabels } from '../api/email'
import type { MailFolder, MailThread } from '../api/email'
import { useAccounts } from '../hooks/useAccounts'
import { useLatestQuery } from '../hooks/useLatestQuery'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import AccountGate from '../components/channels/AccountGate'
import { CONNECTIONS_PATH } from '../components/channels/NoAccountsNotice'
import { LinkifiedText, LoadError, StatusBanner } from '../components/ui'
import { errorMessage } from '../lib/toast'
import ThreadList from './mail/ThreadList'
import ThreadView from './mail/ThreadView'
import MailComposer from './mail/MailComposer'
import { useMailThreads } from './mail/useMailThreads'
import type { MailView } from './mail/useMailThreads'

const FOLDERS: { id: MailFolder; label: string; icon: LucideIcon }[] = [
  { id: 'inbox', label: 'Inbox', icon: Inbox },
  { id: 'sent', label: 'Sent', icon: Send },
  { id: 'archive', label: 'Archive', icon: Archive },
  { id: 'all', label: 'All mail', icon: Layers },
]
/** Connector states in which mail isn't syncing */
const PROBLEM_STATES: ReadonlySet<string> = new Set(['unauthorized', 'error', 'reconnecting', 'stopped'])

/** Labels already shown as folders */
const FOLDER_LABELS = new Set(['Inbox', 'Sent'])

export default function Mail() {
  const [params, setParams] = useSearchParams()
  const accountsState = useAccounts()
  const { accounts, loaded } = accountsState
  const mailAccounts = useMemo(
    () => accounts.filter((a) => a.channel === 'gmail' && a.configured && a.account_id != null),
    [accounts],
  )
  const [accountChoice, setAccountChoice] = useState<number | null>(null)
  // A removed account falls back to every account
  const accountId = accountChoice != null && mailAccounts.some((a) => a.account_id === accountChoice) ? accountChoice : null
  const [folder, setFolder] = useState<MailFolder>('inbox')
  const [label, setLabel] = useState<string | null>(null)
  const [composing, setComposing] = useState(false)

  const selectedId = params.get('conversationId') ? Number(params.get('conversationId')) : null
  const messageIdRaw = params.get('messageId')
  const messageId = messageIdRaw ? Number(messageIdRaw) : null

  const hasAccounts = mailAccounts.length > 0
  const view: MailView = { accountId, folder, label }
  const list = useMailThreads(view, hasAccounts)
  const labels = useLatestQuery(['mail-labels', accountId], (signal) => getMailLabels(accountId, signal), { enabled: hasAccounts })

  const open = useCallback((t: Pick<MailThread, 'id'>) => {
    setComposing(false)
    setParams({ conversationId: String(t.id) })
  }, [setParams])

  const myAddress = useCallback((accId: number) => {
    const acc = mailAccounts.find((a) => a.account_id === accId)
    return acc?.label?.toLowerCase() ?? null
  }, [mailAccounts])

  // Keep the list in step with the reading pane (read state, archive, labels)
  const onChanged = useCallback((conv: Conversation) => {
    list.patch(conv.id, (t) => {
      const next = { ...t, ...conv }
      if (label == null && folder === 'inbox' && conv.is_archived) return null
      if (label == null && folder === 'archive' && !conv.is_archived) return null
      if (label != null && !(conv.labels ?? []).includes(label)) return null
      return next
    })
  }, [list, folder, label])

  const chooseFolder = (f: MailFolder) => { setFolder(f); setLabel(null) }
  const composeAccount = accountId ?? mailAccounts[0]?.account_id ?? null
  const startCompose = () => { setComposing(true); setParams({}) }

  // Filter over the loaded threads (subject, senders, preview)
  const [filter, setFilter] = useState('')
  const filterRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLElement>(null)
  const shownThreads = useMemo(() => {
    const q = filter.trim().toLowerCase()
    if (!q) return list.threads
    return list.threads.filter((t) => [t.title, t.last_message?.text, ...t.thread.senders.map((x) => x.name)]
      .some((v) => (v ?? '').toLowerCase().includes(q)))
  }, [list.threads, filter])

  // j / k: next / previous thread (opens it, keeps it in view)
  const moveThread = (delta: 1 | -1) => {
    const idx = shownThreads.findIndex((t) => t.id === selectedId)
    const next = idx === -1 ? (delta > 0 ? shownThreads[0] : undefined) : shownThreads[idx + delta]
    if (!next) return false
    open(next)
    listRef.current?.querySelector<HTMLElement>(`[data-thread-id="${next.id}"]`)?.scrollIntoView?.({ block: 'nearest' })
  }
  useKeyboardShortcuts({
    j: () => moveThread(1),
    k: () => moveThread(-1),
    '/': () => { filterRef.current?.focus() },
    c: () => { if (composeAccount == null) return false; startCompose() },
  })
  // Accounts that can't sync right now (shown above the list, like Calendar's sync errors)
  const problems = mailAccounts.filter((a) => (accountId == null || a.account_id === accountId) && PROBLEM_STATES.has(a.status.state))
  const viewTitle = label ?? FOLDERS.find((f) => f.id === folder)?.label ?? 'Mail'
  const extraLabels = (labels.data ?? []).filter((l) => !FOLDER_LABELS.has(l.label))

  if (!hasAccounts) {
    return <AccountGate state={accountsState} channels={['gmail']} title="No email account connected" body="Connect a Gmail account to read and send email here." />
  }

  const navCls = (active: boolean) => `flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-sm transition-colors ${
    active ? 'bg-indigo-500/20 text-indigo-300' : 'text-slate-400 hover:bg-slate-700/50 hover:text-slate-200'
  }`

  return (
    <div className="flex flex-1 overflow-hidden" data-testid="mail-page">
      <aside className="flex w-52 flex-shrink-0 flex-col gap-3 overflow-y-auto border-r border-slate-700/50 bg-slate-800 p-3">
        {mailAccounts.length > 1 ? (
          <select
            value={accountId ?? ''}
            onChange={(e) => setAccountChoice(e.target.value ? Number(e.target.value) : null)}
            aria-label="Account"
            className="w-full rounded-lg border border-slate-600 bg-slate-700 px-2 py-1.5 text-xs text-slate-200 outline-none"
          >
            <option value="">All accounts</option>
            {mailAccounts.map((a) => <option key={a.account_key} value={a.account_id!}>{a.label ?? a.account_key}</option>)}
          </select>
        ) : (
          <p className="flex items-center gap-1.5 truncate px-1 text-xs text-slate-400" title={mailAccounts[0]?.label ?? ''}>
            <MailIcon size={13} className="flex-shrink-0" /> {mailAccounts[0]?.label ?? 'Mail'}
          </p>
        )}
        <button
          type="button"
          onClick={startCompose}
          disabled={composeAccount == null}
          className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-indigo-500 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-50"
        >
          <PenSquare size={14} /> Compose
        </button>
        <nav className="flex flex-col gap-0.5" aria-label="Folders">
          {FOLDERS.map(({ id, label: name, icon: Icon }) => (
            <button key={id} type="button" onClick={() => chooseFolder(id)} className={navCls(label == null && folder === id)}>
              <Icon size={14} /> {name}
            </button>
          ))}
        </nav>
        {extraLabels.length > 0 && (
          <nav className="flex flex-col gap-0.5" aria-label="Labels">
            <p className="px-2.5 pt-1 text-[10px] font-medium uppercase tracking-wider text-slate-500">Labels</p>
            {extraLabels.map((l) => (
              <button key={l.label} type="button" onClick={() => setLabel(l.label)} className={navCls(label === l.label)}>
                <Tag size={13} />
                <span className="min-w-0 flex-1 truncate">{l.label}</span>
                <span className="text-[11px] text-slate-500">{l.count}</span>
              </button>
            ))}
          </nav>
        )}
      </aside>

      <section ref={listRef} className="flex w-80 flex-shrink-0 flex-col border-r border-slate-700/50 bg-slate-800/60">
        <header className="flex flex-col gap-2 border-b border-slate-700/50 px-3 py-2.5">
          <span className="text-sm font-medium text-slate-200">{viewTitle}</span>
          <label className="flex items-center gap-1.5 rounded-lg border border-slate-600 bg-slate-700/60 px-2 py-1">
            <Search size={12} className="flex-shrink-0 text-slate-500" />
            <input
              ref={filterRef}
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Escape') { e.stopPropagation(); setFilter(''); e.currentTarget.blur() } }}
              placeholder="Filter loaded threads  (/)"
              aria-label="Filter threads"
              className="min-w-0 flex-1 bg-transparent text-xs text-slate-200 placeholder-slate-500 outline-none"
            />
          </label>
        </header>
        {problems.map((a) => (
          <StatusBanner key={a.account_key} className="mx-3 mt-2" testId="mail-account-problem">
            {mailAccounts.length > 1 ? `${a.label ?? a.account_key}: ` : ''}
            {a.status.state === 'unauthorized' ? 'Signed out — new mail won’t arrive. ' : a.status.state === 'reconnecting' ? 'Reconnecting… ' : ''}
            <LinkifiedText text={a.status.error ?? (a.status.state === 'stopped' ? 'Sync is stopped.' : '')} />
            {a.status.state === 'unauthorized' && <Link to={CONNECTIONS_PATH} className="ml-1 underline hover:text-amber-100">Reconnect</Link>}
          </StatusBanner>
        ))}
        {list.error != null && list.threads.length === 0 ? (
          <LoadError message={`Couldn’t load mail: ${errorMessage(list.error)}`} onRetry={() => { void list.reload() }} />
        ) : (
          <ThreadList
            threads={shownThreads}
            selectedId={composing ? null : selectedId}
            loading={list.loading || !loaded}
            hasMore={list.hasMore}
            loadingMore={list.loadingMore}
            onOpen={open}
            onLoadMore={() => void list.loadMore()}
            emptyText={filter.trim() ? 'No loaded thread matches' : label ? `No threads labelled ${label}` : `Nothing in ${viewTitle}`}
          />
        )}
      </section>

      <main className="flex min-w-0 flex-1 flex-col overflow-hidden bg-slate-900">
        {composing && composeAccount != null ? (
          <div className="flex-1 overflow-y-auto p-5">
            <h2 className="mb-3 text-base font-semibold text-slate-100">New message</h2>
            {mailAccounts.length > 1 && (
              <p className="mb-2 text-xs text-slate-400">From {myAddress(composeAccount) ?? 'this account'} (switch accounts on the left)</p>
            )}
            <MailComposer
              mode={{ kind: 'new', accountId: composeAccount }}
              initial={{ to: [], cc: [], subject: '' }}
              onSent={(conversationId) => { setComposing(false); list.reload(); open({ id: conversationId }) }}
              onCancel={() => setComposing(false)}
            />
          </div>
        ) : selectedId != null && Number.isFinite(selectedId) ? (
          <ThreadView key={selectedId} conversationId={selectedId} messageId={messageId} me={myAddress} onChanged={onChanged} />
        ) : (
          <div className="flex flex-1 items-center justify-center text-sm text-slate-500">Select an email to read it</div>
        )}
      </main>
    </div>
  )
}
