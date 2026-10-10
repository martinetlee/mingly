import { useEffect, useState } from 'react'
import { X, Sparkles, Plus, Tag as TagIcon } from 'lucide-react'
import type { Conversation, Tag, TagSuggestion } from '../../../api/types'
import { getTags, getConversationTags, addTagToConversation, removeTagFromConversation, suggestTags } from '../../../api/client'
import { reportError, toast } from '../../../lib/toast'
import { createOrFindTag, tagErrorMessage } from '../chatTags'

const TAG_COLORS: Record<string, string> = {
  indigo:  'bg-indigo-500/20 text-indigo-300 border border-indigo-500/30',
  emerald: 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30',
  rose:    'bg-rose-500/20 text-rose-300 border border-rose-500/30',
  amber:   'bg-amber-500/20 text-amber-300 border border-amber-500/30',
  violet:  'bg-violet-500/20 text-violet-300 border border-violet-500/30',
  sky:     'bg-sky-500/20 text-sky-300 border border-sky-500/30',
  slate:   'bg-slate-500/20 text-slate-300 border border-slate-500/30',
}

const COLOR_OPTIONS = ['indigo', 'emerald', 'rose', 'amber', 'violet', 'sky', 'slate'] as const

const COLOR_DOTS: Record<string, string> = {
  indigo: 'bg-indigo-400', emerald: 'bg-emerald-400', rose: 'bg-rose-400',
  amber: 'bg-amber-400', violet: 'bg-violet-400', sky: 'bg-sky-400', slate: 'bg-slate-400',
}

