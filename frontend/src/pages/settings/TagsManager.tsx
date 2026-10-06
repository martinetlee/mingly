import { useEffect, useState } from 'react'
import { ApiError, createTag, deleteTag, getTags, updateTag } from '../../api/client'
import type { Tag } from '../../api/types'
import { useConfirm } from '../../components/ui'
import { useAsyncAction } from '../../hooks/useAsyncAction'
import { errorMessage, reportError } from '../../lib/toast'

// ---------------------------------------------------------------------------
// Tag colors
// ---------------------------------------------------------------------------

const TAG_COLORS: Record<string, { chip: string; dot: string }> = {
  indigo:  { chip: 'bg-indigo-500/20 text-indigo-300 border-indigo-500/30',  dot: 'bg-indigo-400' },
  emerald: { chip: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30', dot: 'bg-emerald-400' },
  rose:    { chip: 'bg-rose-500/20 text-rose-300 border-rose-500/30',    dot: 'bg-rose-400' },
  amber:   { chip: 'bg-amber-500/20 text-amber-300 border-amber-500/30',   dot: 'bg-amber-400' },
  violet:  { chip: 'bg-violet-500/20 text-violet-300 border-violet-500/30',  dot: 'bg-violet-400' },
  sky:     { chip: 'bg-sky-500/20 text-sky-300 border-sky-500/30',      dot: 'bg-sky-400' },
  slate:   { chip: 'bg-slate-500/20 text-slate-300 border-slate-500/30',   dot: 'bg-slate-400' },
}
const COLOR_OPTIONS = Object.keys(TAG_COLORS)

// Board-column tags are internal to kanban boards: renaming or deleting one
// would silently break a column, so the manager never shows them.
export const STAGE_CATEGORY = 'stage'
export const isStageTag = (tag: Pick<Tag, 'category'>) => tag.category?.trim().toLowerCase() === STAGE_CATEGORY
const userTags = (tags: Tag[]) => tags.filter((t) => !isStageTag(t))

function validateCategory(category: string): string | null {
  return category.trim().toLowerCase() === STAGE_CATEGORY ? '"stage" is reserved for board columns' : null
}

// ---------------------------------------------------------------------------
// TagsManager component
// ---------------------------------------------------------------------------

export default function TagsManager() {
  const confirm = useConfirm()
  const [tags, setTags] = useState<Tag[]>([])
  const [loading, setLoading] = useState(true)

  // Create form
  const [newName, setNewName] = useState('')
  const [newColor, setNewColor] = useState('slate')
  const [newCategory, setNewCategory] = useState('')

  // Inline edit
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editName, setEditName] = useState('')
  const [editColor, setEditColor] = useState('slate')
  const [editCategory, setEditCategory] = useState('')

  const reload = () =>
    getTags()
      .then((all) => setTags(userTags(all)))
      .catch((e) => reportError(e, 'Failed to load tags'))

  useEffect(() => {
    reload().finally(() => setLoading(false))
  }, [])

  // A 409 means the server's view differs from ours (duplicate name, or a tag
  // that is now board-owned): resync the list, then surface the server's message
  const resyncOnConflict = (e: unknown) => {
    if (e instanceof ApiError && e.status === 409) void reload()
  }

  // Group tags by category
  const grouped = tags.reduce<Record<string, Tag[]>>((acc, tag) => {
    const cat = tag.category?.trim() || 'Uncategorized'
    ;(acc[cat] = acc[cat] || []).push(tag)
    return acc
  }, {})
  // Sort so Uncategorized goes last
  const categories = Object.keys(grouped).sort((a, b) => {
    if (a === 'Uncategorized') return 1
    if (b === 'Uncategorized') return -1
    return a.localeCompare(b)
  })

  const startEdit = (tag: Tag) => {
    setEditingId(tag.id)
    setEditName(tag.name)
    setEditColor(tag.color)
    setEditCategory(tag.category || '')
  }

  const cancelEdit = () => setEditingId(null)

  const save = useAsyncAction(async (tagId: number) => {
    const updated = await updateTag(tagId, {
      name: editName.trim(),
      color: editColor,
      category: editCategory.trim() || null,
    })
    setTags((prev) => userTags(prev.map((t) => (t.id === updated.id ? updated : t))))
    setEditingId(null)
  }, { errorContext: "Couldn't save tag", onError: resyncOnConflict })
  const saving = save.pending
  const editCategoryError = validateCategory(editCategory)
  const saveEdit = (tagId: number) => {
    if (!editName.trim() || editCategoryError) return
    void save.run(tagId)
  }

  const handleDelete = (tag: Tag) => {
    void confirm({
      title: `Delete tag "${tag.name}"?`,
      message: 'It will be removed from all chats.',
      destructive: true,
      onConfirm: async () => {
        try {
          await deleteTag(tag.id)
        } catch (e) {
          resyncOnConflict(e)
          throw e  // shown inline in the dialog
        }
        setTags((prev) => prev.filter((t) => t.id !== tag.id))
      },
    })
  }

  const create = useAsyncAction(async () => {
    const tag = await createTag(newName.trim(), newColor, newCategory.trim() || undefined)
    setTags((prev) => userTags([...prev, tag]))
    setNewName('')
    setNewColor('slate')
    setNewCategory('')
  })
  const creating = create.pending
  const newCategoryError = validateCategory(newCategory)
  const createError = newCategoryError || (create.error ? errorMessage(create.error) : '')
  const handleCreate = () => {
    if (!newName.trim() || newCategoryError) return
    void create.run()
  }

  if (loading) {
    return <p className="mt-6 text-sm text-slate-400">Loading tags…</p>
  }

  return (
    <div className="max-w-2xl space-y-6">

      {/* ── Create new tag ── */}
      <div className="rounded-xl border border-slate-700 bg-slate-800 p-4">
        <h3 className="mb-3 text-sm font-medium text-slate-300">New Tag</h3>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <div className="flex-1">
            <label className="mb-1 block text-xs text-slate-400">Name</label>
            <input
              value={newName}
              onChange={(e) => { setNewName(e.target.value); create.reset() }}
              onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
              placeholder="Tag name…"
              className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-sm text-slate-100 placeholder-slate-500 outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
            />
          </div>
          <div className="flex-1">
            <label className="mb-1 block text-xs text-slate-400">Category (optional)</label>
            <input
              value={newCategory}
              onChange={(e) => { setNewCategory(e.target.value); create.reset() }}
              placeholder="e.g. Work, Personal…"
              className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-sm text-slate-100 placeholder-slate-500 outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs text-slate-400">Color</label>
            <div className="flex gap-1.5">
              {COLOR_OPTIONS.map((c) => (
                <button
                  key={c}
                  onClick={() => setNewColor(c)}
                  className={`h-6 w-6 rounded-full ${TAG_COLORS[c].dot} ring-offset-slate-800 ring-offset-2 transition ${newColor === c ? 'ring-2 ring-blue-400' : ''}`}
                  title={c}
                />
              ))}
            </div>
          </div>
          <button
            onClick={handleCreate}
            disabled={creating || !newName.trim() || !!newCategoryError}
            className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
          >
            {creating ? 'Creating…' : 'Create'}
          </button>
        </div>
        {createError && <p className="mt-2 text-xs text-red-400">{createError}</p>}
      </div>

      {/* ── Tag list grouped by category ── */}
      {tags.length === 0 ? (
        <p className="text-sm text-slate-400">No tags yet.</p>
      ) : (
        categories.map((cat) => (
          <div key={cat} className="rounded-xl border border-slate-700 bg-slate-800">
            <div className="border-b border-slate-700 px-4 py-2.5">
              <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">{cat}</span>
            </div>
            <ul className="divide-y divide-slate-700/50">
              {grouped[cat].map((tag) => (
                <li key={tag.id} className="px-4 py-3">
                  {editingId === tag.id ? (
                    /* ── Inline edit row ── */
                    <div className="flex flex-col gap-2">
                      <div className="flex gap-2">
                        <input
                          value={editName}
                          onChange={(e) => setEditName(e.target.value)}
                          onKeyDown={(e) => { if (e.key === 'Enter') saveEdit(tag.id) }}
                          className="flex-1 rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-sm text-slate-100 outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
                        />
                        <input
                          value={editCategory}
                          onChange={(e) => setEditCategory(e.target.value)}
                          placeholder="Category"
                          className="flex-1 rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-sm text-slate-100 placeholder-slate-500 outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
                        />
                      </div>
                      <div className="flex items-center gap-3">
                        <div className="flex gap-1.5">
                          {COLOR_OPTIONS.map((c) => (
                            <button
                              key={c}
                              onClick={() => setEditColor(c)}
                              className={`h-5 w-5 rounded-full ${TAG_COLORS[c].dot} ring-offset-slate-800 ring-offset-2 transition ${editColor === c ? 'ring-2 ring-blue-400' : ''}`}
                              title={c}
                            />
                          ))}
                        </div>
                        <div className="ml-auto flex gap-2">
                          <button
                            onClick={cancelEdit}
                            className="rounded-lg px-3 py-1 text-xs text-slate-400 hover:bg-slate-700 hover:text-slate-200"
                          >
                            Cancel
                          </button>
                          <button
                            onClick={() => saveEdit(tag.id)}
                            disabled={saving || !editName.trim() || !!editCategoryError}
                            className="rounded-lg bg-blue-600 px-3 py-1 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
                          >
                            {saving ? 'Saving…' : 'Save'}
                          </button>
                        </div>
                      </div>
                      {editCategoryError && <p role="alert" className="text-xs text-amber-400">{editCategoryError}</p>}
                    </div>
                  ) : (
                    /* ── Normal row ── */
                    <div className="flex items-center gap-3">
                      <span className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium ${TAG_COLORS[tag.color]?.chip ?? TAG_COLORS.slate.chip}`}>
                        {tag.name}
                      </span>
                      {tag.is_default && (
                        <span className="text-xs text-slate-500">default</span>
                      )}
                      <div className="ml-auto flex gap-1.5">
                        <button
                          onClick={() => startEdit(tag)}
                          className="rounded-lg px-2.5 py-1 text-xs text-slate-400 hover:bg-slate-700 hover:text-slate-200"
                        >
                          Edit
                        </button>
                        <button
                          onClick={() => handleDelete(tag)}
                          className="rounded-lg px-2.5 py-1 text-xs text-red-400 hover:bg-red-900/30 hover:text-red-300"
                        >
                          Delete
                        </button>
                      </div>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ))
      )}
    </div>
  )
}
