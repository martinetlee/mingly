import { Fragment, memo, useCallback, useEffect, useRef, useState } from 'react'
import type { MouseEvent as ReactMouseEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ChevronDown, Cloud, RefreshCw, Shield as ShieldIcon, ShieldAlert, ShieldCheck, UserCheck, UserX, X } from 'lucide-react'
import type { ScamManualVerdict, ScamVerdictResponse, ScannedMessage } from '../../api/types'
import { useSelection } from '../../hooks/useSelection'
import { useFlaggedSenders } from '../../hooks/useFlaggedSenders'
import { fmtRelative } from '../../utils/time'
import { errorMessage } from '../../lib/toast'
import { openConversationUrl, personUrl } from '../../lib/routes'
import type { ScannedList } from './useScannedList'
import { useVerdictActions } from './useVerdictActions'
import type { useRescan } from './useRescan'
import TraceDetail from './TraceDetail'
import { ConfidencePill, EmptyState, ErrorState, LoadingRow, SyncBadge, VerdictPills, hasResult, messageKey, parseTrace, previewText, stripFormatting } from './shared'

interface RowProps {
  msg: ScannedMessage
  selected: boolean
  traceOpen: boolean
  rescanning: boolean
  devMode: boolean
  onRowClick: (e: ReactMouseEvent, msg: ScannedMessage) => void
  onToggle: (id: string) => void
  onContextMenu: (e: ReactMouseEvent, msg: ScannedMessage) => void
  onToggleTrace: (id: string) => void
  onRescan: (conversationId: number, messageId: number) => void
  onOpen: (conversationId: number, messageId: number) => void
}

/** One scanned-message row (+ its expandable trace). Shared by the Scanned and Unmarked tabs. */
const ScannedRow = memo(function ScannedRow({
  msg, selected, traceOpen, rescanning, devMode, onRowClick, onToggle, onContextMenu, onToggleTrace, onRescan, onOpen,
}: RowProps) {
  const id = messageKey(msg)
  const trace = parseTrace(msg)
  const failed = msg.scam_status === 'failed'
  const retryTitle = failed
    ? `Retry scan${msg.scam_attempts ? ` (${msg.scam_attempts} attempt${msg.scam_attempts === 1 ? '' : 's'} so far)` : ''}`
    : 'Rescan message'
  return (
    <Fragment>
      <tr
        className={`hover:bg-slate-800/50 transition-colors cursor-pointer select-none ${selected ? 'bg-indigo-500/10' : ''}`}
        onClick={(e) => onRowClick(e, msg)}
        onContextMenu={(e) => onContextMenu(e, msg)}
      >
        <td className="px-2 py-2.5">
          <input type="checkbox" checked={selected} onChange={() => onToggle(id)} className="rounded border-slate-600 bg-slate-700 text-indigo-500 focus:ring-0 h-3 w-3" />
        </td>
        <td className="px-6 py-2.5">
          {msg.sender_person_id != null ? (
            <Link to={personUrl(msg.sender_person_id)} className="text-xs text-slate-200 hover:text-indigo-300 truncate max-w-[140px] block">
              {msg.sender_name || `ID: ${msg.sender_identity_id ?? msg.sender_person_id}`}
            </Link>
          ) : (
            <span className="text-xs text-slate-200 truncate max-w-[140px] block">
              {msg.sender_name || (msg.sender_identity_id ? `ID: ${msg.sender_identity_id}` : '—')}
            </span>
          )}
          {msg.date && <span className="text-[10px] text-slate-500">{fmtRelative(msg.date)}</span>}
        </td>
        <td className="px-3 py-2.5">
          <button
            onClick={() => onOpen(msg.conversation_id, msg.message_id)}
            className="text-xs text-indigo-400 hover:text-indigo-300 truncate max-w-[160px] block text-left"
            title={msg.conversation_title}
          >
            {msg.conversation_title}
          </button>
        </td>
        <td className="px-3 py-2.5">
          <VerdictPills m={msg} shortCircuitNode={trace?.short_circuit?.node_id} />
        </td>
        <td className="px-3 py-2.5">
          <span className="text-xs text-slate-400 truncate max-w-[280px] block" title={msg.text ? stripFormatting(msg.text) : undefined}>
            {previewText(msg.text)}
          </span>
        </td>
        <td className="px-3 py-2.5">
          {hasResult(msg) ? <ConfidencePill value={msg.scam_score} /> : <span className="text-[10px] text-slate-500">—</span>}
        </td>
        <td className="px-2 py-2.5 text-center">
          <SyncBadge synced={msg.synced_to_cloud} />
        </td>
        <td className="px-3 py-2.5">
          <div className="flex items-center gap-0.5">
            <button
              onClick={() => onToggleTrace(id)}
              title="View pipeline trace"
              className={`rounded p-1 transition-colors ${traceOpen ? 'text-indigo-400 bg-slate-700' : 'text-slate-500 hover:bg-slate-700 hover:text-slate-300'}`}
            >
              <ChevronDown size={13} className={`transition-transform ${traceOpen ? 'rotate-180' : ''}`} />
            </button>
            <button
              onClick={() => onRescan(msg.conversation_id, msg.message_id)}
              title={retryTitle}
              disabled={rescanning}
              className={`rounded p-1 transition-colors disabled:opacity-40 ${failed ? 'text-orange-400 hover:bg-slate-700 hover:text-orange-300' : 'text-slate-500 hover:bg-slate-700 hover:text-indigo-400'}`}
            >
              <RefreshCw size={13} className={rescanning ? 'animate-spin' : ''} />
            </button>
          </div>
        </td>
      </tr>
      {traceOpen && (
        <tr className="bg-slate-800/30">
          <td colSpan={8} className="px-6 py-3">
            <TraceDetail msg={msg} trace={trace} devMode={devMode} />
          </td>
        </tr>
      )}
    </Fragment>
  )
})

