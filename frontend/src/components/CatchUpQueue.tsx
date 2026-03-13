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
} from 'lucide-react'
import type { DashboardNeedsReply, CatchUpItem, RelationshipStatus } from '../api/types'
import {
  getAllCatchUpItems,
  snoozeCatchUpItem,
  markCatchUpHandled,
  addCatchUpNote,
  updateCatchUpRelationship,
} from '../api/client'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtRelative(iso: string): string {
  const d = new Date(iso)
  const now = new Date()
  const diffMs = now.getTime() - d.getTime()
  const diffMins = Math.floor(diffMs / 60000)
  if (diffMins < 1) return 'Just now'
  if (diffMins < 60) return `${diffMins}m ago`
  const diffHours = Math.floor(diffMins / 60)
  if (diffHours < 24) return `${diffHours}h ago`
  const diffDays = Math.floor(diffHours / 24)
  if (diffDays < 7) return `${diffDays}d ago`
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

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

// ---------------------------------------------------------------------------
// Single triage card
// ---------------------------------------------------------------------------

interface TriageCardProps {
  item: DashboardNeedsReply
  catchUpMeta: CatchUpItem | null
  selected: boolean
  onSnooze: (chatId: number, hours: number) => void
  onHandled: (chatId: number) => void
  onNote: (chatId: number, note: string) => void
  onRelationship: (chatId: number, status: string) => void
  onReply: (chatId: number) => void
}

function TriageCard({
  item,
  catchUpMeta,
  selected,
  onSnooze,
  onHandled,
  onNote,
  onRelationship,
  onReply,
}: TriageCardProps) {
  const [expanded, setExpanded] = useState(false)
  const [showSnooze, setShowSnooze] = useState(false)
  const [showRelStatus, setShowRelStatus] = useState(false)
  const [noteText, setNoteText] = useState(catchUpMeta?.note || '')
  const [showNote, setShowNote] = useState(false)

  const chatId = item.chat.telegram_id

  return (
    <div
      className={`rounded-xl border overflow-hidden transition-all ${
        selected
          ? 'border-indigo-500/50 ring-1 ring-indigo-500/30 bg-slate-800/80'
          : 'border-slate-700/60 bg-slate-800/60 hover:border-slate-600/60'
      }`}
    >
      {/* Header row */}
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
        {expanded ? (
          <ChevronDown size={14} className="shrink-0 text-slate-500" />
        ) : (
          <ChevronRight size={14} className="shrink-0 text-slate-500" />
        )}
        <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-slate-700 text-sm font-medium text-slate-300">
          {item.chat.name.charAt(0).toUpperCase()}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center justify-between gap-2">
            <p className="truncate text-sm font-medium text-gray-200">{item.chat.name}</p>
            <span className="flex-shrink-0 text-[10px] text-slate-500">
              {fmtRelative(item.last_message.date)}
            </span>
          </div>
          <p className="truncate text-xs text-slate-400 mt-0.5">
            {item.last_message.sender_name && (
              <span className="text-slate-300">{item.last_message.sender_name}: </span>
            )}
            {item.last_message.text || <span className="italic">[media]</span>}
          </p>
        </div>
        {catchUpMeta?.note && (
          <StickyNote size={12} className="shrink-0 text-amber-400/60" title="Has note" />
        )}
      </div>

      {/* Expanded actions panel */}
      {expanded && (
        <div className="border-t border-slate-700/50 px-4 py-3 space-y-3">
          {/* Context summary */}
          <div className="rounded-lg bg-slate-700/30 px-3 py-2">
            <p className="text-[11px] text-slate-500 uppercase tracking-wider mb-1">Why this is here</p>
            <p className="text-xs text-slate-400">
              {item.chat.type === 'private'
                ? 'This person sent you a message and is waiting for your reply.'
                : 'Someone in this chat sent a message that needs your attention.'}
              {item.last_message.sender_name && (
                <span>
                  {' '}Last message from <span className="text-slate-300 font-medium">{item.last_message.sender_name}</span>{' '}
                  {fmtRelative(item.last_message.date)}.
                </span>
              )}
            </p>
          </div>

          {/* Primary actions row */}
          <div className="flex flex-wrap gap-1.5">
            <button
              onClick={(e) => {
                e.stopPropagation()
                onReply(chatId)
              }}
              className="flex items-center gap-1.5 rounded-lg bg-indigo-500/20 hover:bg-indigo-500/30 px-3 py-1.5 text-xs font-medium text-indigo-300 transition-colors"
            >
              <MessageSquare size={12} />
              Reply now
            </button>
            <button
              onClick={(e) => {
                e.stopPropagation()
                setShowSnooze((v) => !v)
                setShowRelStatus(false)
                setShowNote(false)
              }}
              className="flex items-center gap-1.5 rounded-lg bg-amber-500/15 hover:bg-amber-500/25 px-3 py-1.5 text-xs font-medium text-amber-300 transition-colors"
            >
              <AlarmClock size={12} />
              Remind later
            </button>
            <button
              onClick={(e) => {
                e.stopPropagation()
                onHandled(chatId)
              }}
              className="flex items-center gap-1.5 rounded-lg bg-emerald-500/15 hover:bg-emerald-500/25 px-3 py-1.5 text-xs font-medium text-emerald-300 transition-colors"
            >
              <CheckCircle2 size={12} />
              Mark handled
            </button>
            <button
              onClick={(e) => {
                e.stopPropagation()
                setShowNote((v) => !v)
                setShowSnooze(false)
                setShowRelStatus(false)
              }}
              className="flex items-center gap-1.5 rounded-lg bg-slate-600/50 hover:bg-slate-600/70 px-3 py-1.5 text-xs font-medium text-slate-300 transition-colors"
            >
              <StickyNote size={12} />
              Add note
            </button>
            {item.chat.type === 'private' && (
              <button
                onClick={(e) => {
                  e.stopPropagation()
                  setShowRelStatus((v) => !v)
                  setShowSnooze(false)
                  setShowNote(false)
                }}
                className="flex items-center gap-1.5 rounded-lg bg-violet-500/15 hover:bg-violet-500/25 px-3 py-1.5 text-xs font-medium text-violet-300 transition-colors"
              >
                <Users size={12} />
                Relationship
              </button>
            )}
          </div>

          {/* Snooze picker */}
          {showSnooze && (
            <div className="flex flex-wrap gap-1.5 rounded-lg bg-slate-700/30 p-2">
              <p className="w-full text-[10px] text-slate-500 uppercase tracking-wider mb-1">Remind me in</p>
              {SNOOZE_OPTIONS.map((opt) => (
                <button
                  key={opt.hours}
                  onClick={(e) => {
                    e.stopPropagation()
                    onSnooze(chatId, opt.hours)
                    setShowSnooze(false)
                  }}
                  className="rounded-md bg-slate-600/50 hover:bg-amber-500/20 hover:text-amber-300 px-2.5 py-1 text-[11px] text-slate-300 transition-colors"
                >
                  {opt.label}
                </button>
              ))}
            </div>
          )}

          {/* Relationship status picker */}
          {showRelStatus && (
            <div className="flex flex-wrap gap-1.5 rounded-lg bg-slate-700/30 p-2">
              <p className="w-full text-[10px] text-slate-500 uppercase tracking-wider mb-1">Set relationship status</p>
              {RELATIONSHIP_STATUSES.map((s) => (
                <button
                  key={s.value}
                  onClick={(e) => {
                    e.stopPropagation()
                    onRelationship(chatId, s.value)
                    setShowRelStatus(false)
                  }}
                  className={`rounded-md px-2.5 py-1 text-[11px] font-medium transition-colors ${s.color} hover:opacity-80`}
                >
                  {s.label}
                </button>
              ))}
            </div>
          )}

          {/* Note editor */}
          {showNote && (
            <div className="rounded-lg bg-slate-700/30 p-2 space-y-2">
              <p className="text-[10px] text-slate-500 uppercase tracking-wider">Note</p>
              <textarea
                value={noteText}
                onChange={(e) => setNoteText(e.target.value)}
                placeholder="Add a note about this conversation..."
                className="w-full rounded-md bg-slate-800 border border-slate-600 px-3 py-2 text-xs text-slate-200 placeholder:text-slate-500 resize-none focus:outline-none focus:border-indigo-500/50"
                rows={2}
                onClick={(e) => e.stopPropagation()}
              />
              <div className="flex justify-end gap-1.5">
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    setShowNote(false)
                  }}
                  className="rounded-md px-2.5 py-1 text-[11px] text-slate-400 hover:text-slate-200 transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    onNote(chatId, noteText)
                    setNoteText('')
                    setShowNote(false)
                  }}
                  className="rounded-md bg-indigo-500/20 hover:bg-indigo-500/30 px-2.5 py-1 text-[11px] font-medium text-indigo-300 transition-colors"
                >
                  Save note
                </button>
              </div>
            </div>
          )}

          {/* Existing note display */}
          {catchUpMeta?.note && !showNote && (
            <div className="rounded-lg bg-amber-500/5 border border-amber-500/20 px-3 py-2">
              <p className="text-[10px] text-amber-400/60 uppercase tracking-wider mb-0.5">Note</p>
              <p className="text-xs text-slate-300">{catchUpMeta.note}</p>
            </div>
          )}

          {/* Jump to chat link */}
          <button
            onClick={(e) => {
              e.stopPropagation()
              onReply(chatId)
            }}
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
// Main CatchUpQueue component
// ---------------------------------------------------------------------------

interface CatchUpQueueProps {
  needsReply: DashboardNeedsReply[]
  onRefreshDashboard: () => void
  onBadgeCount?: (count: number) => void
}

export default function CatchUpQueue({ needsReply, onRefreshDashboard, onBadgeCount }: CatchUpQueueProps) {
  const navigate = useNavigate()
  const [catchUpMeta, setCatchUpMeta] = useState<Record<number, CatchUpItem>>({})
  const [loading, setLoading] = useState(false)
  const [selectedIdx, setSelectedIdx] = useState(0)
  const [actionFeedback, setActionFeedback] = useState<{ chatId: number; msg: string } | null>(null)
  const selectedRef = useRef<HTMLDivElement | null>(null)

  // Load catch-up metadata
  const fetchMeta = useCallback(async () => {
    setLoading(true)
    try {
      const items = await getAllCatchUpItems()
      const map: Record<number, CatchUpItem> = {}
      for (const it of items) {
        map[it.chat_telegram_id] = it
      }
      setCatchUpMeta(map)
    } catch {
      // ignore
    }
    setLoading(false)
  }, [])

  useEffect(() => {
    fetchMeta()
  }, [fetchMeta])

  // Filter out items that are snoozed or handled
  const visibleItems = useMemo(() => {
    return needsReply.filter((item) => {
      const meta = catchUpMeta[item.chat.telegram_id]
      if (!meta) return true // no triage record yet = show it
      if (meta.status === 'handled') return false
      if (meta.status === 'snoozed' && meta.snoozed_until) {
        return new Date(meta.snoozed_until) <= new Date()
      }
      return true
    })
  }, [needsReply, catchUpMeta])

  // Notify parent of badge count changes
  useEffect(() => {
    onBadgeCount?.(visibleItems.length)
  }, [visibleItems.length, onBadgeCount])

  // Keep selection in bounds
  useEffect(() => {
    if (visibleItems.length > 0) {
      setSelectedIdx((i) => Math.min(i, visibleItems.length - 1))
    }
  }, [visibleItems.length])

  // Scroll selected into view
  useEffect(() => {
    selectedRef.current?.scrollIntoView({ block: 'nearest' })
  }, [selectedIdx])

  const showFeedback = (chatId: number, msg: string) => {
    setActionFeedback({ chatId, msg })
    setTimeout(() => setActionFeedback(null), 2000)
  }

  const handleSnooze = async (chatId: number, hours: number) => {
    try {
      const updated = await snoozeCatchUpItem(chatId, hours)
      setCatchUpMeta((prev) => ({ ...prev, [updated.chat_telegram_id]: updated }))
      showFeedback(chatId, `Snoozed for ${hours >= 24 ? `${hours / 24}d` : `${hours}h`}`)
    } catch (err) {
      console.error('Failed to snooze:', err)
    }
  }

  const handleHandled = async (chatId: number) => {
    try {
      const updated = await markCatchUpHandled(chatId)
      setCatchUpMeta((prev) => ({ ...prev, [updated.chat_telegram_id]: updated }))
      showFeedback(chatId, 'Marked as handled')
    } catch (err) {
      console.error('Failed to mark handled:', err)
    }
  }

  const handleNote = async (chatId: number, note: string) => {
    try {
      const updated = await addCatchUpNote(chatId, note)
      setCatchUpMeta((prev) => ({ ...prev, [updated.chat_telegram_id]: updated }))
      showFeedback(chatId, 'Note saved')
    } catch (err) {
      console.error('Failed to add note:', err)
    }
  }

  const handleRelationship = async (chatId: number, status: string) => {
    try {
      await updateCatchUpRelationship(chatId, status)
      showFeedback(chatId, `Status: ${status}`)
      onRefreshDashboard()
    } catch (err) {
      console.error('Failed to update relationship:', err)
    }
  }

  const handleReply = (chatId: number) => {
    navigate(`/chats?chatId=${chatId}`)
  }

  // Keyboard shortcuts
  const shortcuts = useMemo(
    () => ({
      j: () => setSelectedIdx((i) => Math.min(i + 1, visibleItems.length - 1)),
      arrowdown: () => setSelectedIdx((i) => Math.min(i + 1, visibleItems.length - 1)),
      k: () => setSelectedIdx((i) => Math.max(i - 1, 0)),
      arrowup: () => setSelectedIdx((i) => Math.max(i - 1, 0)),
      r: () => {
        const curItem = visibleItems[selectedIdx]
        if (curItem) handleReply(curItem.chat.telegram_id)
      },
      h: () => {
        const curItem = visibleItems[selectedIdx]
        if (curItem) handleHandled(curItem.chat.telegram_id)
      },
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [visibleItems, selectedIdx, navigate],
  )

  useKeyboardShortcuts(shortcuts)

  if (visibleItems.length === 0 && !loading) {
    return (
      <div className="rounded-xl bg-slate-800 border border-slate-700/50 px-4 py-8 text-center">
        <CheckCircle2 size={24} className="mx-auto mb-2 text-emerald-400/50" />
        <p className="text-sm text-slate-400 font-medium">All caught up!</p>
        <p className="text-xs text-slate-500 mt-1">No conversations waiting for your attention.</p>
      </div>
    )
  }

  return (
    <div className="space-y-3">
      {/* Queue header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-xs text-slate-500">
            {visibleItems.length} item{visibleItems.length !== 1 ? 's' : ''} to triage
          </span>
          <span className="text-[10px] text-slate-600">|</span>
          <span className="text-[10px] text-slate-600">
            j/k to navigate, r to reply, h to handle
          </span>
        </div>
        <button
          onClick={() => {
            fetchMeta()
            onRefreshDashboard()
          }}
          disabled={loading}
          className="text-slate-500 hover:text-slate-300 disabled:opacity-50 transition-colors"
          title="Refresh"
        >
          <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
        </button>
      </div>

      {/* Triage cards */}
      <div className="space-y-2">
        {visibleItems.map((item, i) => (
          <div
            key={item.chat.telegram_id}
            ref={i === selectedIdx ? selectedRef : null}
            className="relative"
          >
            <TriageCard
              item={item}
              catchUpMeta={catchUpMeta[item.chat.telegram_id] || null}
              selected={i === selectedIdx}
              onSnooze={handleSnooze}
              onHandled={handleHandled}
              onNote={handleNote}
              onRelationship={handleRelationship}
              onReply={handleReply}
            />
            {/* Action feedback toast */}
            {actionFeedback?.chatId === item.chat.telegram_id && (
              <div className="absolute top-2 right-2 rounded-md bg-emerald-500/20 border border-emerald-500/30 px-2.5 py-1 text-[11px] font-medium text-emerald-400 animate-pulse z-10">
                {actionFeedback.msg}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
