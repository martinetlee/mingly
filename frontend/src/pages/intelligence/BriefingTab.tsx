import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertCircle, ChevronDown, ChevronRight, Loader2, Newspaper, RotateCcw, Trash2, X } from 'lucide-react'
import {
  deleteBriefingRun,
  getBriefingConversations,
  getBriefingHistory,
  getBriefingPrompts,
  getPrefs,
  setBriefing,
  setBriefingDay,
  setBriefingPrompts,
} from '../../api/client'
import type { BriefingHistoryEntry, BriefingRun, Conversation, Prefs } from '../../api/types'
import { conversationTitle, kindLabel } from '../../lib/conversation'
import ChannelIcon from '../../components/channels/ChannelIcon'
import { BriefingEntryList, type RegenerateControls } from '../../components/intelligence/BriefingEntryCard'
import { useConfirm } from '../../components/ui'
import { useLatestQuery } from '../../hooks/useLatestQuery'
import { useSettingField } from '../../hooks/useSettingField'
import { WEEKDAYS, dayLabel } from '../../features/briefing/schedule'
import { useWsEvent } from '../../hooks/useWebSocket'
import { reportError, toast } from '../../lib/toast'
import { fmtDateTime } from '../../utils/time'
import { describeBriefingWindow, useBriefingJob, useRegenerateEntry, type BriefingOutcome } from './useBriefingJob'

const TIMEFRAME_OPTIONS = [
  { label: '1d', value: 1 },
  { label: '3d', value: 3 },
  { label: '7d', value: 7 },
  { label: '14d', value: 14 },
  { label: '30d', value: 30 },
]

// Same day list/labels as Settings → Briefing
const DAY_OPTIONS = WEEKDAYS.map((value) => ({ value, label: dayLabel(value).slice(0, 3) }))

/** Check for a scheduled (or other-window) briefing while idle */
const STATUS_POLL_MS = 30_000

const windowLabel = (days: number) =>
  days === 1 ? '24h' : `${days} days`

const RUN_STATUS_BADGE: Record<string, string> = {
  running: 'bg-emerald-500/15 text-emerald-400',
  partial: 'bg-amber-500/15 text-amber-400',
  failed: 'bg-rose-500/15 text-rose-400',
}