interface MenuState { x: number; y: number; msgs: ScannedMessage[] }

function ContextMenu({ menu, flaggedIds, onClose, onVerdict, onFlagSender, onDismissSender }: {
  menu: MenuState
  flaggedIds: Set<number>
  onClose: () => void
  onVerdict: (msgs: ScannedMessage[], v: ScamManualVerdict) => void
  onFlagSender: (id: number) => void
  onDismissSender: (id: number, name: string | null) => void
}) {
  useEffect(() => {
    const close = () => onClose()
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('click', close)
    window.addEventListener('keydown', onKey)
    window.addEventListener('scroll', close, true)
    return () => {
      window.removeEventListener('click', close)
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('scroll', close, true)
    }
  }, [onClose])

  const senders = new Set(menu.msgs.map((m) => m.sender_identity_id))
  const singleSender = senders.size === 1 ? menu.msgs[0].sender_identity_id : null
  const senderName = menu.msgs[0]?.sender_name ?? null
  const item = 'flex w-full items-center gap-2 px-3 py-1.5 text-xs hover:bg-slate-700'
  const n = menu.msgs.length
  const suffix = n > 1 ? ` (${n})` : ''
  return (
    <div
      role="menu"
      className="fixed z-50 bg-slate-800 border border-slate-700 rounded-lg shadow-xl py-1 min-w-[180px]"
      style={{ top: menu.y, left: menu.x }}
      onClick={(e) => e.stopPropagation()}
    >
      <button onClick={() => onVerdict(menu.msgs, 'scam')} className={`${item} text-red-300`}>
        <ShieldAlert size={12} /> Mark as scam{suffix}
      </button>
      <button onClick={() => onVerdict(menu.msgs, 'not_scam')} className={`${item} text-blue-300`}>
        <ShieldCheck size={12} /> Mark as not scam{suffix}
      </button>
      {menu.msgs.some((m) => m.scam_manual_verdict) && (
        <button onClick={() => onVerdict(menu.msgs, null)} className={`${item} text-slate-400`}>
          <X size={12} /> Clear manual verdict{suffix}
        </button>
      )}
      {singleSender != null && (
        <>
          <div className="my-1 border-t border-slate-700" />
          {flaggedIds.has(singleSender) ? (
            <button onClick={() => onDismissSender(singleSender, senderName)} className={`${item} text-slate-300`}>
              <UserCheck size={12} /> Unflag sender
            </button>
          ) : (
            <button onClick={() => onFlagSender(singleSender)} className={`${item} text-red-300`}>
              <UserX size={12} /> Flag sender as scammer
            </button>
          )}
        </>
      )}
    </div>
  )
}

export interface ScannedTableProps {
  list: ScannedList
  devMode: boolean
  rescan: ReturnType<typeof useRescan>
  emptyTitle: string
  emptyHint?: string
}

