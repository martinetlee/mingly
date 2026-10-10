import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  CheckCircle2,
  StickyNote,
  ArrowRight,
  ChevronDown,
  ChevronRight,
  RefreshCw,
  Users,
  MessageSquare,
  AlarmClock,
  RotateCcw,
} from 'lucide-react'
import type { DashboardData, DashboardNeedsReply, CatchUpItem, CatchUpSummaryJob, RelationshipStatus } from '../../api/types'
import {
  snoozeCatchUpItem,
  markCatchUpHandled,
  reopenCatchUpItem,
  addCatchUpNote,
  clearCatchUpNote,
  updateCatchUpRelationship,
  triggerCatchUpSummaries,
  getCatchUpSummaryStatus,
} from '../../api/client'
import { useKeyboardShortcuts } from '../../hooks/useKeyboardShortcuts'
import { useWsEvent } from '../../hooks/useWebSocket'
import { reportError, toast } from '../../lib/toast'
import { fmtRelative, parseServerDate } from '../../utils/time'
import Markdown from '../ui/Markdown'
import ChannelIcon from '../channels/ChannelIcon'
import { openConversationUrl } from '../../lib/routes'
import { conversationTitle } from '../../lib/conversation'

const RELATIONSHIP_STATUSES: { value: RelationshipStatus; label: string; color: string }[] = [
  { value: 'active', label: 'Active', color: 'bg-emerald-500/20 text-emerald-400' },
  { value: 'nurturing', label: 'Nurturing', color: 'bg-sky-500/20 text-sky-400' },
  { value: 'stalled', label: 'Stalled', color: 'bg-amber-500/20 text-amber-400' },
  { value: 'cold', label: 'Cold', color: 'bg-slate-500/20 text-slate-400' },
  { value: 'new', label: 'New', color: 'bg-violet-500/20 text-violet-400' },
  { value: 'needs_reply', label: 'Needs Reply', color: 'bg-rose-500/20 text-rose-400' },
]

const SNOOZE_OPTIONS = [
  { hours: 1, label: '1 hour' },
  { hours: 4, label: '4 hours' },
  { hours: 24, label: '1 day' },
  { hours: 72, label: '3 days' },
  { hours: 168, label: '1 week' },
]

const SUMMARY_POLL_MS = 4000

// ---------------------------------------------------------------------------
// Optimistic triage update (pure; the server is the source of truth and the
// dashboard is refetched right after)
// ---------------------------------------------------------------------------

const isActiveTriage = (t: CatchUpItem | null, now: Date): boolean => {
  if (!t) return true
  if (t.status === 'handled') return false
  if (t.status === 'snoozed') {
    const until = parseServerDate(t.snoozed_until)
    return !until || until <= now
  }
  return true
}

/** Move a chat between needs_reply / recently_handled after a triage change. */
export function applyTriageUpdate(data: DashboardData, triage: CatchUpItem, now: Date = new Date()): DashboardData {
  const conversationId = triage.conversation_id
  const all = [...data.needs_reply, ...(data.recently_handled ?? [])]
  const existing = all.find((it) => it.conversation.id === conversationId)
  if (!existing) return data
  const updated: DashboardNeedsReply = { ...existing, triage }
  const needs = data.needs_reply.filter((it) => it.conversation.id !== conversationId)
  const handled = (data.recently_handled ?? []).filter((it) => it.conversation.id !== conversationId)
  if (triage.status === 'handled') {
    handled.unshift(updated)
  } else if (isActiveTriage(triage, now)) {
    needs.push(updated)
    needs.sort((a, b) => (parseServerDate(b.last_message.date)?.getTime() ?? 0) - (parseServerDate(a.last_message.date)?.getTime() ?? 0))
  }
  // A future snooze drops out of both lists
  return { ...data, needs_reply: needs, recently_handled: handled }
}

// ---------------------------------------------------------------------------
// Note editor — seeded from the saved note every time it opens
// ---------------------------------------------------------------------------

