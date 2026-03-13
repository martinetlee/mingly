import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Mail, ArrowRight, FileText, AtSign, RefreshCw, ChevronDown, ChevronRight, Bookmark, CheckCheck, ListChecks } from 'lucide-react'
import type { DashboardData, MentionGroup, MentionMessage } from '../api/types'
import { getDashboard, getMentions, reactToMessage } from '../api/client'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import CatchUpQueue from '../components/CatchUpQueue'

function getGreeting(): string {
  const hour = new Date().getHours()
  if (hour < 12) return 'Good morning'
  if (hour < 17) return 'Good afternoon'
  return 'Good evening'
}

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

// ---------------------------------------------------------------------------
// Mention components (simplified from Intelligence page)
// ---------------------------------------------------------------------------

function MentionMsgRow({ msg, highlighted }: { msg: MentionMessage; highlighted: boolean }) {
  return (
    <div className={`rounded-lg px-3 py-2 text-xs leading-relaxed transition-colors ${
      highlighted ? 'bg-amber-500/10 border border-amber-500/30' : 'bg-slate-700/30'
    }`}>
      <div className="flex items-baseline justify-between gap-2 mb-0.5">
        <span className={`font-medium truncate ${highlighted ? 'text-amber-300' : 'text-slate-300'}`}>
          {msg.sender_name || 'Unknown'}
        </span>
        <span className="shrink-0 text-[10px] text-slate-500">{fmtRelative(msg.date)}</span>
      </div>
      {msg.text && <p className="text-slate-400 leading-snug line-clamp-3">{msg.text}</p>}
      {msg.media_type && !msg.text && (
        <span className="italic text-slate-500">[{msg.media_type}]</span>
      )}
      {!msg.text && !msg.media_type && (
        <span className="italic text-slate-500">[no text]</span>
      )}
    </div>
  )
}