export default function ScannedTable({ list, devMode, rescan, emptyTitle, emptyHint }: ScannedTableProps) {
  const navigate = useNavigate()
  const { ids: flaggedIds } = useFlaggedSenders()
  const { rows, total, hasMore, loading, error, refetch, patchRows, loadMore } = list
  const selection = useSelection(rows, messageKey)
  const [openTrace, setOpenTrace] = useState<string | null>(null)
  const [menu, setMenu] = useState<MenuState | null>(null)

  const onRows = useCallback((r: ScamVerdictResponse) => patchRows(r.messages), [patchRows])
  const verdicts = useVerdictActions(onRows)

  const applyVerdict = useCallback(async (msgs: ScannedMessage[], v: ScamManualVerdict) => {
    setMenu(null)
    const r = await verdicts.setMessages(msgs, v)
    if (r) selection.clear()
  }, [verdicts, selection])

  const onOpen = useCallback((conversationId: number, messageId: number) => {
    navigate(openConversationUrl(conversationId, messageId))
  }, [navigate])

  const { click, toggle } = selection
  const onRowClick = useCallback((e: ReactMouseEvent, msg: ScannedMessage) => {
    if ((e.target as HTMLElement).closest('button, a, input')) return
    click(messageKey(msg), { shift: e.shiftKey })
  }, [click])

  const { selectedIds } = selection
  // Read through a ref so the handler (and memoized rows) stay stable
  const selectionRef = useRef(selection)
  selectionRef.current = selection
  const onContextMenu = useCallback((e: ReactMouseEvent, msg: ScannedMessage) => {
    e.preventDefault()
    const sel = selectionRef.current
    // Right-click inside the selection acts on the whole selection, else on this row
    const msgs = sel.isSelected(messageKey(msg)) ? sel.selectedItems : [msg]
    setMenu({ x: Math.min(e.clientX, window.innerWidth - 220), y: Math.min(e.clientY, window.innerHeight - 180), msgs })
  }, [])

  const onToggleTrace = useCallback((id: string) => setOpenTrace((cur) => (cur === id ? null : id)), [])
  const closeMenu = useCallback(() => setMenu(null), [])

  if (loading) return <LoadingRow />
  if (error && !rows.length) return <ErrorState message={`Couldn't load scanned messages: ${errorMessage(error)}`} onRetry={() => { void refetch() }} />
  if (!rows.length) return <EmptyState icon={<ShieldIcon size={32} />} title={emptyTitle} hint={emptyHint} />

  return (
    <>
      {selection.count > 0 && (
        <div className="sticky top-0 z-20 flex items-center gap-2 bg-indigo-950/90 border-b border-indigo-500/30 px-4 py-2">
          <span className="text-xs text-indigo-300">{selection.count} selected</span>
          <button disabled={verdicts.pending} onClick={() => applyVerdict(selection.selectedItems, 'scam')} className="rounded bg-red-500/20 px-2 py-1 text-[10px] font-medium text-red-300 hover:bg-red-500/30 disabled:opacity-40">Mark as scam</button>
          <button disabled={verdicts.pending} onClick={() => applyVerdict(selection.selectedItems, 'not_scam')} className="rounded bg-blue-500/20 px-2 py-1 text-[10px] font-medium text-blue-300 hover:bg-blue-500/30 disabled:opacity-40">Mark as not scam</button>
          <button disabled={verdicts.pending} onClick={() => applyVerdict(selection.selectedItems, null)} className="rounded bg-slate-700 px-2 py-1 text-[10px] font-medium text-slate-300 hover:bg-slate-600 disabled:opacity-40">Clear verdict</button>
          <button onClick={selection.clear} className="ml-auto text-[10px] text-slate-500 hover:text-slate-300">Deselect all</button>
        </div>
      )}
      <table className="w-full text-left">
        <thead className="sticky top-0 z-10 bg-slate-900 border-b border-slate-700">
          <tr className="text-[10px] uppercase tracking-wider text-slate-500">
            <th className="px-2 py-2 w-6">
              <input type="checkbox" checked={selection.allSelected} onChange={selection.toggleAll} title="Select all loaded" className="rounded border-slate-600 bg-slate-700 text-indigo-500 focus:ring-0 h-3 w-3" />
            </th>
            <th className="px-6 py-2 font-medium">Sent By</th>
            <th className="px-3 py-2 font-medium">Chat</th>
            <th className="px-3 py-2 font-medium">Verdict</th>
            <th className="px-3 py-2 font-medium">Message</th>
            <th className="px-3 py-2 font-medium">Confidence</th>
            <th className="px-2 py-2 font-medium w-8" title="Cloud sync status"><Cloud size={10} /></th>
            <th className="px-3 py-2 font-medium w-8"></th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800">
          {rows.map((msg) => {
            const id = messageKey(msg)
            return (
              <ScannedRow
                key={id}
                msg={msg}
                selected={selectedIds.has(id)}
                traceOpen={openTrace === id}
                rescanning={rescan.isRescanning(id)}
                devMode={devMode}
                onRowClick={onRowClick}
                onToggle={toggle}
                onContextMenu={onContextMenu}
                onToggleTrace={onToggleTrace}
                onRescan={rescan.rescan}
                onOpen={onOpen}
              />
            )
          })}
        </tbody>
      </table>

      <div className="flex items-center justify-center gap-3 border-t border-slate-700 py-3">
        <span className="text-[10px] text-slate-500">Showing {rows.length} of {total}</span>
        {hasMore && (
          <button
            disabled={loadMore.pending}
            onClick={() => { void loadMore.run() }}
            className="rounded px-3 py-1 text-xs text-slate-400 hover:bg-slate-800 disabled:opacity-40"
          >
            {loadMore.pending ? 'Loading…' : 'Load more'}
          </button>
        )}
      </div>

      {menu && (
        <ContextMenu
          menu={menu}
          flaggedIds={flaggedIds}
          onClose={closeMenu}
          onVerdict={applyVerdict}
          onFlagSender={(id) => { setMenu(null); void verdicts.flagSender(id) }}
          onDismissSender={(id, name) => { setMenu(null); void verdicts.dismissSender(id, name) }}
        />
      )}
    </>
  )
}
