/**
 * A person's cross-channel timeline on their page: one-to-one conversations,
 * their group messages, the emails they take part in and the meetings they
 * attended, newest first. Each row opens the message where it lives (a
 * meeting: its day in Calendar); "Load more" pages back by date.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { CalendarDays, Loader2 } from 'lucide-react'
import { getPersonTimeline, type TimelineItem, type TimelineMeetingItem, type TimelineMessageItem } from '../api/timeline'
import { calendarEventUrl, eventTimeLabel, parseDateKey } from '../pages/calendar/agenda'
import { useLatestQuery } from '../hooks/useLatestQuery'
import { useWsEvent } from '../hooks/useWebSocket'
import { conversationTitle } from '../lib/conversation'
import { conversationUrl, openConversationUrl } from '../lib/routes'
import { errorMessage, reportError } from '../lib/toast'
import { fmtDate } from '../utils/time'
import ChannelIcon from './ChannelIcon'

export const TIMELINE_PAGE = 30

/** Subject for email, else the text, else "[photo]"-style attachment label */
export function timelineSnippet(item: TimelineMessageItem): string {
  const m = item.message
  const isEmail = item.conversation?.kind === 'email_thread'
  const text = (isEmail ? m.subject || m.text : m.text || m.subject)?.trim()
  if (text) return text.replace(/\s+/g, ' ')
  const first = m.attachments[0]
  return first ? `[${first.kind}]` : isEmail ? '(no subject)' : ''
}

function senderLabel(item: TimelineMessageItem): string {
  if (item.message.is_outgoing) return 'You'
  return item.message.sender?.name || 'Unknown'
}

export default function PersonTimeline({ personId }: { personId: number }) {
  const first = useLatestQuery(
    ['person-timeline', personId],
    (signal) => getPersonTimeline(personId, { limit: TIMELINE_PAGE, signal }),
  )
  // Older pages, appended to the first one; dropped whenever the first page reloads
  const [older, setOlder] = useState<{ items: TimelineItem[]; hasMore: boolean } | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const genRef = useRef(0)
  useEffect(() => {
    genRef.current += 1
    setOlder(null)
    setLoadingMore(false)
  }, [first.data])

  useWsEvent('resync', () => { void first.refetch() })

  const items = [...(first.data?.items ?? []), ...(older?.items ?? [])]
  const hasMore = older ? older.hasMore : first.data?.has_more ?? false

  const loadMore = useCallback(async () => {
    // Pages are cut by messages: continue before the oldest message shown
    const last = [...items].reverse().find((i): i is TimelineMessageItem => i.kind === 'message')
    if (!last || loadingMore) return
    const gen = genRef.current
    setLoadingMore(true)
    try {
      const page = await getPersonTimeline(personId, { limit: TIMELINE_PAGE, before: last.message })
      if (gen !== genRef.current) return
      setOlder((prev) => {
        const seen = new Set([...(first.data?.items ?? []), ...(prev?.items ?? [])].map(itemKey))
        return {
          items: [...(prev?.items ?? []), ...page.items.filter((i) => !seen.has(itemKey(i)))],
          hasMore: page.has_more,
        }
      })
    } catch (e) {
      if (gen === genRef.current) reportError(e, 'Couldn’t load more of the timeline')
    } finally {
      if (gen === genRef.current) setLoadingMore(false)
    }
  }, [items, loadingMore, personId, first.data])

  return (
    <div className="mb-6 rounded-xl bg-slate-800 border border-slate-700/50 p-4" data-testid="person-timeline">
      <h3 className="mb-3 text-xs font-medium uppercase tracking-wider text-slate-500">Timeline</h3>
      {first.loading ? (
        <p className="py-2 text-xs text-slate-500">Loading…</p>
      ) : first.error && !first.data ? (
        <p className="py-2 text-xs text-red-400">{errorMessage(first.error)}</p>
      ) : items.length === 0 ? (
        <p className="py-2 text-xs text-slate-500">No messages yet</p>
      ) : (
        <div className="flex flex-col">
          {items.map((item) => (
            item.kind === 'meeting'
              ? <MeetingRow key={itemKey(item)} item={item} />
              : <TimelineRow key={itemKey(item)} item={item} />
          ))}
          {hasMore && (
            <button
              onClick={() => { void loadMore() }}
              disabled={loadingMore}
              className="mt-2 flex items-center justify-center gap-1.5 rounded-lg py-1.5 text-xs text-slate-400 hover:bg-slate-700/50 hover:text-slate-200 transition-colors disabled:opacity-50"
            >
              {loadingMore && <Loader2 size={12} className="animate-spin" />}
              Load more
            </button>
          )}
        </div>
      )}
    </div>
  )
}

const itemKey = (item: TimelineItem) => (item.kind === 'meeting' ? `e${item.event.id}` : `m${item.message.id}`)

function TimelineRow({ item }: { item: TimelineMessageItem }) {
  const conv = item.conversation
  const m = item.message
  const title = conv ? conversationTitle({ title: conv.title, kind: conv.kind }) : 'Conversation'
  return (
    <Link
      to={conv ? conversationUrl(conv, m.id) : openConversationUrl(m.conversation_id, m.id)}
      className="flex items-center gap-2.5 rounded-lg px-2 py-1.5 text-sm hover:bg-slate-700/50 transition-colors"
      data-testid="timeline-row"
    >
      <ChannelIcon channel={conv?.channel} size={12} />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-1.5">
          <span className="truncate text-xs font-medium text-slate-300">{title}</span>
          <span className="flex-shrink-0 text-[11px] text-slate-500">· {senderLabel(item)}</span>
        </div>
        <p className="truncate text-xs text-slate-400">{timelineSnippet(item)}</p>
      </div>
      <span className="flex-shrink-0 text-[11px] text-slate-500">{fmtDate(m.date)}</span>
    </Link>
  )
}

function MeetingRow({ item }: { item: TimelineMeetingItem }) {
  const ev = item.event
  const others = ev.attendees.filter((a) => !a.is_self).length
  const date = ev.all_day ? parseDateKey(ev.start_at) : ev.start_at
  return (
    <Link
      to={calendarEventUrl(ev)}
      className="flex items-center gap-2.5 rounded-lg px-2 py-1.5 text-sm hover:bg-slate-700/50 transition-colors"
      data-testid="timeline-meeting"
    >
      <CalendarDays size={12} className="flex-shrink-0 text-slate-400" />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-1.5">
          <span className="truncate text-xs font-medium text-slate-300">{ev.title || '(no title)'}</span>
          <span className="flex-shrink-0 text-[11px] text-slate-500">· Meeting</span>
        </div>
        <p className="truncate text-xs text-slate-400">
          {[eventTimeLabel(ev), ev.location, others > 0 ? `${others} ${others === 1 ? 'person' : 'people'}` : null].filter(Boolean).join(' · ')}
        </p>
      </div>
      <span className="flex-shrink-0 text-[11px] text-slate-500">{fmtDate(date)}</span>
    </Link>
  )
}