/** The conversation's tags (grouped by category), AI suggestions, adding existing / new tags */
export default function TagsSection({ chat }: { chat: Conversation }) {
  const [allTags, setAllTags] = useState<Tag[]>([])
  const [chatTags, setChatTags] = useState<Tag[]>([])
  const [suggestions, setSuggestions] = useState<TagSuggestion[]>([])
  const [suggesting, setSuggesting] = useState(false)
  const [showAddDropdown, setShowAddDropdown] = useState(false)
  const [showNewTag, setShowNewTag] = useState(false)
  const [newTagName, setNewTagName] = useState('')
  const [newTagColor, setNewTagColor] = useState<string>('slate')

  useEffect(() => {
    getTags().then(setAllTags).catch((e) => reportError(e, 'Couldn’t load tags'))
    getConversationTags(chat.id).then(setChatTags).catch((e) => reportError(e, 'Couldn’t load chat tags'))
    setSuggestions([])
  }, [chat.id])

  const assignedIds = new Set(chatTags.map((t) => t.id))
  // Stage/stream tags are managed from kanban boards, not the general picker
  const unassignedTags = allTags.filter(
    (t) => !assignedIds.has(t.id) && t.category !== 'stage' && t.category !== 'stream',
  )

  /** Resolves true when the tag is on the chat; failures are toasted */
  const handleAdd = async (tag: Tag): Promise<boolean> => {
    if (assignedIds.has(tag.id)) return true
    try {
      await addTagToConversation(chat.id, tag.id)
    } catch (e) {
      toast.error(`Couldn’t add tag: ${tagErrorMessage(e, tag.name)}`)
      return false
    }
    setChatTags((prev) => (prev.some((t) => t.id === tag.id) ? prev : [...prev, tag]))
    setShowAddDropdown(false)
    setSuggestions((prev) => prev.filter((s) => s.name !== tag.name))
    return true
  }

  /** Create (or reuse an existing same-named) tag and put it on the chat */
  const createAndAdd = async (name: string, color: string): Promise<boolean> => {
    let tag: Tag
    try {
      const res = await createOrFindTag(name, color, allTags)
      tag = res.tag
      if (res.created) setAllTags((prev) => [...prev, res.tag])
    } catch (e) {
      toast.error(`Couldn’t create tag “${name}”: ${tagErrorMessage(e, name)}`)
      return false
    }
    return handleAdd(tag)
  }

  const handleRemove = async (tag: Tag) => {
    try {
      await removeTagFromConversation(chat.id, tag.id)
      setChatTags((prev) => prev.filter((t) => t.id !== tag.id))
    } catch (e) {
      reportError(e, 'Remove tag failed')
    }
  }

  const handleCreateAndAdd = async () => {
    const name = newTagName.trim()
    if (!name) return
    // On failure the form stays open with the name, so the user can fix and retry
    if (await createAndAdd(name, newTagColor)) {
      setNewTagName('')
      setNewTagColor('slate')
      setShowNewTag(false)
    }
  }

  const handleSuggest = async () => {
    setSuggesting(true)
    try {
      const results = await suggestTags(chat.id)
      setSuggestions(results)
    } catch (e) {
      reportError(e, 'Tag suggestions failed')
    } finally {
      setSuggesting(false)
    }
  }

  const handleAcceptSuggestion = async (s: TagSuggestion) => {
    // The suggestion stays (so it can be retried) unless the tag really landed on the chat
    if (await createAndAdd(s.name, s.color)) {
      setSuggestions((prev) => prev.filter((x) => x.name !== s.name))
    }
  }

  return (
    <div className="px-4 py-4 flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5 text-xs font-medium text-slate-400 uppercase tracking-wider">
          <TagIcon size={11} />
          Tags
        </div>
        <button
          onClick={handleSuggest}
          disabled={suggesting}
          title="Get AI tag suggestions"
          className="flex items-center gap-1 rounded px-2 py-1 text-[11px] text-violet-400 hover:bg-violet-500/10 disabled:opacity-50 transition-colors"
        >
          <Sparkles size={11} />
          {suggesting ? 'Thinking...' : 'Suggest'}
        </button>
      </div>

      {/* Assigned tags grouped by category */}
      {chatTags.length === 0 ? (
        <p className="text-xs text-slate-500">No tags yet</p>
      ) : (
        <div className="flex flex-col gap-2.5">
          {(() => {
            const grouped = chatTags.reduce<Record<string, Tag[]>>((acc, tag) => {
              const cat = tag.category?.trim() || 'Uncategorized'
              ;(acc[cat] = acc[cat] || []).push(tag)
              return acc
            }, {})
            const cats = Object.keys(grouped).sort((a, b) => {
              if (a === 'Uncategorized') return -1
              if (b === 'Uncategorized') return 1
              return a.localeCompare(b)
            })
            return cats.map((cat) => (
              <div key={cat}>
                <p className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-slate-600">{cat}</p>
                <div className="flex flex-wrap gap-1.5">
                  {grouped[cat].map((tag) => (
                    <span
                      key={tag.id}
                      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${TAG_COLORS[tag.color] ?? TAG_COLORS.slate}`}
                    >
                      {tag.name}
                      <button
                        onClick={() => handleRemove(tag)}
                        className="opacity-60 hover:opacity-100 transition-opacity"
                      >
                        <X size={10} />
                      </button>
                    </span>
                  ))}
                </div>
              </div>
            ))
          })()}
        </div>
      )}

      {/* AI Suggestions */}
      {suggestions.length > 0 && (
        <div className="rounded-lg bg-violet-500/5 border border-violet-500/20 p-2.5 flex flex-col gap-2">
          <p className="text-[10px] font-medium text-violet-400 uppercase tracking-wider">AI Suggestions</p>
          <div className="flex flex-wrap gap-1.5">
            {suggestions.map((s) => (
              <button
                key={s.name}
                onClick={() => handleAcceptSuggestion(s)}
                title={s.reason}
                className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium border border-dashed hover:border-solid transition-all ${TAG_COLORS[s.color] ?? TAG_COLORS.slate}`}
              >
                <Plus size={9} />
                {s.name}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Add existing tag */}
      <div className="relative">
        <button
          onClick={() => { setShowAddDropdown((v) => !v); setShowNewTag(false) }}
          className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200 transition-colors"
        >
          <Plus size={12} />
          Add tag
        </button>
        {showAddDropdown && (
          <div className="absolute left-0 top-6 z-20 w-48 rounded-lg border border-slate-600 bg-slate-800 shadow-xl overflow-hidden">
            {unassignedTags.length === 0 ? (
              <p className="px-3 py-2 text-xs text-slate-500">All tags assigned</p>
            ) : (
              <div className="max-h-48 overflow-y-auto py-1">
                {(() => {
                  const grouped = unassignedTags.reduce<Record<string, Tag[]>>((acc, tag) => {
                    const cat = tag.category?.trim() || 'Uncategorized'
                    ;(acc[cat] = acc[cat] || []).push(tag)
                    return acc
                  }, {})
                  const cats = Object.keys(grouped).sort((a, b) => {
                    if (a === 'Uncategorized') return -1
                    if (b === 'Uncategorized') return 1
                    return a.localeCompare(b)
                  })
                  return cats.map((cat, i) => (
                    <div key={cat}>
                      {(cats.length > 1 || cat !== 'Uncategorized') && (
                        <p className={`px-3 py-1 text-[10px] font-semibold uppercase tracking-wider text-slate-500 ${i > 0 ? 'border-t border-slate-700 mt-1 pt-1.5' : ''}`}>{cat}</p>
                      )}
                      {grouped[cat].map((tag) => (
                        <button
                          key={tag.id}
                          onClick={() => handleAdd(tag)}
                          className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-slate-300 hover:bg-slate-700 transition-colors"
                        >
                          <span className={`h-2 w-2 rounded-full ${COLOR_DOTS[tag.color] ?? 'bg-slate-400'}`} />
                          {tag.name}
                        </button>
                      ))}
                    </div>
                  ))
                })()}
              </div>
            )}
            <div className="border-t border-slate-700 px-3 py-1.5">
              <button
                onClick={() => { setShowNewTag(true); setShowAddDropdown(false) }}
                className="flex items-center gap-1.5 text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
              >
                <Plus size={11} />
                Create new tag
              </button>
            </div>
          </div>
        )}
      </div>

      {/* New tag form */}
      {showNewTag && (
        <div className="rounded-lg border border-slate-600 bg-slate-700/50 p-3 flex flex-col gap-2">
          <input
            autoFocus
            value={newTagName}
            onChange={(e) => setNewTagName(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') handleCreateAndAdd(); if (e.key === 'Escape') setShowNewTag(false) }}
            placeholder="Tag name..."
            className="w-full rounded bg-slate-700 border border-slate-600 px-2 py-1 text-xs text-slate-200 outline-none focus:border-indigo-500"
          />
          <div className="flex gap-1.5">
            {COLOR_OPTIONS.map((c) => (
              <button
                key={c}
                onClick={() => setNewTagColor(c)}
                className={`h-4 w-4 rounded-full ${COLOR_DOTS[c]} transition-transform ${newTagColor === c ? 'scale-125 ring-2 ring-white/40' : 'opacity-60 hover:opacity-100'}`}
              />
            ))}
          </div>
          <div className="flex gap-2">
            <button
              onClick={handleCreateAndAdd}
              disabled={!newTagName.trim()}
              className="flex-1 rounded bg-indigo-500 px-2 py-1 text-xs font-medium text-white hover:bg-indigo-600 disabled:opacity-40 transition-colors"
            >
              Create & Add
            </button>
            <button
              onClick={() => setShowNewTag(false)}
              className="rounded px-2 py-1 text-xs text-slate-400 hover:text-slate-200 transition-colors"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