function fmtScheduleTime(hhmm: string): string {
  const [h, m] = hhmm.split(':').map(Number)
  if (!Number.isFinite(h) || !Number.isFinite(m)) return hhmm
  const d = new Date()
  d.setHours(h, m, 0, 0)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export default function BriefingTab() {
  const confirm = useConfirm()

  // History
  const historyQuery = useLatestQuery(['briefing-history'], (signal) => getBriefingHistory(signal))
  const history = useMemo(() => historyQuery.data ?? [], [historyQuery.data])
  const { refetch: refetchHistory, setData: setHistory } = historyQuery
  const [expandedRunId, setExpandedRunId] = useState<number | null>(null)

  // Manual generation timeframe (independent of the schedule)
  const [selectedDays, setSelectedDays] = useState(1)
  const [showFlow, setShowFlow] = useState(false)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

  const onFinished = useCallback(async (outcome: BriefingOutcome) => {
    if (outcome.kind === 'conflict') return // shown as a banner with Join / Cancel
    if (outcome.kind === 'done') {
      setLastUpdated(new Date())
      if (outcome.status === 'empty') toast.info('Nothing to brief in this period.')
      else if (outcome.status === 'partial') toast.info(`Briefing finished, but ${outcome.failed} chat${outcome.failed === 1 ? '' : 's'} could not be summarized.`)
    } else if (outcome.kind === 'cancelled') {
      toast.info('Briefing stopped. Summaries generated so far were kept.')
    } else if (outcome.kind === 'lost') {
      toast.error('Lost connection to the briefing. Check the history below.')
    }
    const runs = await refetchHistory()
    const runId = outcome.kind === 'done' || outcome.kind === 'error' ? outcome.runId : null
    if (runId != null) setExpandedRunId(runId)
    else if (outcome.kind === 'done' && runs?.length) setExpandedRunId(runs[0].id)
  }, [refetchHistory])

  const job = useBriefingJob(onFinished)
  const { state: run } = job

  // Attach to a running job on mount, and poll for scheduled runs while idle
  const { attachIfRunning } = job
  useEffect(() => {
    void attachIfRunning()
  }, [attachIfRunning])
  useEffect(() => {
    if (run.running) return
    const t = setInterval(() => { void attachIfRunning() }, STATUS_POLL_MS)
    return () => clearInterval(t)
  }, [run.running, attachIfRunning])
  useWsEvent('resync', () => {
    void refetchHistory()
    void attachIfRunning()
  })

  // Regenerate a single entry
  const onEntryUpdated = useCallback((runId: number, updated: BriefingHistoryEntry) => {
    setHistory((prev) => prev?.map((r) =>
      r.id !== runId ? r : { ...r, entries: r.entries.map((e) => (e.id === updated.id ? updated : e)) },
    ))
  }, [setHistory])
  const regen = useRegenerateEntry(onEntryUpdated)
  const [flowOpen, setFlowOpen] = useState<Record<number, boolean>>({})

  const regenControlsFor = (runId: number): RegenerateControls => ({
    activeEntryId: regen.activeEntryId,
    busy: regen.activeEntryId != null,
    reasoning: regen.reasoning,
    tokens: regen.tokens,
    flowOpen,
    onRegenerate: (entry) => {
      setFlowOpen((p) => ({ ...p, [entry.id]: false }))
      void regen.regenerate(runId, entry.id)
    },
    onStop: regen.stop,
    onToggleFlow: (id) => setFlowOpen((p) => ({ ...p, [id]: !p[id] })),
  })

  // Schedule prefs (display; the backend scheduler runs them)
  const [prefs, setPrefs] = useState<Prefs | undefined>(undefined)
  const schedule = prefs?.briefing_schedule === 'weekly' ? 'weekly' : 'daily'
  const briefingTime = prefs?.briefing_time ?? '09:00'
  // Same field semantics as Settings: serialized writes, revert + toast on failure
  const briefingDay = useSettingField<string>({
    initial: 'monday',
    serverValue: prefs ? (prefs.briefing_day ?? 'monday') : undefined,
    set: setBriefingDay,
    echo: (r) => (r as { day?: string } | undefined)?.day,
    label: 'briefing day',
  })

  // Prompt editor
  const [showPromptEditor, setShowPromptEditor] = useState(false)
  const [trackedPromptDraft, setTrackedPromptDraft] = useState('')
  const [briefingPromptDraft, setBriefingPromptDraft] = useState('')
  const [trackedPromptSaved, setTrackedPromptSaved] = useState('')
  const [briefingPromptSaved, setBriefingPromptSaved] = useState('')
  const [trackedPromptDefault, setTrackedPromptDefault] = useState('')
  const [briefingPromptDefault, setBriefingPromptDefault] = useState('')
  const [savingPrompt, setSavingPrompt] = useState(false)
  const promptDirty = trackedPromptDraft !== trackedPromptSaved || briefingPromptDraft !== briefingPromptSaved

  // Sources (briefing chats)
  const [showSources, setShowSources] = useState(false)
  const [briefingChats, setBriefingChats] = useState<Conversation[]>([])
  const [sourceSearch, setSourceSearch] = useState('')

  useEffect(() => {
    getBriefingConversations().then(setBriefingChats).catch((e) => reportError(e, 'Could not load briefing sources'))
    getPrefs().then(setPrefs).catch((e) => reportError(e, 'Could not load briefing schedule'))
    getBriefingPrompts().then(({ tracked, briefing }) => {
      setTrackedPromptDefault(tracked.default)
      setBriefingPromptDefault(briefing.default)
      setTrackedPromptSaved((tracked.custom ?? '').trim())
      setBriefingPromptSaved((briefing.custom ?? '').trim())
      setTrackedPromptDraft((tracked.custom ?? '').trim())
      setBriefingPromptDraft((briefing.custom ?? '').trim())
    }).catch((e) => reportError(e, 'Could not load briefing prompts'))
  }, [])

  const handleRemoveSource = async (chat: Conversation) => {
    try {
      await setBriefing(chat.id, false)
      setBriefingChats((prev) => prev.filter((c) => c.id !== chat.id))
    } catch (e) {
      reportError(e, 'Could not remove briefing source')
    }
  }

  const savePrompts = async (tracked: string, briefing: string) => {
    setSavingPrompt(true)
    try {
      await setBriefingPrompts(tracked, briefing)
      setTrackedPromptSaved(tracked)
      setBriefingPromptSaved(briefing)
      setTrackedPromptDraft(tracked)
      setBriefingPromptDraft(briefing)
      toast.success(tracked || briefing ? 'Briefing prompts saved' : 'Briefing prompts reset to default')
    } catch (e) {
      reportError(e, 'Could not save briefing prompts')
    } finally {
      setSavingPrompt(false)
    }
  }

  const handleDeleteRun = async (r: BriefingRun) => {
    await confirm({
      title: 'Delete this briefing?',
      message: `${fmtDateTime(r.generated_at)} · ${r.entries.length} summar${r.entries.length === 1 ? 'y' : 'ies'}`,
      destructive: true,
      confirmLabel: 'Delete',
      onConfirm: async () => {
        await deleteBriefingRun(r.id)
        setHistory((prev) => prev?.filter((x) => x.id !== r.id))
        setExpandedRunId((cur) => (cur === r.id ? null : cur))
      },
    })
  }

  const filteredSources = briefingChats.filter((c) => conversationTitle(c).toLowerCase().includes(sourceSearch.toLowerCase()))
  // The run being generated is shown live above; hide its (partial) history row
  const visibleHistory = run.running && run.runId != null ? history.filter((r) => r.id !== run.runId) : history
  const liveDays = run.days ?? selectedDays
  const regenBusy = regen.activeEntryId != null

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      {/* Header bar */}
      <div className="flex shrink-0 items-center justify-between border-b border-slate-700/50 bg-slate-800/40 px-4 py-2.5">
        <div className="flex items-center gap-2 text-xs text-slate-400">
          <span className="font-medium text-slate-300 capitalize">{schedule}</span>
          {schedule === 'weekly' && (
            <>
              <span className="text-slate-600">·</span>
              <div className="flex items-center gap-0.5">
                {DAY_OPTIONS.map((d) => (
                  <button
                    key={d.value}
                    onClick={() => briefingDay.setValue(d.value, { immediate: true })}
                    className={`rounded px-1.5 py-0.5 text-[10px] font-medium transition-colors ${
                      briefingDay.value === d.value ? 'bg-slate-600/60 text-slate-200' : 'text-slate-500 hover:text-slate-300'
                    }`}
                  >
                    {d.label}
                  </button>
                ))}
              </div>
            </>
          )}
          <span className="text-slate-600">·</span>
          <span>{fmtScheduleTime(briefingTime)}</span>
          {lastUpdated && (
            <span className="text-slate-500">
              · Updated {lastUpdated.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1">
            {TIMEFRAME_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                onClick={() => setSelectedDays(opt.value)}
                disabled={run.running}
                className={`rounded-full px-2 py-0.5 text-[10px] font-medium transition-colors disabled:opacity-40 ${
                  selectedDays === opt.value
                    ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/40'
                    : 'text-slate-500 hover:text-slate-300 border border-transparent hover:border-slate-600/50'
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
          {run.running ? (
            <button
              onClick={() => void job.cancel()}
              disabled={job.cancelling}
              className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium text-rose-400 hover:bg-rose-900/30 hover:text-rose-300 transition-colors border border-rose-600/30 disabled:opacity-50"
            >
              {job.cancelling ? <Loader2 size={12} className="animate-spin" /> : <X size={12} />}
              {job.cancelling ? 'Stopping…' : 'Stop'}
            </button>
          ) : (
            <button
              onClick={() => { job.clearError(); job.start(selectedDays) }}
              className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium text-emerald-400 hover:bg-emerald-900/30 hover:text-emerald-300 transition-colors border border-emerald-600/30"
            >
              <RotateCcw size={12} />
              Generate Now
            </button>
          )}
        </div>
      </div>

      {/* Settings drawers: prompts + sources */}
      <div className="shrink-0 border-b border-slate-700/40 bg-slate-900/20 px-4 py-2">
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowPromptEditor((v) => !v)}
            className="inline-flex items-center gap-1 rounded border border-slate-600/60 px-2 py-0.5 text-[10px] text-slate-300 hover:bg-slate-700/30"
          >
            {showPromptEditor ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
            Briefing Prompts
          </button>
          <button
            onClick={() => setShowSources((v) => !v)}
            className="inline-flex items-center gap-1 rounded border border-slate-600/60 px-2 py-0.5 text-[10px] text-slate-300 hover:bg-slate-700/30"
          >
            {showSources ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
            Sources
            <span className="ml-0.5 text-slate-500">({briefingChats.length})</span>
          </button>
        </div>

        {showSources && (
          <div className="mt-2 rounded border border-slate-700/60 bg-slate-900/50 p-2">
            <input
              type="text"
              placeholder="Filter chats…"
              value={sourceSearch}
              onChange={(e) => setSourceSearch(e.target.value)}
              className="mb-2 w-full rounded border border-slate-600 bg-slate-800 px-2 py-1 text-[11px] text-gray-200 placeholder:text-slate-500 outline-none focus:border-emerald-500/60"
            />
            <div className="max-h-40 overflow-y-auto space-y-0.5">
              {filteredSources.length === 0 ? (
                <p className="py-3 text-center text-[11px] text-slate-500">
                  {briefingChats.length === 0 ? 'No briefing chats. Mark chats for briefing from the Chats page.' : 'No matches.'}
                </p>
              ) : (
                filteredSources.map((chat) => (
                  <div key={chat.id} className="group flex items-center gap-2 rounded px-2 py-1 hover:bg-slate-700/40">
                    <div className="flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full bg-emerald-500/20 text-[9px] font-medium text-emerald-400">
                      {conversationTitle(chat).charAt(0).toUpperCase()}
                    </div>
                    <span className="flex-1 truncate text-[11px] text-slate-200" title={conversationTitle(chat)}>{conversationTitle(chat)}</span>
                    <ChannelIcon channel={chat.channel} />
                    <span className="text-[9px] text-slate-500">{kindLabel(chat.kind)}</span>
                    <button
                      onClick={() => void handleRemoveSource(chat)}
                      title="Remove from briefing"
                      className="hidden group-hover:flex rounded p-0.5 text-slate-500 hover:text-rose-400 transition-colors"
                    >
                      <X size={11} />
                    </button>
                  </div>
                ))
              )}
            </div>
          </div>
        )}

        {showPromptEditor && (
          <div className="mt-2 rounded border border-slate-700/60 bg-slate-900/50 p-2">
            <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-violet-300">
              Tracked Chats Prompt {trackedPromptSaved ? '(Custom)' : '(Default)'}
            </p>
            <textarea
              value={trackedPromptDraft || trackedPromptDefault}
              onChange={(e) => setTrackedPromptDraft(e.target.value)}
              placeholder="Leave empty to use built-in default prompt."
              className="h-28 w-full resize-y rounded border border-violet-700/60 bg-slate-950/70 px-2 py-1.5 text-[11px] leading-relaxed text-slate-200 outline-none focus:border-violet-500/60"
            />
            <p className="mb-1 mt-3 text-[10px] font-semibold uppercase tracking-wide text-emerald-300">
              Briefing Chats Prompt {briefingPromptSaved ? '(Custom)' : '(Default)'}
            </p>
            <textarea
              value={briefingPromptDraft || briefingPromptDefault}
              onChange={(e) => setBriefingPromptDraft(e.target.value)}
              placeholder="Leave empty to use built-in default prompt."
              className="h-32 w-full resize-y rounded border border-slate-700 bg-slate-950/70 px-2 py-1.5 text-[11px] leading-relaxed text-slate-200 outline-none focus:border-emerald-500/60"
            />
            <div className="mt-2 flex items-center gap-2">
              <button
                onClick={() => void savePrompts(trackedPromptDraft.trim(), briefingPromptDraft.trim())}
                disabled={savingPrompt || !promptDirty}
                className="rounded border border-emerald-600/40 px-2 py-1 text-[10px] text-emerald-300 hover:bg-emerald-900/30 disabled:opacity-50"
              >
                {savingPrompt ? 'Saving…' : 'Save Prompt'}
              </button>
              <button
                onClick={() => void savePrompts('', '')}
                disabled={savingPrompt || (!trackedPromptSaved && !briefingPromptSaved)}
                className="rounded border border-slate-600/50 px-2 py-1 text-[10px] text-slate-300 hover:bg-slate-700/30 disabled:opacity-50"
              >
                Reset to Default
              </button>
              {promptDirty && <span className="text-[10px] text-amber-300">Unsaved changes</span>}
            </div>
          </div>
        )}
      </div>

      {/* Progress */}
      {run.running && (
        <div className="shrink-0 px-4 py-2 bg-slate-800/60 border-b border-slate-700/50">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-xs text-slate-400">
              {!run.progress ? (
                run.attached ? 'Joining the briefing in progress…' : 'Starting briefing…'
              ) : run.progress.stage === 'syncing' ? (
                'Syncing briefing chats…'
              ) : run.progress.chatName ? (
                <>
                  Summarising <span className="text-slate-200 font-medium">{run.progress.chatName}</span>…
                  {run.progress.tokens != null && run.progress.tokens > 0 && (
                    <span className="ml-1.5 text-amber-400/80">({run.progress.tokens} tokens)</span>
                  )}
                </>
              ) : (
                'Preparing…'
              )}
            </span>
            {run.progress && run.progress.stage !== 'syncing' && (
              <span className="text-xs text-slate-500">{run.progress.current}/{run.progress.total}</span>
            )}
          </div>
          <div className="h-1 w-full rounded-full bg-slate-700">
            <div
              className="h-1 rounded-full bg-emerald-500 transition-all duration-300"
              style={{ width: run.progress && run.progress.total > 0 ? `${(run.progress.current / run.progress.total) * 100}%` : '0%' }}
            />
          </div>

          <div className="mt-2">
            <button
              onClick={() => setShowFlow((v) => !v)}
              className="inline-flex items-center gap-1 rounded border border-slate-600/60 px-2 py-0.5 text-[10px] text-slate-300 hover:bg-slate-700/40"
            >
              {showFlow ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
              {showFlow ? 'Hide' : 'Show'} LLM Flow
            </button>
          </div>

          {showFlow && (
            <div className="mt-2 max-h-64 overflow-y-auto rounded border border-slate-700/60 bg-slate-900/60 p-2 space-y-2">
              {run.flowOrder.length === 0 ? (
                <p className="text-[11px] text-slate-500">Waiting for reasoning stream…</p>
              ) : (
                run.flowOrder.map((chatName) => (
                  <div key={chatName} className="rounded border border-slate-700/50 bg-slate-800/50 p-2">
                    <p className="mb-1 text-[10px] font-medium uppercase tracking-wide text-slate-300">{chatName}</p>
                    <pre className="whitespace-pre-wrap break-words text-[11px] leading-relaxed text-slate-400">
                      {run.flowByChat[chatName] || '(no reasoning chunks yet)'}
                    </pre>
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      )}

      {run.conflict && !run.running && (
        <div role="alert" className="shrink-0 mx-4 mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-amber-700/40 bg-amber-900/15 px-3 py-2 text-xs text-amber-300">
          <AlertCircle size={13} className="shrink-0" />
          <span className="flex-1 min-w-48">
            {run.conflict.trigger === 'scheduled' ? 'A scheduled ' + describeBriefingWindow(run.conflict.runningDays).slice(2) : describeBriefingWindow(run.conflict.runningDays)} is already running.
            {' '}Only one briefing runs at a time.
          </span>
          <button
            onClick={job.joinRunning}
            className="rounded border border-amber-600/40 px-2 py-0.5 text-[11px] hover:bg-amber-900/30"
          >
            View
          </button>
          <button
            onClick={() => job.cancelAndRun(run.conflict?.requestedDays ?? selectedDays)}
            className="rounded border border-rose-600/40 px-2 py-0.5 text-[11px] text-rose-300 hover:bg-rose-900/30"
          >
            Cancel &amp; run {windowLabel(run.conflict.requestedDays ?? selectedDays)}
          </button>
          <button onClick={job.dismissConflict} className="text-amber-400/70 hover:text-amber-300" title="Dismiss">
            <X size={12} />
          </button>
        </div>
      )}

      {run.error && (
        <div className="shrink-0 mx-4 mt-3 flex items-center gap-2 rounded-lg border border-rose-700/40 bg-rose-900/20 px-3 py-2 text-xs text-rose-400">
          <AlertCircle size={13} className="shrink-0" />
          <span className="flex-1">{run.error}</span>
          <button onClick={job.clearError} className="text-rose-400/70 hover:text-rose-300" title="Dismiss">
            <X size={12} />
          </button>
        </div>
      )}

      {run.failed.length > 0 && (
        <div className="shrink-0 mx-4 mt-3 rounded-lg border border-amber-700/40 bg-amber-900/10 px-3 py-2 text-xs text-amber-300">
          <p className="font-medium">Skipped {run.failed.length} chat{run.failed.length === 1 ? '' : 's'}:</p>
          <ul className="mt-1 space-y-0.5 text-amber-300/80">
            {run.failed.map((f) => (
              <li key={`${f.conversation_id}-${f.conversation_title}`}>{f.conversation_title}: {f.message}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
        {/* Live results */}
        {run.running && run.live.length > 0 && (
          <section>
            <div className="mb-3 flex items-center gap-2">
              <Loader2 size={13} className="text-emerald-400 animate-spin" />
              <h3 className="text-xs font-semibold text-emerald-400">Generating…</h3>
              <span className="rounded-full bg-emerald-500/10 border border-emerald-500/30 px-1.5 py-0.5 text-[10px] text-emerald-500">
                Last {windowLabel(liveDays)}
              </span>
            </div>
            <BriefingEntryList entries={run.live} />
          </section>
        )}

        {historyQuery.loading && (
          <div className="flex justify-center py-10 text-slate-500"><Loader2 size={16} className="animate-spin" /></div>
        )}
        {!historyQuery.loading && historyQuery.error != null && (
          <div className="flex items-center gap-2 rounded-lg border border-rose-700/40 bg-rose-900/20 px-3 py-2 text-xs text-rose-400">
            <AlertCircle size={13} />
            Could not load briefing history.
            <button onClick={() => void refetchHistory()} className="ml-auto underline">Retry</button>
          </div>
        )}
        {!historyQuery.loading && historyQuery.error == null && visibleHistory.length === 0 && !run.running && (
          <div className="flex flex-1 flex-col items-center justify-center gap-3 text-slate-500 py-16">
            <Newspaper size={32} className="opacity-30" />
            <p className="text-sm">No briefings yet. Select a timeframe and click "Generate Now".</p>
          </div>
        )}

        {visibleHistory.map((r) => {
          const isExpanded = expandedRunId === r.id
          const status = r.status ?? 'done'
          const isRunning = status === 'running'
          return (
            <div key={r.id} className="rounded-xl border border-slate-700/50 bg-slate-800/30 overflow-hidden">
              <div
                className="flex items-center justify-between px-4 py-3 cursor-pointer hover:bg-slate-700/20 transition-colors"
                onClick={() => setExpandedRunId(isExpanded ? null : r.id)}
              >
                <div className="flex items-center gap-2">
                  {isExpanded ? <ChevronDown size={14} className="text-slate-400" /> : <ChevronRight size={14} className="text-slate-400" />}
                  <span className="text-xs font-medium text-slate-200">{fmtDateTime(r.generated_at)}</span>
                  <span className="rounded-full bg-slate-700/60 px-1.5 py-0.5 text-[10px] text-slate-400">
                    Last {windowLabel(r.days)} · {r.entries.length} chat{r.entries.length !== 1 ? 's' : ''}
                  </span>
                  {status !== 'done' && (
                    <span className={`rounded-full px-1.5 py-0.5 text-[10px] ${RUN_STATUS_BADGE[status] ?? ''}`} title={r.error ?? undefined}>
                      {isRunning ? 'generating' : status}
                    </span>
                  )}
                </div>
                <button
                  onClick={(e) => { e.stopPropagation(); void handleDeleteRun(r) }}
                  disabled={isRunning}
                  className="rounded-md p-1.5 text-slate-500 hover:bg-rose-900/30 hover:text-rose-400 transition-colors disabled:opacity-30 disabled:hover:bg-transparent"
                  title={isRunning ? 'This briefing is still being generated' : 'Delete this briefing'}
                >
                  <Trash2 size={13} />
                </button>
              </div>

              {isExpanded && (
                <div className="px-4 pb-4 pt-1 border-t border-slate-700/40">
                  {r.error && <p className="mb-2 text-[11px] text-rose-400/80">{r.error}</p>}
                  <BriefingEntryList entries={r.entries} regen={isRunning ? undefined : { ...regenControlsFor(r.id), busy: regenBusy }} />
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