export function NoteEditor({
  savedNote,
  onSave,
  onClear,
  onClose,
}: {
  savedNote: string | null
  onSave: (note: string) => Promise<boolean>
  onClear: () => Promise<boolean>
  onClose: () => void
}) {
  const [text, setText] = useState(savedNote ?? '')
  const [saving, setSaving] = useState(false)
  const trimmed = text.trim()

  const run = async (fn: () => Promise<boolean>) => {
    setSaving(true)
    const ok = await fn()
    setSaving(false)
    if (ok) onClose()
  }

  return (
    <div className="rounded-lg bg-slate-700/30 p-2 space-y-2" onClick={(e) => e.stopPropagation()}>
      <p className="text-[10px] text-slate-500 uppercase tracking-wider">Note</p>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Add a note about this conversation..."
        aria-label="Note"
        className="w-full rounded-md bg-slate-800 border border-slate-600 px-3 py-2 text-xs text-slate-200 placeholder:text-slate-500 resize-none focus:outline-none focus:border-indigo-500/50"
        rows={2}
        autoFocus
      />
      <div className="flex items-center gap-1.5">
        {savedNote && (
          <button
            onClick={() => void run(onClear)}
            disabled={saving}
            className="rounded-md px-2.5 py-1 text-[11px] text-rose-400 hover:bg-rose-900/30 disabled:opacity-50 transition-colors"
          >
            Delete note
          </button>
        )}
        <div className="ml-auto flex gap-1.5">
          <button onClick={onClose} className="rounded-md px-2.5 py-1 text-[11px] text-slate-400 hover:text-slate-200 transition-colors">
            Cancel
          </button>
          <button
            onClick={() => void run(() => onSave(trimmed))}
            disabled={saving || !trimmed || trimmed === (savedNote ?? '')}
            className="rounded-md bg-indigo-500/20 hover:bg-indigo-500/30 px-2.5 py-1 text-[11px] font-medium text-indigo-300 disabled:opacity-50 transition-colors"
          >
            Save note
          </button>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Single triage card
// ---------------------------------------------------------------------------

interface TriageCardProps {
  item: DashboardNeedsReply
  selected: boolean
  onSnooze: (conversationId: number, hours: number) => void
  onHandled: (conversationId: number) => void
  onSaveNote: (conversationId: number, note: string) => Promise<boolean>
  onClearNote: (conversationId: number) => Promise<boolean>
  onRelationship: (conversationId: number, status: string) => void
  onReply: (conversationId: number) => void
}

export function TriageCard({ item, selected, onSnooze, onHandled, onSaveNote, onClearNote, onRelationship, onReply }: TriageCardProps) {
  const [expanded, setExpanded] = useState(false)
  const [panel, setPanel] = useState<'snooze' | 'relationship' | 'note' | null>(null)
  const togglePanel = (p: NonNullable<typeof panel>) => setPanel((cur) => (cur === p ? null : p))

  const conversationId = item.conversation.id
  const note = item.triage?.note ?? null
  const context = item.context

  return (
    <div
      className={`rounded-xl border overflow-hidden transition-all ${
        selected ? 'border-indigo-500/50 ring-1 ring-indigo-500/30 bg-slate-800/80' : 'border-slate-700/60 bg-slate-800/60 hover:border-slate-600/60'
      }`}
    >
      <div
        role="button"
        tabIndex={0}
        onClick={() => setExpanded((v) => !v)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            setExpanded((v) => !v)
          }
        }}
        className="flex items-center gap-3 px-4 py-3 cursor-pointer select-none"
      >
        {expanded ? <ChevronDown size={14} className="shrink-0 text-slate-500" /> : <ChevronRight size={14} className="shrink-0 text-slate-500" />}
        <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-slate-700 text-sm font-medium text-slate-300">
          {conversationTitle(item.conversation).charAt(0).toUpperCase()}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center justify-between gap-2">
            <p className="truncate text-sm font-medium text-gray-200 flex items-center gap-1.5">
              {conversationTitle(item.conversation)}
              {item.conversation.kind === 'group' && <Users size={12} className="shrink-0 text-slate-500" />}
              <ChannelIcon channel={item.conversation.channel} />
            </p>
            <span className="flex-shrink-0 text-[10px] text-slate-500">{fmtRelative(item.last_message.date)}</span>
          </div>
          <p className="truncate text-xs text-slate-400 mt-0.5">
            {item.last_message.sender_name && <span className="text-slate-300">{item.last_message.sender_name}: </span>}
            {item.last_message.text || <span className="italic">[media]</span>}
          </p>
        </div>
        {note && <span title="Has note"><StickyNote size={12} className="shrink-0 text-amber-400/60" /></span>}
      </div>

      {expanded && (
        <div className="border-t border-slate-700/50 px-4 py-3 space-y-3">
          {/* Context summary */}
          <div className="rounded-lg bg-slate-700/30 px-3 py-2 space-y-2">
            <div className="flex items-center justify-between">
              <p className="flex items-center gap-1.5 text-[11px] text-slate-500 uppercase tracking-wider">
                Summary
                {context?.summary && context.degraded && (
                  <span
                    className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium normal-case tracking-normal text-amber-400"
                    title="The AI model was unavailable, so this is a simple extract of recent messages. It is replaced automatically once the model is back."
                  >
                    Basic summary — AI unavailable
                  </span>
                )}
              </p>
              {context?.generated_at && (
                <span className="text-[10px] text-slate-600">Generated {fmtRelative(context.generated_at)}</span>
              )}
            </div>
            {context?.summary ? (
              <Markdown content={context.summary} size="xs" className="text-slate-400" />
            ) : (
              <p className="text-xs italic text-slate-500">No summary yet. Use "Refresh summaries" to generate one.</p>
            )}
            {context && context.new_messages_since_summary > 0 && (
              <p className="text-[11px] font-medium text-indigo-400">
                +{context.new_messages_since_summary} new message{context.new_messages_since_summary !== 1 ? 's' : ''} since this summary
              </p>
            )}
            {context && context.summary_messages.length > 0 && (
              <div className="space-y-1 mt-1.5">
                {context.summary_messages.slice(0, 3).map((m, i) => (
                  <div key={i} className="rounded-md bg-slate-800/60 px-2.5 py-1.5 text-xs">
                    <div className="flex items-baseline justify-between gap-2 mb-0.5">
                      <span className="font-medium text-slate-300 truncate">{m.sender_name || 'Unknown'}</span>
                      {m.date && <span className="shrink-0 text-[10px] text-slate-500">{fmtRelative(m.date)}</span>}
                    </div>
                    <p className="text-slate-400 line-clamp-2">
                      {m.text || <span className="italic">{m.attachments?.length ? `[${m.attachments[0].kind}]` : '[no text]'}</span>}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Primary actions */}
          <div className="flex flex-wrap gap-1.5">
            <button
              onClick={(e) => { e.stopPropagation(); onReply(conversationId) }}
              className="flex items-center gap-1.5 rounded-lg bg-indigo-500/20 hover:bg-indigo-500/30 px-3 py-1.5 text-xs font-medium text-indigo-300 transition-colors"
            >
              <MessageSquare size={12} />
              Reply now
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); togglePanel('snooze') }}
              className="flex items-center gap-1.5 rounded-lg bg-amber-500/15 hover:bg-amber-500/25 px-3 py-1.5 text-xs font-medium text-amber-300 transition-colors"
            >
              <AlarmClock size={12} />
              Remind later
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); onHandled(conversationId) }}
              className="flex items-center gap-1.5 rounded-lg bg-emerald-500/15 hover:bg-emerald-500/25 px-3 py-1.5 text-xs font-medium text-emerald-300 transition-colors"
            >
              <CheckCircle2 size={12} />
              Mark handled
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); togglePanel('note') }}
              className="flex items-center gap-1.5 rounded-lg bg-slate-600/50 hover:bg-slate-600/70 px-3 py-1.5 text-xs font-medium text-slate-300 transition-colors"
            >
              <StickyNote size={12} />
              {note ? 'Edit note' : 'Add note'}
            </button>
            {item.conversation.kind === 'direct' && (
              <button
                onClick={(e) => { e.stopPropagation(); togglePanel('relationship') }}
                className="flex items-center gap-1.5 rounded-lg bg-violet-500/15 hover:bg-violet-500/25 px-3 py-1.5 text-xs font-medium text-violet-300 transition-colors"
              >
                <Users size={12} />
                Relationship
              </button>
            )}
          </div>

          {panel === 'snooze' && (
            <div className="flex flex-wrap gap-1.5 rounded-lg bg-slate-700/30 p-2">
              <p className="w-full text-[10px] text-slate-500 uppercase tracking-wider mb-1">Remind me in</p>
              {SNOOZE_OPTIONS.map((opt) => (
                <button
                  key={opt.hours}
                  onClick={(e) => { e.stopPropagation(); onSnooze(conversationId, opt.hours); setPanel(null) }}
                  className="rounded-md bg-slate-600/50 hover:bg-amber-500/20 hover:text-amber-300 px-2.5 py-1 text-[11px] text-slate-300 transition-colors"
                >
                  {opt.label}
                </button>
              ))}
            </div>
          )}

          {panel === 'relationship' && (
            <div className="flex flex-wrap gap-1.5 rounded-lg bg-slate-700/30 p-2">
              <p className="w-full text-[10px] text-slate-500 uppercase tracking-wider mb-1">Set relationship status</p>
              {RELATIONSHIP_STATUSES.map((s) => (
                <button
                  key={s.value}
                  onClick={(e) => { e.stopPropagation(); onRelationship(conversationId, s.value); setPanel(null) }}
                  className={`rounded-md px-2.5 py-1 text-[11px] font-medium transition-colors ${s.color} hover:opacity-80`}
                >
                  {s.label}
                </button>
              ))}
            </div>
          )}

          {/* Mounted on open, so it always starts from the saved note */}
          {panel === 'note' && (
            <NoteEditor
              savedNote={note}
              onSave={(text) => onSaveNote(conversationId, text)}
              onClear={() => onClearNote(conversationId)}
              onClose={() => setPanel(null)}
            />
          )}

          {note && panel !== 'note' && (
            <div className="rounded-lg bg-amber-500/5 border border-amber-500/20 px-3 py-2">
              <p className="text-[10px] text-amber-400/60 uppercase tracking-wider mb-0.5">Note</p>
              <p className="text-xs text-slate-300 whitespace-pre-wrap">{note}</p>
            </div>
          )}

          <button
            onClick={(e) => { e.stopPropagation(); onReply(conversationId) }}
            className="w-full rounded-lg bg-slate-700 hover:bg-slate-600 px-3 py-1.5 text-xs text-slate-300 transition-colors flex items-center justify-center gap-1.5"
          >
            Open conversation <ArrowRight size={11} />
          </button>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Queue
