import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { conversationUrl, personUrl } from '../lib/routes'
import { canStartConversation, NoConversationError, openDirectConversation } from '../lib/directConversation'
import { useAsyncAction } from '../hooks/useAsyncAction'
import { Mail, MessageSquare, ArrowRight, FileText, AtSign, RefreshCw, ListChecks, Users, AlertCircle } from 'lucide-react'
import type { CatchUpItem, Person } from '../api/types'
import { getDashboard } from '../api/client'
import { useLatestQuery } from '../hooks/useLatestQuery'
import { useCoalescedRefetch } from '../hooks/useCoalescedRefetch'
import { useWebSocket } from '../hooks/useWebSocket'
import { useMentionNavigator, useMentions } from '../hooks/useMentions'
import CatchUpQueue, { applyTriageUpdate } from '../components/CatchUpQueue'
import MentionCard, {
  MENTION_FILTER_OPTIONS,
  countMentions,
  filterMentions,
  mentionFilterClass,
  mentionKey,
  type MentionFilter,
} from '../components/MentionCard'
import { BriefingEntryList } from '../components/BriefingEntryCard'
import NoAccountsNotice from '../components/NoAccountsNotice'
import { errorMessage, toast } from '../lib/toast'
import { fmtRelative } from '../utils/time'

function getGreeting(): string {
  const hour = new Date().getHours()
  if (hour < 12) return 'Good morning'
  if (hour < 17) return 'Good afternoon'
  return 'Good evening'
}

const DASHBOARD_POLL_MS = 60_000
const DASHBOARD_REFETCH_INTERVAL_MS = 5000

type HomeTab = 'catchup' | 'mentions'
type ContactsSubTab = 'needs_checkin' | 'all'

