import { useEffect, useMemo, useState } from 'react'
import { AlertCircle, AtSign, Loader2, RefreshCw, Search, Tag } from 'lucide-react'
import { getTags } from '../../api/client'
import MentionCard, {
  MENTION_FILTER_OPTIONS,
  filterMentions,
  mentionFilterClass,
  mentionKey,
  type MentionFilter,
} from '../../components/MentionCard'
import { useDebouncedValue } from '../../hooks/useDebouncedQuery'
import { useMentionNavigator, useMentions } from '../../hooks/useMentions'
import { errorMessage, reportError } from '../../lib/toast'

const TIME_OPTIONS = [
  { label: '7 days', value: 7 },
  { label: '14 days', value: 14 },
  { label: '1 month', value: 30 },
]

// Intelligence lists everything by default; Home defaults to "Not acted"
const FILTERS = [...MENTION_FILTER_OPTIONS].sort((a, b) => (a.value === 'all' ? -1 : b.value === 'all' ? 1 : 0))

export default function MentionsTab() {
  const [days, setDays] = useState(7)
  const [groupSearch, setGroupSearch] = useState('')
  const groupName = useDebouncedValue(groupSearch, 350)
  const [tagFilter, setTagFilter] = useState('')
  const [actionFilter, setActionFilter] = useState<MentionFilter>('all')
  const [availableTags, setAvailableTags] = useState<{ id: number; name: string }[]>([])

  useEffect(() => {
    getTags().then(setAvailableTags).catch((e) => reportError(e, 'Could not load tags'))
  }, [])

  const { groups, loading, fetching, error, refetch, toggleAcknowledge } = useMentions({ days, groupName, tagName: tagFilter })
  const visible = useMemo(() => filterMentions(groups, actionFilter), [groups, actionFilter])
  const { selectedIdx, setSelectedIdx, selectedRef, openInChat } = useMentionNavigator(visible, { onAcknowledge: toggleAcknowledge })

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      <div className="border-b border-slate-700/50 px-4 pt-3 pb-0">
        <div className="flex items-center justify-between mb-2">
          <div>
            <p className="text-sm font-semibold text-slate-100">Mentions</p>
            <p className="text-xs text-slate-500">Messages where you were tagged</p>
          </div>
          <button
            onClick={() => void refetch()}
            disabled={fetching}
            className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs text-slate-400 hover:bg-slate-700 disabled:opacity-50 transition-colors"
            title="Refresh mentions"
          >
            <RefreshCw size={13} className={fetching ? 'animate-spin' : ''} />
            Refresh
          </button>
        </div>

        <div className="flex items-center gap-2 pb-3 flex-wrap">
          <div className="relative flex-1 min-w-32">
            <Search size={11} className="absolute left-2 top-1/2 -translate-y-1/2 text-slate-500 pointer-events-none" />
            <input
              type="text"
              value={groupSearch}
              onChange={(e) => setGroupSearch(e.target.value)}
              placeholder="Filter by group…"
              className="w-full rounded-lg border border-slate-700 bg-slate-800 pl-6 pr-2 py-1 text-xs text-slate-200 placeholder-slate-500 outline-none focus:border-amber-500/50"
            />
          </div>

          <div className="relative">
            <Tag size={11} className="absolute left-2 top-1/2 -translate-y-1/2 text-slate-500 pointer-events-none" />
            <select
              value={tagFilter}
              onChange={(e) => setTagFilter(e.target.value)}
              className="appearance-none rounded-lg border border-slate-700 bg-slate-800 pl-6 pr-3 py-1 text-xs text-slate-200 outline-none focus:border-amber-500/50 cursor-pointer"
            >
              <option value="">All tags</option>
              {availableTags.map((t) => (
                <option key={t.id} value={t.name}>{t.name}</option>
              ))}
            </select>
          </div>

          <div className="flex rounded-lg border border-slate-700 overflow-hidden">
            {TIME_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                onClick={() => setDays(opt.value)}
                className={`px-2.5 py-1 text-xs transition-colors ${
                  days === opt.value ? 'bg-amber-600/80 text-white' : 'text-slate-400 hover:bg-slate-700'
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>

        <div className="flex items-center gap-1.5 pb-3">
          {FILTERS.map((opt) => (
            <button
              key={opt.value}
              onClick={() => setActionFilter(opt.value)}
              className={`rounded-full px-2.5 py-0.5 text-[11px] font-medium transition-colors ${mentionFilterClass(opt.value, actionFilter === opt.value)}`}
            >
              {opt.label}
            </button>
          ))}
          {groups.length > 0 && (
            <span className="ml-auto text-[10px] text-slate-500">{visible.length} / {groups.length}</span>
          )}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-4">
        {loading && (
          <div className="flex items-center justify-center py-16 gap-2 text-slate-500">
            <Loader2 size={16} className="animate-spin" />
            <span className="text-sm">Loading mentions…</span>
          </div>
        )}

        {!loading && error != null && (
          <div className="flex items-center gap-2 rounded-lg bg-red-900/20 border border-red-700/30 px-3 py-2 text-xs text-red-400">
            <AlertCircle size={13} />
            {errorMessage(error)}
          </div>
        )}

        {!loading && error == null && visible.length === 0 && (
          <div className="flex flex-col items-center justify-center py-16 gap-3 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-amber-500/10 text-amber-400">
              <AtSign size={24} />
            </div>
            <div>
              <p className="text-sm font-medium text-slate-300">No mentions found</p>
              <p className="mt-1 text-xs text-slate-500">
                {actionFilter !== 'all'
                  ? `No ${actionFilter.replace('_', ' ')} mentions. Try a different filter.`
                  : 'Try adjusting the time window or filters.'}
              </p>
            </div>
          </div>
        )}

        {!loading && error == null && visible.length > 0 && (
          <div className="space-y-2">
            {visible.map((group, i) => (
              <div key={mentionKey(group)} ref={i === selectedIdx ? selectedRef : null}>
                <MentionCard
                  group={group}
                  onNavigate={openInChat}
                  selected={i === selectedIdx}
                  onToggleExpand={() => setSelectedIdx(i)}
                />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
