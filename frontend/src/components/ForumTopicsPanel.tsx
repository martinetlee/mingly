import { useEffect, useState, useCallback } from 'react'
import { MessageSquare, Plus, Lock, Unlock, Pin, X } from 'lucide-react'
import type { ForumTopic, Message } from '../api/types'
import { getForumTopics, createForumTopic, setTopicState, getTopicMessages } from '../api/client'

interface ForumTopicsPanelProps {
  telegramId: number
  onSelectTopic: (topicId: number | null, topicTitle: string | null) => void
  selectedTopicId: number | null
}

export default function ForumTopicsPanel({ telegramId, onSelectTopic, selectedTopicId }: ForumTopicsPanelProps) {
  const [topics, setTopics] = useState<ForumTopic[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [newTitle, setNewTitle] = useState('')
  const [creating, setCreating] = useState(false)

  const loadTopics = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await getForumTopics(telegramId)
      setTopics(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load topics')
    } finally {
      setLoading(false)
    }
  }, [telegramId])

  useEffect(() => {
    loadTopics()
  }, [loadTopics])

  const handleCreate = async () => {
    if (!newTitle.trim() || creating) return
    setCreating(true)
    try {
      await createForumTopic(telegramId, newTitle.trim())
      setNewTitle('')
      setShowCreate(false)
      await loadTopics()
    } catch {
      // ignore
    } finally {
      setCreating(false)
    }
  }

  const handleToggleState = async (topic: ForumTopic) => {
    try {
      await setTopicState(telegramId, topic.id, !topic.is_closed)
      setTopics((prev) =>
        prev.map((t) => t.id === topic.id ? { ...t, is_closed: !t.is_closed } : t)
      )
    } catch {
      // ignore
    }
  }

  if (loading) {
    return (
      <div className="border-b border-slate-700/50 px-3 py-2">
        <p className="text-xs text-slate-500">Loading topics...</p>
      </div>
    )
  }

  if (error) {
    return (
      <div className="border-b border-slate-700/50 px-3 py-2">
        <p className="text-xs text-slate-500">{error}</p>
      </div>
    )
  }

  return (
    <div className="border-b border-slate-700/50 flex flex-col">
      {/* Topic header */}
      <div className="flex items-center justify-between px-3 py-2 bg-slate-800/50">
        <div className="flex items-center gap-1.5 text-xs font-medium text-slate-400 uppercase tracking-wider">
          <MessageSquare size={11} />
          Topics ({topics.length})
        </div>
        <div className="flex items-center gap-1">
          {selectedTopicId !== null && (
            <button
              onClick={() => onSelectTopic(null, null)}
              title="Show all messages"
              className="rounded p-1 text-slate-500 hover:text-slate-300 transition-colors"
            >
              <X size={12} />
            </button>
          )}
          <button
            onClick={() => setShowCreate((v) => !v)}
            title="Create topic"
            className="rounded p-1 text-slate-500 hover:text-indigo-400 transition-colors"
          >
            <Plus size={12} />
          </button>
        </div>
      </div>

      {/* Create form */}
      {showCreate && (
        <div className="flex items-center gap-2 px-3 py-2 border-t border-slate-700/30">
          <input
            autoFocus
            type="text"
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') handleCreate(); if (e.key === 'Escape') setShowCreate(false) }}
            placeholder="Topic title..."
            className="flex-1 rounded bg-slate-700 border border-slate-600 px-2 py-1 text-xs text-slate-200 outline-none focus:border-indigo-500"
          />
          <button
            onClick={handleCreate}
            disabled={!newTitle.trim() || creating}
            className="rounded bg-indigo-500 px-2 py-1 text-xs font-medium text-white hover:bg-indigo-600 disabled:opacity-40 transition-colors"
          >
            {creating ? '...' : 'Create'}
          </button>
        </div>
      )}

      {/* Topic list */}
      <div className="max-h-40 overflow-y-auto">
        {topics.map((topic) => (
          <div
            key={topic.id}
            className={`group flex items-center gap-2 px-3 py-1.5 cursor-pointer transition-colors ${
              selectedTopicId === topic.id
                ? 'bg-indigo-500/15'
                : 'hover:bg-slate-700/50'
            }`}
          >
            <button
              onClick={() => onSelectTopic(topic.id, topic.title)}
              className="flex-1 flex items-center gap-2 min-w-0 text-left"
            >
              <span
                className="flex h-5 w-5 flex-shrink-0 items-center justify-center rounded text-[10px]"
                style={{ backgroundColor: topic.icon_color ? `#${topic.icon_color.toString(16).padStart(6, '0')}` : undefined }}
              >
                <MessageSquare size={10} className="text-slate-300" />
              </span>
              <span className={`truncate text-xs ${
                selectedTopicId === topic.id ? 'text-indigo-300 font-medium' : 'text-slate-300'
              }`}>
                {topic.title}
              </span>
              {topic.is_pinned && (
                <Pin size={9} className="text-amber-400 flex-shrink-0" />
              )}
              {topic.is_closed && (
                <Lock size={9} className="text-rose-400 flex-shrink-0" />
              )}
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); handleToggleState(topic) }}
              title={topic.is_closed ? 'Reopen topic' : 'Close topic'}
              className="rounded p-0.5 text-slate-600 opacity-0 group-hover:opacity-100 hover:text-slate-300 transition-all"
            >
              {topic.is_closed ? <Unlock size={10} /> : <Lock size={10} />}
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}