export default function Home() {
  const navigate = useNavigate()
  // Same "message this person" rule as Contacts and the avatar menu (lib/directConversation)
  const messagePerson = useAsyncAction(async (p: Person) => {
    try {
      const conv = await openDirectConversation(p.id, { canStart: canStartConversation(p.identities) })
      navigate(conversationUrl(conv))
    } catch (e) {
      if (e instanceof NoConversationError) { toast.info(`No conversation with ${p.name} yet`); return }
      throw e
    }
  }, { errorContext: 'Couldn’t open a conversation' })
  const [activeTab, setActiveTab] = useState<HomeTab>('catchup')
  const [contactsSubTab, setContactsSubTab] = useState<ContactsSubTab>('needs_checkin')

  // Dashboard: latest-only (a slow older response can't overwrite a newer one)
  const dashboard = useLatestQuery(['dashboard'], (signal) => getDashboard(signal))
  const { data, refetch: refetchDashboard, setData: setDashboard } = dashboard
  const refreshDashboard = useCoalescedRefetch(refetchDashboard, DASHBOARD_REFETCH_INTERVAL_MS)

  useEffect(() => {
    const t = setInterval(refreshDashboard, DASHBOARD_POLL_MS)
    return () => clearInterval(t)
  }, [refreshDashboard])

  // New incoming messages and syncs change needs-reply; storms collapse into one refetch
  useWebSocket((evt) => {
    if (
      (evt.type === 'new_message' && !evt.is_outgoing) ||
      evt.type === 'conversations_synced' || evt.type === 'conversation_synced' ||
      evt.type === 'bulk_sync_complete' || evt.type === 'resync'
    ) {
      refreshDashboard()
    }
  })

  const handleTriageChange = (item: CatchUpItem) => {
    setDashboard((prev) => (prev ? applyTriageUpdate(prev, item) : prev))
    void refetchDashboard()
  }

  // Mentions (shared with Intelligence)
  const [mentionFilter, setMentionFilter] = useState<MentionFilter>('not_acted')
  const mentions = useMentions({ days: 7 })
  const mentionCounts = useMemo(() => countMentions(mentions.groups), [mentions.groups])
  const visibleMentions = useMemo(() => filterMentions(mentions.groups, mentionFilter), [mentions.groups, mentionFilter])
  const mentionNav = useMentionNavigator(visibleMentions, {
    enabled: activeTab === 'mentions',
    onAcknowledge: mentions.toggleAcknowledge,
  })

  if (!data) {
    if (dashboard.error != null && !dashboard.fetching) {
      return (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 text-sm text-slate-500">
          <p className="flex items-center gap-1.5"><AlertCircle size={14} className="text-rose-400" />Failed to load dashboard: {errorMessage(dashboard.error)}</p>
          <button onClick={() => void refetchDashboard()} className="text-xs text-indigo-400 hover:text-indigo-300">Retry</button>
        </div>
      )
    }
    return (
      <div className="flex flex-1 items-center justify-center text-slate-500 text-sm">
        Loading dashboard...
      </div>
    )
  }

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-3xl px-6 py-8">
        {/* Greeting */}
        <h1 className="text-2xl font-bold text-gray-100 mb-6">{getGreeting()}</h1>

        <NoAccountsNotice className="mb-8" />

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

        {/* Tabs: Catch Up / Mentions */}
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
                {data.needs_reply.length > 0 && (
                  <span className="rounded-full bg-emerald-500/20 px-1.5 py-0.5 text-[10px] font-medium text-emerald-400">
                    {data.needs_reply.length}
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
          </div>
        </div>

        {/* Catch Up triage tab content */}
        {activeTab === 'catchup' && (
          <div className="mb-8">
            <CatchUpQueue
              needsReply={data.needs_reply}
              recentlyHandled={data.recently_handled ?? []}
              onTriageChange={handleTriageChange}
              onRefreshDashboard={refreshDashboard}
              refreshing={dashboard.fetching}
            />
          </div>
        )}

        {/* Mentions tab content */}
        {activeTab === 'mentions' && (
          <div className="mb-8">
            {/* Filter buttons */}
            <div className="flex items-center gap-1.5 mb-3">
              {MENTION_FILTER_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  onClick={() => setMentionFilter(opt.value)}
                  className={`rounded-full px-2.5 py-0.5 text-[11px] font-medium transition-colors ${mentionFilterClass(opt.value, mentionFilter === opt.value)}`}
                >
                  {opt.label}{mentionCounts[opt.value] > 0 ? ` (${mentionCounts[opt.value]})` : ''}
                </button>
              ))}
              <button
                onClick={() => void mentions.refetch()}
                disabled={mentions.fetching}
                className="ml-auto text-slate-500 hover:text-slate-300 disabled:opacity-50 transition-colors"
                title="Refresh mentions"
              >
                <RefreshCw size={12} className={mentions.fetching ? 'animate-spin' : ''} />
              </button>
            </div>

            {mentions.loading ? (
              <div className="rounded-xl bg-slate-800 border border-slate-700/50 px-4 py-8 text-center">
                <p className="text-sm text-slate-500">Loading mentions...</p>
              </div>
            ) : mentions.error != null ? (
              <div className="rounded-xl bg-slate-800 border border-rose-700/30 px-4 py-6 text-center">
                <p className="text-sm text-rose-400">Could not load mentions: {errorMessage(mentions.error)}</p>
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
                  <div key={mentionKey(group)} ref={i === mentionNav.selectedIdx ? mentionNav.selectedRef : null}>
                    <MentionCard
                      group={group}
                      onNavigate={mentionNav.openInChat}
                      selected={i === mentionNav.selectedIdx}
                      onToggleExpand={() => mentionNav.setSelectedIdx(i)}
                    />
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Important Contacts */}
        <div className="mb-8">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-xs font-medium uppercase tracking-wider text-slate-400 flex items-center gap-1.5">
              <Users size={12} />
              Important Contacts
            </h2>
            <Link to="/contacts?filter=important" className="text-[10px] text-indigo-400 hover:text-indigo-300 transition-colors flex items-center gap-0.5">
              View all <ArrowRight size={10} />
            </Link>
          </div>

          {/* Sub-tabs for Important Contacts */}
          <div className="flex gap-1.5 mb-3">
            <button
              onClick={() => setContactsSubTab('needs_checkin')}
              className={`rounded-full px-3 py-1 text-[11px] font-medium transition-colors ${
                contactsSubTab === 'needs_checkin'
                  ? 'bg-rose-500/20 text-rose-400'
                  : 'text-slate-500 hover:text-slate-300 hover:bg-slate-700/50'
              }`}
            >
              Needs Check-in
              {(() => {
                const count = data.attention_people.filter(
                  (c) => c.relationship_status === 'needs_reply' || c.relationship_status === 'stalled'
                ).length
                return count > 0 ? (
                  <span className="ml-1 rounded-full bg-rose-500/20 px-1.5 py-0.5 text-[9px] font-medium text-rose-400">
                    {count}
                  </span>
                ) : null
              })()}
            </button>
            <button
              onClick={() => setContactsSubTab('all')}
              className={`rounded-full px-3 py-1 text-[11px] font-medium transition-colors ${
                contactsSubTab === 'all'
                  ? 'bg-slate-500/20 text-slate-200'
                  : 'text-slate-500 hover:text-slate-300 hover:bg-slate-700/50'
              }`}
            >
              All Contacts
              <span className="ml-1 rounded-full bg-slate-600/40 px-1.5 py-0.5 text-[9px] font-medium text-slate-400">
                {data.attention_people.length}
              </span>
            </button>
          </div>

          {(() => {
            const contactsToShow = contactsSubTab === 'needs_checkin'
              ? data.attention_people.filter(
                  (c) => c.relationship_status === 'needs_reply' || c.relationship_status === 'stalled'
                )
              : data.attention_people

            if (contactsToShow.length === 0) {
              return (
                <div className="rounded-xl bg-slate-800 border border-slate-700/50 px-4 py-6 text-center">
                  <p className="text-sm text-slate-500">
                    {contactsSubTab === 'needs_checkin'
                      ? 'No contacts need your check-in right now.'
                      : 'All contacts are in good standing.'}
                  </p>
                </div>
              )
            }

            return (
              <div className="flex flex-col gap-2">
                {contactsToShow.map((c) => (
                  <div
                    key={c.id}
                    role="button"
                    tabIndex={0}
                    onClick={() => navigate(personUrl(c.id))}
                    onKeyDown={(e) => { if (e.key === 'Enter') navigate(personUrl(c.id)) }}
                    className="cursor-pointer rounded-xl bg-slate-800 border border-slate-700/50 px-4 py-3 hover:bg-slate-700/50 transition-colors text-left w-full"
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
                      <button
                        type="button"
                        onClick={(e) => { e.stopPropagation(); void messagePerson.run(c) }}
                        disabled={messagePerson.pending}
                        title="Message"
                        aria-label={`Message ${c.name}`}
                        className="flex-shrink-0 rounded-lg p-1.5 text-slate-500 hover:bg-slate-700 hover:text-indigo-300 transition-colors disabled:opacity-50"
                      >
                        <MessageSquare size={14} />
                      </button>
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
                  </div>
                ))}
              </div>
            )
          })()}
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
              {/* Same cards as Intelligence → Briefing: [n] citations jump to the source message */}
              <BriefingEntryList entries={data.latest_briefing.entries} />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
