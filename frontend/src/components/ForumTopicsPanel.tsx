import { useState } from 'react'
import { MessageSquare, Plus, Lock, Unlock, Pin, X, RotateCcw, Loader2 } from 'lucide-react'
import type { ForumTopic } from '../api/types'
import { getForumTopics, createForumTopic, setTopicState } from '../api/client'
import { useLatestQuery } from '../hooks/useLatestQuery'
import { useAsyncAction } from '../hooks/useAsyncAction'
import { reportError, errorMessage } from '../lib/toast'

interface ForumTopicsPanelProps {
  telegramId: number
  /** null = all messages */
  onSelectTopic: (topic: { id: number; title: string } | null) => void
  selectedTopicId: number | null
}

/** Telegram's General topic (id 1) isn't always returned by the topic list */
export const GENERAL_TOPIC: ForumTopic = {
  id: 1, title: 'General', icon_color: null, icon_emoji_id: null, date: null, is_closed: false, is_pinned: true,
}

export function withGeneral(topics: ForumTopic[]): ForumTopic[] {
  return topics.some((t) => t.id === 1) ? topics : [GENERAL_TOPIC, ...topics]
}

export default function ForumTopicsPanel({ telegramId, onSelectTopic, selectedTopicId }: ForumTopicsPanelProps) {
  // Keyed by chat: a slow list for the previous forum can never render here
  const { data, loading, error, refetch, setData } = useLatestQuery(['topics', telegramId], () => getForumTopics(telegramId))
  const topics = withGeneral(data ?? [])
  const [showCreate, setShowCreate] = useState(false)
  const [newTitle, setNewTitle] = useState('')

  const create = useAsyncAction(async (title: string) => {
    await createForumTopic(telegramId, title)
    setNewTitle('')
    setShowCreate(false)
    await refetch()
  }, { errorContext: 'Couldn’t create topic' })

  const handleToggleState = async (topic: ForumTopic) => {
    try {
      await setTopicState(telegramId, topic.id, !topic.is_closed)
      setData((prev) => prev?.map((t) => (t.id === topic.id ? { ...t, is_closed: !t.is_closed } : t)))
    } catch (e) {
      reportError(e, topic.is_closed ? 'Couldn’t reopen topic' : 'Couldn’t close topic')
    }
  }

  if (loading) {
    return (
      <div className="border-b border-slate-700/50 px-3 py-2">
        <p className="text-xs text-slate-500">Loading topics…</p>
      </div>
    )
  }

  return (
    <div className="border-b border-slate-700/50 flex flex-col">
      <div className="flex items-center justify-between px-3 py-2 bg-slate-800/50">
        <div className="flex items-center gap-1.5 text-xs font-medium text-slate-400 uppercase tracking-wider">
          <MessageSquare size={11} />
          Topics ({topics.length})
        </div>
        <div className="flex items-center gap-1">
          {selectedTopicId !== null && (
            <button onClick={() => onSelectTopic(null)} title="Show all messages" className="rounded p-1 text-slate-500 hover:text-slate-300 transition-colors">
              <X size={12} />
            </button>
          )}
          <button onClick={() => setShowCreate((v) => !v)} title="Create topic" className="rounded p-1 text-slate-500 hover:text-indigo-400 transition-colors">
            <Plus size={12} />
          </button>
        </div>
      </div>

      {error != null && (
        <div className="flex items-center gap-2 px-3 py-1.5 text-[11px] text-orange-300">
          <span className="flex-1 truncate">Topics unavailable: {errorMessage(error)}</span>
          <button onClick={() => void refetch()} className="flex items-center gap-1 underline"><RotateCcw size={10} /> Retry</button>
        </div>
      )}

      {showCreate && (
        <div className="flex items-center gap-2 px-3 py-2 border-t border-slate-700/30">
          <input
            autoFocus
            type="text"
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && newTitle.trim()) void create.run(newTitle.trim())
              if (e.key === 'Escape') setShowCreate(false)
            }}
            placeholder="Topic title…"
            className="flex-1 rounded bg-slate-700 border border-slate-600 px-2 py-1 text-xs text-slate-200 outline-none focus:border-indigo-500"
          />
          <button
            onClick={() => void create.run(newTitle.trim())}
            disabled={!newTitle.trim() || create.pending}
            className="rounded bg-indigo-500 px-2 py-1 text-xs font-medium text-white hover:bg-indigo-600 disabled:opacity-40 transition-colors"
          >
            {create.pending ? <Loader2 size={12} className="animate-spin" /> : 'Create'}
          </button>
        </div>
      )}

      <div className="max-h-40 overflow-y-auto">
        {topics.map((topic) => (
          <div
            key={topic.id}
            className={`group flex items-center gap-2 px-3 py-1.5 transition-colors ${
              selectedTopicId === topic.id ? 'bg-indigo-500/15' : 'hover:bg-slate-700/50'
            }`}
          >
            <button onClick={() => onSelectTopic({ id: topic.id, title: topic.title })} className="flex-1 flex items-center gap-2 min-w-0 text-left">
              <span
                className="flex h-5 w-5 flex-shrink-0 items-center justify-center rounded text-[10px]"
                style={{ backgroundColor: topic.icon_color ? `#${topic.icon_color.toString(16).padStart(6, '0')}` : undefined }}
              >
                <MessageSquare size={10} className="text-slate-300" />
              </span>
              <span className={`truncate text-xs ${selectedTopicId === topic.id ? 'text-indigo-300 font-medium' : 'text-slate-300'}`}>
                {topic.title}
              </span>
              {topic.is_pinned && <Pin size={9} className="text-amber-400 flex-shrink-0" />}
              {topic.is_closed && <Lock size={9} className="text-rose-400 flex-shrink-0" />}
            </button>
            {topic.id !== 1 && (
              <button
                onClick={(e) => { e.stopPropagation(); void handleToggleState(topic) }}
                title={topic.is_closed ? 'Reopen topic' : 'Close topic'}
                className="rounded p-0.5 text-slate-600 opacity-0 group-hover:opacity-100 focus:opacity-100 hover:text-slate-300 transition-all"
              >
                {topic.is_closed ? <Unlock size={10} /> : <Lock size={10} />}
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
