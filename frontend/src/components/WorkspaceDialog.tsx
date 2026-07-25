import { useEffect, useMemo, useState } from 'react'
import { X } from 'lucide-react'
import type { Tag, Workspace } from '../api/types'
import { addBoardColumn, createWorkspace, deleteWorkspace, getTags, updateWorkspace } from '../api/client'

const COLOR_OPTIONS = ['indigo', 'emerald', 'rose', 'amber', 'violet', 'sky', 'slate'] as const
const COLOR_DOTS: Record<string, string> = {
  indigo: 'bg-indigo-400', emerald: 'bg-emerald-400', rose: 'bg-rose-400',
  amber: 'bg-amber-400', violet: 'bg-violet-400', sky: 'bg-sky-400', slate: 'bg-slate-400',
}

const TEMPLATES: { key: string; label: string; columns: string[] }[] = [
  { key: 'pipeline', label: 'Pipeline', columns: ['New', 'Contacted', 'Negotiating', 'Confirmed', 'Done'] },
  { key: 'simple', label: 'Simple', columns: ['Todo', 'Doing', 'Done'] },
  { key: 'empty', label: 'Start empty', columns: [] },
]

export default function WorkspaceDialog({
  open,
  onClose,
  onSaved,
  workspace,
}: {
  open: boolean
  onClose: () => void
  onSaved: (id: number | null, viewMode?: 'list' | 'kanban') => void
  workspace?: Workspace | null
}) {
  const [name, setName] = useState('')
  const [emoji, setEmoji] = useState('')
  const [color, setColor] = useState<string>('indigo')
  const [viewMode, setViewMode] = useState<'list' | 'kanban'>('list')
  const [template, setTemplate] = useState('pipeline')
  const [selectedTagIds, setSelectedTagIds] = useState<number[]>([])
  const [tagQuery, setTagQuery] = useState('')
  const [allTags, setAllTags] = useState<Tag[]>([])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirmingDelete, setConfirmingDelete] = useState(false)

  const editing = workspace != null

  useEffect(() => {
    if (!open) return
    setName(workspace?.name ?? '')
    setEmoji(workspace?.emoji ?? '')
    setColor(workspace?.color ?? 'indigo')
    setViewMode(workspace?.view_mode ?? 'list')
    setTemplate('pipeline')
    setSelectedTagIds(workspace?.tag_ids ?? [])
    setTagQuery('')
    setError(null)
    setConfirmingDelete(false)
    getTags().then(setAllTags).catch(() => {})
  }, [open, workspace])

  const selectableTags = useMemo(() => {
    const q = tagQuery.trim().toLowerCase()
    return allTags
      .filter((t) => t.category !== 'stage' && t.category !== 'stream' && !selectedTagIds.includes(t.id))
      .filter((t) => (q ? t.name.toLowerCase().includes(q) : (t.chat_count ?? 0) > 0))
      .slice(0, 12)
  }, [allTags, selectedTagIds, tagQuery])

  const selectedTags = allTags.filter((t) => selectedTagIds.includes(t.id))

  if (!open) return null

  const save = async () => {
    const trimmed = name.trim()
    if (!trimmed) {
      setError('Name is required')
      return
    }
    if (selectedTagIds.length === 0) {
      setError('Pick at least one tag — it defines which chats belong here')
      return
    }
    setSaving(true)
    setError(null)
    try {
      if (editing && workspace) {
        await updateWorkspace(workspace.id, {
          name: trimmed, emoji: emoji || null, color, tag_ids: selectedTagIds, view_mode: viewMode,
        })
        onSaved(workspace.id, viewMode)
      } else {
        const ws = await createWorkspace({
          name: trimmed, emoji: emoji || null, color, tag_ids: selectedTagIds, view_mode: viewMode,
        })
        if (viewMode === 'kanban' && ws.main_board_id != null) {
          const cols = TEMPLATES.find((t) => t.key === template)?.columns ?? []
          for (const col of cols) {
            await addBoardColumn(ws.id, ws.main_board_id, col).catch(() => {})
          }
        }
        onSaved(ws.id, viewMode)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save workspace')
    } finally {
      setSaving(false)
    }
  }

  const remove = async () => {
    if (!workspace) return
    setSaving(true)
    try {
      await deleteWorkspace(workspace.id)
      onSaved(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to delete workspace')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div
        className="w-full max-w-lg rounded-xl border border-slate-700 bg-slate-800 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-slate-700/50 px-4 py-3">
          <h2 className="text-sm font-semibold text-gray-200">
            {editing ? 'Edit workspace' : 'New workspace'}
          </h2>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-300">
            <X size={16} />
          </button>
        </div>

        <div className="flex flex-col gap-4 px-4 py-4">
          <div className="flex gap-2">
            <input
              type="text"
              placeholder="🏷"
              value={emoji}
              onChange={(e) => setEmoji(e.target.value)}
              maxLength={4}
              className="w-14 rounded-lg border border-slate-600 bg-slate-700 px-2 py-1.5 text-center text-sm text-gray-200 outline-none focus:border-indigo-500"
              title="Emoji (optional)"
            />
            <input
              type="text"
              autoFocus
              placeholder="Workspace name (e.g. ETHTaipei)"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="min-w-0 flex-1 rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-sm text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500"
            />
            <div className="flex items-center gap-1">
              {COLOR_OPTIONS.map((c) => (
                <button
                  key={c}
                  onClick={() => setColor(c)}
                  title={c}
                  className={`h-4 w-4 rounded-full ${COLOR_DOTS[c]} ${color === c ? 'ring-2 ring-white/70' : 'opacity-50 hover:opacity-100'}`}
                />
              ))}
            </div>
          </div>

          {/* Tag rules */}
          <div>
            <p className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-slate-500">
              Chats with any of these tags
            </p>
            {selectedTags.length > 0 && (
              <div className="mb-2 flex flex-wrap gap-1.5">
                {selectedTags.map((t) => (
                  <span
                    key={t.id}
                    className="inline-flex items-center gap-1 rounded-full bg-indigo-500/20 px-2.5 py-1 text-xs text-indigo-300"
                  >
                    <span className={`h-2 w-2 rounded-full ${COLOR_DOTS[t.color] ?? 'bg-slate-400'}`} />
                    {t.name}
                    <button onClick={() => setSelectedTagIds((prev) => prev.filter((id) => id !== t.id))}>
                      <X size={11} />
                    </button>
                  </span>
                ))}
              </div>
            )}
            <input
              type="text"
              placeholder="Search tags…"
              value={tagQuery}
              onChange={(e) => setTagQuery(e.target.value)}
              className="mb-1.5 w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-xs text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500"
            />
            <div className="flex max-h-28 flex-wrap gap-1.5 overflow-y-auto">
              {selectableTags.map((t) => (
                <button
                  key={t.id}
                  onClick={() => setSelectedTagIds((prev) => [...prev, t.id])}
                  className="inline-flex items-center gap-1 rounded-full border border-slate-600 px-2.5 py-1 text-xs text-slate-300 hover:border-indigo-500/50 hover:bg-indigo-500/10"
                >
                  <span className={`h-2 w-2 rounded-full ${COLOR_DOTS[t.color] ?? 'bg-slate-400'}`} />
                  {t.name}
                  <span className="text-slate-500">{t.chat_count ?? 0}</span>
                </button>
              ))}
            </div>
          </div>

          {/* View mode */}
          <div>
            <p className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-slate-500">View</p>
            <div className="flex rounded-lg border border-slate-700 overflow-hidden text-xs">
              {(['list', 'kanban'] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => setViewMode(m)}
                  className={`flex-1 py-1.5 transition-colors ${
                    viewMode === m
                      ? 'bg-indigo-500/20 font-medium text-indigo-400'
                      : 'text-slate-500 hover:bg-slate-700/50 hover:text-slate-300'
                  }`}
                >
                  {m === 'list' ? 'List' : 'Kanban board'}
                </button>
              ))}
            </div>
            {viewMode === 'kanban' && !editing && (
              <div className="mt-2 flex gap-1.5">
                {TEMPLATES.map((t) => (
                  <button
                    key={t.key}
                    onClick={() => setTemplate(t.key)}
                    title={t.columns.join(' → ') || 'No columns'}
                    className={`rounded-md border px-2.5 py-1 text-[11px] transition-colors ${
                      template === t.key
                        ? 'border-indigo-500/50 bg-indigo-500/15 text-indigo-300'
                        : 'border-slate-700 text-slate-400 hover:border-slate-500'
                    }`}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
            )}
          </div>

          {error && <p className="text-xs text-rose-400">{error}</p>}
        </div>

        <div className="flex items-center justify-between border-t border-slate-700/50 px-4 py-3">
          <div>
            {editing && (
              confirmingDelete ? (
                <span className="flex items-center gap-2 text-xs">
                  <span className="text-slate-400">Delete workspace? Chats and tags are kept.</span>
                  <button onClick={remove} className="font-medium text-rose-400 hover:text-rose-300">Delete</button>
                  <button onClick={() => setConfirmingDelete(false)} className="text-slate-500 hover:text-slate-300">Cancel</button>
                </span>
              ) : (
                <button onClick={() => setConfirmingDelete(true)} className="text-xs text-rose-400/80 hover:text-rose-400">
                  Delete workspace
                </button>
              )
            )}
          </div>
          <div className="flex gap-2">
            <button
              onClick={onClose}
              className="rounded-lg px-3 py-1.5 text-xs text-slate-400 hover:bg-slate-700/50"
            >
              Cancel
            </button>
            <button
              onClick={save}
              disabled={saving}
              className="rounded-lg bg-indigo-500 px-4 py-1.5 text-xs font-medium text-white hover:bg-indigo-400 disabled:opacity-50"
            >
              {saving ? 'Saving…' : editing ? 'Save' : 'Create'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