// ---------------------------------------------------------------------------

interface CatchUpQueueProps {
  /** Server-filtered (handled + future snoozes removed) */
  needsReply: DashboardNeedsReply[]
  recentlyHandled: DashboardNeedsReply[]
  /** Apply a triage change locally; the parent refetches the dashboard afterwards */
  onTriageChange: (item: CatchUpItem) => void
  onRefreshDashboard: () => void
  refreshing?: boolean
  /** Keyboard shortcuts only while the queue is the visible tab */
  shortcutsEnabled?: boolean
}

export default function CatchUpQueue({
  needsReply,
  recentlyHandled,
  onTriageChange,
  onRefreshDashboard,
  refreshing = false,
  shortcutsEnabled = true,
}: CatchUpQueueProps) {
  const navigate = useNavigate()
  const [selectedIdx, setSelectedIdx] = useState(0)
  const [actionFeedback, setActionFeedback] = useState<{ conversationId: number; msg: string } | null>(null)
  const [showHandled, setShowHandled] = useState(false)
  const selectedRef = useRef<HTMLDivElement | null>(null)

  // ---- Summary generation job (server-side, single-flight with the scheduler)
  const [summaryJob, setSummaryJob] = useState<CatchUpSummaryJob | null>(null)
  const generating = summaryJob?.status === 'running'
  const onRefreshRef = useRef(onRefreshDashboard)
  onRefreshRef.current = onRefreshDashboard

  const settleJob = useCallback((job: CatchUpSummaryJob) => {
    setSummaryJob(job)
    if (job.status === 'running') return
    if (job.status === 'error' || job.llm_error) {
      toast.error(`Summary generation failed: ${job.llm_error || job.error || 'unknown error'}`)
    }
    onRefreshRef.current()
  }, [])

  // Pick up a run already in progress (scheduled or from another window)
  useEffect(() => {
    getCatchUpSummaryStatus()
      .then((s) => { if (s.status === 'running') setSummaryJob(s) })
      .catch((e) => reportError(e, 'Could not check summary status'))
  }, [])

  // While running, poll status as a fallback for a missed WebSocket event
  useEffect(() => {
    if (!generating) return
    const t = setInterval(() => {
      getCatchUpSummaryStatus()
        .then((s) => { if (s.status !== 'running') settleJob(s) })
        .catch((e) => reportError(e, 'Could not check summary status'))
    }, SUMMARY_POLL_MS)
    return () => clearInterval(t)
  }, [generating, settleJob])

  useWsEvent('catchup_summaries_updated', () => {
    setSummaryJob((j) => (j?.status === 'running' ? { ...j, status: 'done' } : j))
    onRefreshRef.current()
  })

  const handleGenerateSummaries = async () => {
    if (generating) return
    try {
      const job = await triggerCatchUpSummaries(needsReply.map((item) => item.conversation.id))
      if (job.started === false && job.status === 'running') toast.info('Summaries are already being generated')
      settleJob(job)
    } catch (e) {
      reportError(e, 'Could not start summary generation')
    }
  }

  // ---- Selection
  useEffect(() => {
    if (needsReply.length > 0) setSelectedIdx((i) => Math.min(i, needsReply.length - 1))
  }, [needsReply.length])

  useEffect(() => {
    selectedRef.current?.scrollIntoView?.({ block: 'nearest' })
  }, [selectedIdx])

  const feedbackTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => {
    if (feedbackTimerRef.current) clearTimeout(feedbackTimerRef.current)
  }, [])

  const showFeedback = (conversationId: number, msg: string) => {
    setActionFeedback({ conversationId, msg })
    if (feedbackTimerRef.current) clearTimeout(feedbackTimerRef.current)
    feedbackTimerRef.current = setTimeout(() => setActionFeedback(null), 2000)
  }

  // ---- Actions
  const triage = async (conversationId: number, fn: () => Promise<CatchUpItem>, feedback: string, errorContext: string): Promise<boolean> => {
    try {
      onTriageChange(await fn())
      showFeedback(conversationId, feedback)
      return true
    } catch (e) {
      reportError(e, errorContext)
      return false
    }
  }

  const handleSnooze = (conversationId: number, hours: number) =>
    triage(conversationId, () => snoozeCatchUpItem(conversationId, hours), `Snoozed for ${hours >= 24 ? `${hours / 24}d` : `${hours}h`}`, 'Snooze failed')
  const handleHandled = (conversationId: number) =>
    triage(conversationId, () => markCatchUpHandled(conversationId), 'Marked as handled', 'Could not mark handled')
  const handleReopen = (conversationId: number) =>
    triage(conversationId, () => reopenCatchUpItem(conversationId), 'Reopened', 'Could not reopen')
  const handleSaveNote = (conversationId: number, note: string) =>
    triage(conversationId, () => addCatchUpNote(conversationId, note), 'Note saved', 'Could not save note')
  const handleClearNote = (conversationId: number) =>
    triage(conversationId, () => clearCatchUpNote(conversationId), 'Note deleted', 'Could not delete note')

  const handleRelationship = async (conversationId: number, status: string) => {
    try {
      await updateCatchUpRelationship(conversationId, status)
      showFeedback(conversationId, `Status: ${status}`)
      onRefreshDashboard()
    } catch (e) {
      reportError(e, 'Could not update relationship')
    }
  }

  const handleReply = (conversationId: number) => navigate(openConversationUrl(conversationId))

  const shortcuts = useMemo(() => ({
    j: () => setSelectedIdx((i) => Math.min(i + 1, needsReply.length - 1)),
    arrowdown: () => setSelectedIdx((i) => Math.min(i + 1, needsReply.length - 1)),
    k: () => setSelectedIdx((i) => Math.max(i - 1, 0)),
    arrowup: () => setSelectedIdx((i) => Math.max(i - 1, 0)),
    r: () => {
      const cur = needsReply[selectedIdx]
      if (!cur) return false
      handleReply(cur.conversation.id)
    },
    h: () => {
      const cur = needsReply[selectedIdx]
      if (!cur) return false
      void handleHandled(cur.conversation.id)
    },
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [needsReply, selectedIdx, navigate])

  useKeyboardShortcuts(shortcuts, { enabled: shortcutsEnabled, yieldScrollKeys: false })

  const handledSection = recentlyHandled.length > 0 && (
    <div className="mt-4">
      <button
        onClick={() => setShowHandled((v) => !v)}
        className="flex items-center gap-1.5 text-[11px] text-slate-500 hover:text-slate-400 transition-colors"
      >
        {showHandled ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        {recentlyHandled.length} handled
      </button>
      {showHandled && (
        <div className="mt-2 space-y-1">
          {recentlyHandled.map((item) => (
            <div key={item.conversation.id} className="flex items-center gap-3 rounded-lg border border-slate-700/40 bg-slate-800/40 px-3 py-2">
              <div className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full bg-slate-700/50 text-xs font-medium text-slate-400">
                {conversationTitle(item.conversation).charAt(0).toUpperCase()}
              </div>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm text-slate-400">{conversationTitle(item.conversation)}</p>
                {item.triage?.handled_at && (
                  <p className="text-[10px] text-slate-600">Handled {fmtRelative(item.triage.handled_at)}</p>
                )}
              </div>
              <button
                onClick={() => void handleReopen(item.conversation.id)}
                title="Reopen"
                className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] text-slate-500 hover:bg-slate-700 hover:text-slate-300 transition-colors"
              >
                <RotateCcw size={11} />
                Reopen
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )

  if (needsReply.length === 0) {
    return (
      <div>
        <div className="rounded-xl bg-slate-800 border border-slate-700/50 px-4 py-8 text-center">
          <CheckCircle2 size={24} className="mx-auto mb-2 text-emerald-400/50" />
          <p className="text-sm text-slate-400 font-medium">All caught up!</p>
          <p className="text-xs text-slate-500 mt-1">No conversations waiting for your attention.</p>
        </div>
        {handledSection}
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-xs text-slate-500">
            {needsReply.length} item{needsReply.length !== 1 ? 's' : ''} to triage
          </span>
          <span className="text-[10px] text-slate-600">|</span>
          <span className="text-[10px] text-slate-600">j/k to navigate, r to reply, h to handle</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => void handleGenerateSummaries()}
            disabled={generating}
            className="flex items-center gap-1 rounded-md bg-indigo-500/15 hover:bg-indigo-500/25 px-2 py-1 text-[10px] font-medium text-indigo-300 disabled:opacity-50 transition-colors"
            title="Regenerate all summaries now"
          >
            <RefreshCw size={10} className={generating ? 'animate-spin' : ''} />
            {generating
              ? `Generating${summaryJob?.total ? ` ${summaryJob.done ?? 0}/${summaryJob.total}` : ''}…`
              : 'Refresh summaries'}
          </button>
          <button
            onClick={onRefreshDashboard}
            disabled={refreshing}
            className="text-slate-500 hover:text-slate-300 disabled:opacity-50 transition-colors"
            title="Refresh"
          >
            <RefreshCw size={12} className={refreshing ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      <div className="space-y-2">
        {needsReply.map((item, i) => (
          <div key={item.conversation.id} ref={i === selectedIdx ? selectedRef : null} className="relative">
            <TriageCard
              item={item}
              selected={i === selectedIdx}
              onSnooze={handleSnooze}
              onHandled={handleHandled}
              onSaveNote={handleSaveNote}
              onClearNote={handleClearNote}
              onRelationship={handleRelationship}
              onReply={handleReply}
            />
            {actionFeedback?.conversationId === item.conversation.id && (
              <div className="absolute top-2 right-2 rounded-md bg-emerald-500/20 border border-emerald-500/30 px-2.5 py-1 text-[11px] font-medium text-emerald-400 animate-pulse z-10">
                {actionFeedback.msg}
              </div>
            )}
          </div>
        ))}
      </div>

      {handledSection}
    </div>
  )
}