function HomeMentionCard({
  group,
  onNavigate,
  selected,
}: {
  group: MentionGroup
  onNavigate: (chatId: number, msgId: number) => void
  selected: boolean
}) {
  const [expanded, setExpanded] = useState(false)

  const chatTypeColor = {
    group: 'bg-purple-500/20 text-purple-400',
    channel: 'bg-amber-500/20 text-amber-400',
    private: 'bg-indigo-500/20 text-indigo-400',
  }[group.chat.type] ?? 'bg-slate-500/20 text-slate-400'

  const allMsgs = [
    ...group.context_before.map((m) => ({ msg: m, highlighted: false })),
    { msg: group.mention_msg, highlighted: true },
    ...group.context_after.map((m) => ({ msg: m, highlighted: false })),
  ]

  const trackedBorder = group.chat.is_tracked
    ? 'border-violet-500/40 bg-violet-900/10'
    : 'border-slate-700/60 bg-slate-800/60'

  const mentionText = group.mention_msg.text || ''
  const senderName = group.mention_msg.sender_name || 'Unknown'

  return (
    <div className={`rounded-xl border ${trackedBorder} overflow-hidden transition-colors${selected ? ' ring-1 ring-amber-500/50' : ''}`}>
      <div
        role="button"
        tabIndex={0}
        onClick={() => setExpanded((v) => !v)}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpanded((v) => !v) } }}
        className="flex items-center gap-2 px-3 py-2.5 cursor-pointer hover:bg-slate-700/30 transition-colors select-none"
      >
        {expanded ? <ChevronDown size={12} className="shrink-0 text-slate-500" /> : <ChevronRight size={12} className="shrink-0 text-slate-500" />}
        {group.chat.is_tracked && <Bookmark size={11} className="shrink-0 text-violet-400" />}
        <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium shrink-0 ${chatTypeColor}`}>
          {group.chat.type}
        </span>
        <span className="flex-1 min-w-0 truncate text-xs font-semibold text-slate-200">{group.chat.name}</span>
        {group.replied && (
          <span className="flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] font-medium bg-emerald-500/15 text-emerald-400 shrink-0">
            <CheckCheck size={9} />
            replied
          </span>
        )}
        {group.mention_msg.my_reaction && !group.replied && (
          <span className="flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] font-medium bg-amber-500/15 text-amber-400 shrink-0">
            <span className="text-[10px] leading-none">{group.mention_msg.my_reaction}</span>
            ack
          </span>
        )}
        <AtSign size={11} className="shrink-0 text-amber-400" />
        <span className="shrink-0 text-[10px] text-slate-500">{fmtRelative(group.mention_msg.date)}</span>
      </div>

      {!expanded && (
        <div
          role="button"
          tabIndex={0}
          onClick={() => onNavigate(group.chat.telegram_id, group.mention_msg.telegram_id)}
          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onNavigate(group.chat.telegram_id, group.mention_msg.telegram_id) } }}
          className="px-3 pb-2.5 cursor-pointer"
        >
          <p className="text-xs text-slate-500 truncate">
            <span className="text-amber-300/70 font-medium">{senderName}: </span>
            {mentionText || <span className="italic">{group.mention_msg.media_type ? `[${group.mention_msg.media_type}]` : '[no text]'}</span>}
          </p>
        </div>
      )}

      {expanded && (
        <div className="border-t border-slate-700/50">
          <div className="px-3 py-2.5 space-y-1.5">
            {allMsgs.map(({ msg, highlighted }, i) => (
              <MentionMsgRow key={`${msg.telegram_id}-${i}`} msg={msg} highlighted={highlighted} />
            ))}
          </div>
          {group.user_reply_msg && (
            <div className="px-3 pb-2.5 space-y-1.5">
              {group.reply_has_gap && (
                <div className="text-center text-[11px] text-slate-500 py-0.5 select-none">
                  <span className="rounded bg-slate-700/40 px-2 py-0.5">···</span>
                </div>
              )}
              <MentionMsgRow msg={group.user_reply_msg} highlighted={false} />
            </div>
          )}
          <div className="px-3 pb-3">
            <button
              onClick={() => onNavigate(group.chat.telegram_id, group.mention_msg.telegram_id)}
              className="w-full rounded-lg bg-slate-700 hover:bg-slate-600 px-3 py-1.5 text-xs text-slate-300 transition-colors"
            >
              Jump to mention →
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

type HomeTab = 'catchup' | 'mentions' | 'needs_reply'
type MentionFilter = 'not_acted' | 'all' | 'acknowledged' | 'replied'

export default function Home() {
  const [data, setData] = useState<DashboardData | null>(null)
  const [loading, setLoading] = useState(true)
  const navigate = useNavigate()

  // Tab state -- default to Catch Up triage queue
  const [activeTab, setActiveTab] = useState<HomeTab>('catchup')
  const [catchUpCount, setCatchUpCount] = useState<number | null>(null)

  // Mentions state
  const [mentions, setMentions] = useState<MentionGroup[]>([])
  const [mentionsLoading, setMentionsLoading] = useState(true)
  const [mentionFilter, setMentionFilter] = useState<MentionFilter>('not_acted')
  const [selectedMentionIdx, setSelectedMentionIdx] = useState(0)
  const selectedMentionRef = useRef<HTMLDivElement | null>(null)

  const refreshDashboard = useCallback(() => {
    getDashboard().then(setData).catch(() => {})
  }, [])

  useEffect(() => {
    getDashboard()
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  // Auto-refresh dashboard every 60s
  useEffect(() => {
    const interval = setInterval(() => {
      getDashboard().then(setData).catch(() => {})
    }, 60000)
    return () => clearInterval(interval)
  }, [])

  // Load mentions
  const fetchMentions = useCallback(async () => {
    setMentionsLoading(true)
    try {
      const data = await getMentions({ days: 7 })
      setMentions(data)
    } catch {}
    setMentionsLoading(false)
  }, [])

  useEffect(() => {
    fetchMentions()
  }, [fetchMentions])

  // Filter mentions client-side
  const visibleMentions = useMemo(() => mentions.filter((g) => {
    const isReplied = g.replied
    const isAcknowledged = !!g.mention_msg.my_reaction && !isReplied
    if (mentionFilter === 'replied') return isReplied
    if (mentionFilter === 'acknowledged') return isAcknowledged
    if (mentionFilter === 'not_acted') return !isReplied && !isAcknowledged
    return true
  }), [mentions, mentionFilter])

  // Keep selectedIdx in bounds
  useEffect(() => {
    if (visibleMentions.length > 0) setSelectedMentionIdx((i) => Math.min(i, visibleMentions.length - 1))
  }, [visibleMentions.length])

  // Scroll selected into view
  useEffect(() => {
    selectedMentionRef.current?.scrollIntoView({ block: 'nearest' })
  }, [selectedMentionIdx])

  const handleMentionNavigate = (chatId: number, msgId: number) => {
    navigate(`/chats?chatId=${chatId}&msgId=${msgId}`)
  }

  // Keyboard shortcuts for mentions
  const mentionShortcuts = useMemo(() => {
    if (activeTab !== 'mentions') return {}
    return {
      'j': () => setSelectedMentionIdx((i) => Math.min(i + 1, visibleMentions.length - 1)),
      'arrowdown': () => setSelectedMentionIdx((i) => Math.min(i + 1, visibleMentions.length - 1)),
      'k': () => setSelectedMentionIdx((i) => Math.max(i - 1, 0)),
      'arrowup': () => setSelectedMentionIdx((i) => Math.max(i - 1, 0)),
      'a': () => {
        const g = visibleMentions[selectedMentionIdx]
        if (!g) return
        const emoji = g.mention_msg.my_reaction ? null : '👀'
        reactToMessage(g.chat.telegram_id, g.mention_msg.telegram_id, emoji)
          .then(() => {
            setMentions((prev) => prev.map((pg) =>
              pg.mention_msg.telegram_id === g.mention_msg.telegram_id
                ? { ...pg, mention_msg: { ...pg.mention_msg, my_reaction: emoji } }
                : pg
            ))
          })
          .catch((err) => console.error('Failed to react to mention:', err))
      },
      'r': () => {
        const g = visibleMentions[selectedMentionIdx]
        if (g) navigate(`/chats?chatId=${g.chat.telegram_id}&msgId=${g.mention_msg.telegram_id}`)
      },
    }
  }, [activeTab, visibleMentions, selectedMentionIdx, navigate])

  useKeyboardShortcuts(mentionShortcuts)

  if (loading) {
    return (
      <div className="flex flex-1 items-center justify-center text-slate-500 text-sm">
        Loading dashboard...
      </div>
    )
  }

  if (!data) {
    return (
      <div className="flex flex-1 items-center justify-center text-slate-500 text-sm">
        Failed to load dashboard
      </div>
    )
  }

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-3xl px-6 py-8">
        {/* Greeting */}
        <h1 className="text-2xl font-bold text-gray-100 mb-6">{getGreeting()}</h1>

        {/* Stats */}
        <div className="mb-8">
          <div className="rounded-xl bg-slate-800 border border-slate-700/50 p-4">
            <div className="flex items-center gap-2 mb-1">
              <Mail size={14} className="text-indigo-400" />
              <span className="text-xs text-slate-400">Unread Today</span>
            </div>
            <p className="text-2xl font-bold text-gray-100">{data.stats.unread_today}</p>
            <p className="text-[10px] text-slate-500 mt-0.5">From important contacts & tracked chats</p>
          </div>
        </div>

        {/* Tabs: Catch Up / Mentions / Needs Reply */}
        <div className="mb-4">
          <div className="flex border-b border-slate-700/50">
            <button
              onClick={() => setActiveTab('catchup')}
              className={`px-4 py-2 text-sm font-medium transition-colors border-b-2 ${
                activeTab === 'catchup'
                  ? 'border-emerald-400 text-emerald-400'
                  : 'border-transparent text-slate-500 hover:text-slate-300'
              }`}
            >
              <span className="flex items-center gap-1.5">
                <ListChecks size={13} />
                Catch Up
                {(catchUpCount ?? data.needs_reply.length) > 0 && (
                  <span className="rounded-full bg-emerald-500/20 px-1.5 py-0.5 text-[10px] font-medium text-emerald-400">
                    {catchUpCount ?? data.needs_reply.length}
                  </span>
                )}
              </span>
            </button>
            <button
              onClick={() => setActiveTab('mentions')}
              className={`px-4 py-2 text-sm font-medium transition-colors border-b-2 ${
                activeTab === 'mentions'
                  ? 'border-amber-400 text-amber-400'
                  : 'border-transparent text-slate-500 hover:text-slate-300'
              }`}
            >
              <span className="flex items-center gap-1.5">
                <AtSign size={13} />
                Mentions
                {visibleMentions.length > 0 && (
                  <span className="rounded-full bg-amber-500/20 px-1.5 py-0.5 text-[10px] font-medium text-amber-400">
                    {visibleMentions.length}
                  </span>
                )}
              </span>
            </button>
            <button
              onClick={() => setActiveTab('needs_reply')}
              className={`px-4 py-2 text-sm font-medium transition-colors border-b-2 ${
                activeTab === 'needs_reply'
                  ? 'border-indigo-400 text-indigo-400'
                  : 'border-transparent text-slate-500 hover:text-slate-300'
              }`}
            >
              <span className="flex items-center gap-1.5">
                <Mail size={13} />
                Needs Reply
                {data.needs_reply.length > 0 && (
                  <span className="rounded-full bg-indigo-500/20 px-1.5 py-0.5 text-[10px] font-medium text-indigo-400">
                    {data.needs_reply.length}
                  </span>
                )}
              </span>
            </button>
          </div>
        </div>

        {/* Catch Up triage tab content */}
        {activeTab === 'catchup' && (
          <div className="mb-8">
            <CatchUpQueue needsReply={data.needs_reply} onRefreshDashboard={refreshDashboard} onBadgeCount={setCatchUpCount} />
          </div>
        )}

        {/* Mentions tab content */}
        {activeTab === 'mentions' && (
          <div className="mb-8">
            {/* Filter buttons */}
            <div className="flex items-center gap-1.5 mb-3">
              {([
                { label: 'Not acted', value: 'not_acted' as MentionFilter },
                { label: 'All', value: 'all' as MentionFilter },
                { label: 'Acknowledged', value: 'acknowledged' as MentionFilter },
                { label: 'Replied', value: 'replied' as MentionFilter },
              ]).map((opt) => (
                <button
                  key={opt.value}
                  onClick={() => setMentionFilter(opt.value)}
                  className={`rounded-full px-2.5 py-0.5 text-[11px] font-medium transition-colors ${
                    mentionFilter === opt.value
                      ? opt.value === 'not_acted'
                        ? 'bg-slate-500/30 text-slate-200'
                        : opt.value === 'acknowledged'
                          ? 'bg-amber-500/25 text-amber-300'
                          : opt.value === 'replied'
                            ? 'bg-emerald-500/25 text-emerald-300'
                            : 'bg-slate-600 text-slate-200'
                      : 'text-slate-500 hover:text-slate-300 hover:bg-slate-700/50'
                  }`}
                >
                  {opt.label}
                </button>
              ))}
              <button
                onClick={fetchMentions}
                disabled={mentionsLoading}
                className="ml-auto text-slate-500 hover:text-slate-300 disabled:opacity-50 transition-colors"
                title="Refresh mentions"
              >
                <RefreshCw size={12} className={mentionsLoading ? 'animate-spin' : ''} />
              </button>
            </div>

            {mentionsLoading ? (
              <div className="rounded-xl bg-slate-800 border border-slate-700/50 px-4 py-8 text-center">
                <p className="text-sm text-slate-500">Loading mentions...</p>
              </div>
            ) : visibleMentions.length === 0 ? (
              <div className="rounded-xl bg-slate-800 border border-slate-700/50 px-4 py-8 text-center">
                <AtSign size={20} className="mx-auto mb-2 text-amber-400/50" />
                <p className="text-sm text-slate-500">
                  {mentionFilter === 'not_acted' ? 'No unacted mentions in the last 7 days' : 'No mentions found'}
                </p>
              </div>
            ) : (
              <div className="space-y-2">
                {visibleMentions.map((group, i) => (
                  <div key={`${group.chat.telegram_id}-${group.mention_msg.telegram_id}-${i}`} ref={i === selectedMentionIdx ? selectedMentionRef : null}>
                    <HomeMentionCard
                      group={group}
                      onNavigate={handleMentionNavigate}
                      selected={i === selectedMentionIdx}
                    />
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Needs Reply tab content */}
        {activeTab === 'needs_reply' && (
          <div className="mb-8">
            {data.needs_reply.length === 0 ? (
              <div className="rounded-xl bg-slate-800 border border-slate-700/50 px-4 py-6 text-center">
                <p className="text-sm text-slate-500">You're all caught up! No conversations waiting for your reply.</p>
              </div>
            ) : (
              <div className="flex flex-col gap-2">
                {data.needs_reply.map((item) => (
                  <button
                    key={item.chat.telegram_id}
                    onClick={() => navigate(`/chats?chatId=${item.chat.telegram_id}`)}
                    className="flex items-center gap-3 rounded-xl bg-slate-800 border border-slate-700/50 px-4 py-3 text-left hover:bg-slate-700/50 transition-colors w-full"
                  >
                    <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full bg-slate-700 text-sm font-medium text-slate-300">
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
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Important Contacts */}
        <div className="mb-8">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-xs font-medium uppercase tracking-wider text-slate-400">Important Contacts</h2>
            <Link to="/contacts?filter=important" className="text-[10px] text-indigo-400 hover:text-indigo-300 transition-colors flex items-center gap-0.5">
              View all <ArrowRight size={10} />
            </Link>
          </div>
          {data.attention_contacts.length === 0 ? (
            <div className="rounded-xl bg-slate-800 border border-slate-700/50 px-4 py-6 text-center">
              <p className="text-sm text-slate-500">All contacts are in good standing.</p>
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              {data.attention_contacts.map((c) => (
                <button
                  key={c.id}
                  onClick={() => navigate(`/chats?chatId=${c.telegram_id}`)}
                  className="rounded-xl bg-slate-800 border border-slate-700/50 px-4 py-3 hover:bg-slate-700/50 transition-colors text-left w-full"
                >
                  <div className="flex items-center gap-3">
                    <div className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full bg-slate-700 text-[10px] font-medium text-slate-300">
                      {c.name.charAt(0).toUpperCase()}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <p className="truncate text-sm font-medium text-gray-200">{c.name}</p>
                        {c.relationship_status && (
                          <span className={`flex-shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium ${
                            c.relationship_status === 'needs_reply' ? 'bg-red-500/20 text-red-400' :
                            c.relationship_status === 'stalled' ? 'bg-orange-500/20 text-orange-400' :
                            c.relationship_status === 'cold' ? 'bg-slate-500/20 text-slate-400' :
                            c.relationship_status === 'active' ? 'bg-emerald-500/20 text-emerald-400' :
                            c.relationship_status === 'nurturing' ? 'bg-amber-500/20 text-amber-400' :
                            'bg-slate-700 text-slate-400'
                          }`}>
                            {c.relationship_status.replace('_', ' ')}
                            {c.status_source === 'manual' && ' (manual)'}
                          </span>
                        )}
                      </div>
                      {c.primary_position && (
                        <p className="truncate text-[10px] text-slate-500">
                          {c.primary_position.title
                            ? `${c.primary_position.title} @ ${c.primary_position.organization}`
                            : c.primary_position.organization}
                        </p>
                      )}
                    </div>
                  </div>
                  {(c.attention_reason || c.status_explanation) && (
                    <div className="mt-2 ml-10 space-y-1">
                      {c.attention_reason && (
                        <p className="text-[11px] text-amber-400/80">{c.attention_reason}</p>
                      )}
                      {c.status_explanation && (
                        <p className="text-[11px] text-slate-500">{c.status_explanation}</p>
                      )}
                      {c.recommended_action && (
                        <p className="text-[11px] text-indigo-400/70">{c.recommended_action}</p>
                      )}
                    </div>
                  )}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Latest Briefing */}
        <div className="mb-8">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-xs font-medium uppercase tracking-wider text-slate-400">Latest Briefing</h2>
            <Link
              to="/intelligence?tab=briefing"
              className="text-[10px] text-indigo-400 hover:text-indigo-300 transition-colors flex items-center gap-0.5"
            >
              View full <ArrowRight size={10} />
            </Link>
          </div>
          {!data.latest_briefing ? (
            <div className="rounded-xl bg-slate-800 border border-slate-700/50 px-4 py-6 text-center">
              <p className="text-sm text-slate-500">No briefing yet.</p>
              <Link
                to="/intelligence?tab=briefing"
                className="mt-1 text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
              >
                Generate one from Intelligence
              </Link>
            </div>
          ) : (
            <div className="rounded-xl bg-slate-800 border border-slate-700/50 p-4">
              <div className="flex items-center gap-2 mb-3">
                <FileText size={12} className="text-slate-400" />
                <span className="text-[10px] text-slate-500">
                  {fmtRelative(data.latest_briefing.generated_at)}
                </span>
              </div>
              <div className="flex flex-col gap-2">
                {data.latest_briefing.entries.map((e) => (
                  <div key={e.id} className="text-xs">
                    <span className="font-medium text-slate-300">{e.chat_name}: </span>
                    <span className="text-slate-400 line-clamp-2">{e.summary}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
