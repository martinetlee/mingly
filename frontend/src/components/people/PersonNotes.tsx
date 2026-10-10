import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { openConversationUrl } from '../../lib/routes'
import { Bot, Loader2, MessageSquareQuote, NotebookPen, Pencil, Plus, Trash2, User } from 'lucide-react'
import type { PersonNote, PersonNoteInput, PersonNoteKind } from '../../api/types'
import { createPersonNote, deletePersonNote, getPersonNotes, updatePersonNote } from '../../api/client'
import { useLatestQuery } from '../../hooks/useLatestQuery'
import { useAsyncAction } from '../../hooks/useAsyncAction'
import { useWsEvent } from '../../hooks/useWebSocket'
import { errorMessage } from '../../lib/toast'
import { fmtDateTime, fmtRelative, parseServerDate } from '../../utils/time'
import { useConfirm } from '../ui'
import Markdown from '../ui/Markdown'

export const NOTE_KINDS: { value: PersonNoteKind; label: string; tone: string }[] = [
  { value: 'context', label: 'Context', tone: 'bg-sky-500/15 text-sky-300 border-sky-500/30' },
  { value: 'relationship', label: 'Relationship', tone: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30' },
  { value: 'due_diligence', label: 'Due diligence', tone: 'bg-violet-500/15 text-violet-300 border-violet-500/30' },
  { value: 'risk', label: 'Risk', tone: 'bg-rose-500/15 text-rose-300 border-rose-500/30' },
  { value: 'todo', label: 'To-do', tone: 'bg-amber-500/15 text-amber-300 border-amber-500/30' },
]
const KIND_META = Object.fromEntries(NOTE_KINDS.map((k) => [k.value, k])) as Record<PersonNoteKind, (typeof NOTE_KINDS)[number]>

const inputCls = 'w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-sm text-slate-100 outline-none focus:border-indigo-500'

const sortTime = (n: PersonNote) => parseServerDate(n.created_at)?.getTime() ?? 0

/** Newest first; ties broken by id so the order is stable */
export function sortNotes(notes: PersonNote[]): PersonNote[] {
  return [...notes].sort((a, b) => sortTime(b) - sortTime(a) || b.id - a.id)
}

/**
 * Notes timeline for any person (contact or not): agent-written
 * context / due diligence over MCP plus the user's own notes. The user can
 * edit or delete any note. Live via `person_notes_changed`.
 */
export default function PersonNotes({ personId, identityKey }: {
  personId: number
  /** The person's identity ids: a change (merge / undo) moved notes in or out */
  identityKey?: string
}) {
  const navigate = useNavigate()
  const confirm = useConfirm()
  const q = useLatestQuery(['person-notes', personId], (signal) => getPersonNotes(personId, signal))
  const notes = useMemo(() => (q.data ? sortNotes(q.data) : []), [q.data])
  const [adding, setAdding] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)

  const idRef = useRef(personId)
  idRef.current = personId
  // Switching person closes any open editor
  const shownIdRef = useRef(personId)
  if (shownIdRef.current !== personId) {
    shownIdRef.current = personId
    if (adding) setAdding(false)
    if (editingId != null) setEditingId(null)
  }

  /** Apply a mutation result only if that person is still on screen, then resync with the server */
  const settle = (pid: number, fn: (prev: PersonNote[]) => PersonNote[]) => {
    if (idRef.current !== pid) return
    q.setData((prev) => fn(prev ?? []))
    void q.refetch()
  }

  useWsEvent('person_notes_changed', (e) => { if (e.person_id === idRef.current) void q.refetch() })
  useWsEvent('resync', () => { void q.refetch() })
  const identityRef = useRef({ personId, identityKey })
  useEffect(() => {
    const prev = identityRef.current
    identityRef.current = { personId, identityKey }
    if (prev.personId === personId && prev.identityKey !== identityKey) void q.refetch()
  }, [personId, identityKey]) // eslint-disable-line react-hooks/exhaustive-deps

  const create = useAsyncAction(async (pid: number, input: PersonNoteInput) => {
    const note = await createPersonNote(pid, input)
    settle(pid, (prev) => [note, ...prev.filter((n) => n.id !== note.id)])
    if (idRef.current === pid) setAdding(false)
    return true
  }, { errorContext: "Couldn't add the note" })

  const update = useAsyncAction(async (pid: number, id: number, input: PersonNoteInput) => {
    const note = await updatePersonNote(pid, id, input)
    settle(pid, (prev) => prev.map((n) => (n.id === id ? note : n)))
    if (idRef.current === pid) setEditingId(null)
    return true
  }, { errorContext: "Couldn't save the note" })

  const remove = (note: PersonNote) => {
    const pid = note.person_id
    void confirm({
      title: 'Delete this note?',
      message: note.author === 'agent'
        ? `“${note.title}” was written by ${note.agent_label || 'an agent'}. It is removed for good.`
        : `“${note.title}” is removed for good.`,
      destructive: true,
      confirmLabel: 'Delete',
      onConfirm: async () => {
        await deletePersonNote(pid, note.id)
        settle(pid, (prev) => prev.filter((n) => n.id !== note.id))
      },
    })
  }

  const openCitation = (conversationId: number, messageId: number) => navigate(openConversationUrl(conversationId, messageId))

  return (
    <div className="mb-6 rounded-xl border border-slate-700/50 bg-slate-800 p-4" data-testid="person-notes">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wider text-slate-500">
          <NotebookPen size={12} /> Notes{q.data ? ` (${notes.length})` : ''}
        </h3>
        {!adding && (
          <button
            type="button"
            onClick={() => { setAdding(true); setEditingId(null) }}
            className="flex items-center gap-1 rounded px-2 py-1 text-[11px] text-indigo-400 hover:bg-indigo-500/10 transition-colors"
          >
            <Plus size={11} /> Add note
          </button>
        )}
      </div>

      {adding && (
        <NoteEditor
          saving={create.pending}
          onCancel={() => setAdding(false)}
          onSave={(input) => void create.run(personId, input)}
        />
      )}

      {q.loading && !q.data && (
        <div className="flex items-center gap-2 py-2 text-xs text-slate-500"><Loader2 size={12} className="animate-spin" /> Loading notes…</div>
      )}
      {q.error != null && !q.data && !q.loading && (
        <div className="flex items-center gap-2 py-2 text-xs text-rose-400">
          <span className="flex-1">Couldn't load notes: {errorMessage(q.error)}</span>
          <button type="button" onClick={() => void q.refetch()} className="text-slate-300 underline hover:text-slate-100">Retry</button>
        </div>
      )}
      {q.data && notes.length === 0 && !adding && (
        <p className="text-xs text-slate-500">No notes yet. Add one, or let a connected agent record context here (Settings › MCP).</p>
      )}

      <ol className="flex flex-col gap-3">
        {notes.map((note) => (
          <li key={note.id} className="rounded-lg border border-slate-700 bg-slate-900/40 p-3" data-testid="person-note">
            {editingId === note.id ? (
              <NoteEditor
                initial={note}
                saving={update.pending}
                onCancel={() => setEditingId(null)}
                onSave={(input) => void update.run(note.person_id, note.id, input)}
              />
            ) : (
              <>
                <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
                  <span className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${KIND_META[note.kind]?.tone ?? 'border-slate-600 text-slate-300'}`}>
                    {KIND_META[note.kind]?.label ?? note.kind}
                  </span>
                  <span
                    className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium ${
                      note.author === 'agent' ? 'bg-indigo-500/15 text-indigo-300' : 'bg-slate-600/40 text-slate-300'
                    }`}
                  >
                    {note.author === 'agent' ? <Bot size={10} /> : <User size={10} />}
                    {note.author === 'agent' ? `Agent${note.agent_label ? ` — ${note.agent_label}` : ''}` : 'You'}
                  </span>
                  <span className="text-[10px] text-slate-500" title={fmtDateTime(note.created_at)}>
                    {fmtRelative(note.created_at)}
                    {note.updated_at && note.updated_at !== note.created_at && ' · edited'}
                  </span>
                  <span className="ml-auto flex items-center gap-0.5">
                    <button
                      type="button"
                      onClick={() => { setEditingId(note.id); setAdding(false) }}
                      className="rounded p-1 text-slate-500 hover:bg-slate-700 hover:text-slate-200"
                      aria-label={`Edit note ${note.title}`}
                      title="Edit"
                    >
                      <Pencil size={12} />
                    </button>
                    <button
                      type="button"
                      onClick={() => remove(note)}
                      className="rounded p-1 text-slate-500 hover:bg-slate-700 hover:text-rose-400"
                      aria-label={`Delete note ${note.title}`}
                      title="Delete"
                    >
                      <Trash2 size={12} />
                    </button>
                  </span>
                </div>
                <p className="mb-1 text-sm font-medium text-slate-100">{note.title}</p>
                {note.body && <Markdown content={note.body} className="text-slate-300" />}
                {note.citations.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {note.citations.map((c, i) => (
                      <button
                        key={`${c.conversation_id}:${c.message_id}`}
                        type="button"
                        onClick={() => openCitation(c.conversation_id, c.message_id)}
                        className="inline-flex items-center gap-1 rounded border border-indigo-500/25 bg-indigo-500/10 px-1.5 py-0.5 text-[10px] text-indigo-300 hover:bg-indigo-500/25 transition-colors"
                        title="Open the cited message"
                      >
                        <MessageSquareQuote size={10} /> Source {i + 1}
                      </button>
                    ))}
                  </div>
                )}
              </>
            )}
          </li>
        ))}
      </ol>
    </div>
  )
}

function NoteEditor({ initial, saving, onSave, onCancel }: {
  initial?: PersonNote
  saving: boolean
  onSave: (input: PersonNoteInput) => void
  onCancel: () => void
}) {
  const [kind, setKind] = useState<PersonNoteKind>(initial?.kind ?? 'context')
  const [title, setTitle] = useState(initial?.title ?? '')
  const [body, setBody] = useState(initial?.body ?? '')
  const canSave = title.trim() !== '' && !saving

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!canSave) return
    // Citations are kept as-is on edit (the backend keeps them when omitted)
    onSave({ kind, title: title.trim(), body })
  }

  return (
    <form onSubmit={submit} className="mb-3 flex flex-col gap-2" aria-label={initial ? 'Edit note' : 'New note'}>
      <div className="flex gap-2">
        <select aria-label="Kind" value={kind} onChange={(e) => setKind(e.target.value as PersonNoteKind)} className={`${inputCls} w-40 flex-shrink-0`}>
          {NOTE_KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
        </select>
        <input
          aria-label="Title"
          autoFocus
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Title"
          className={inputCls}
        />
      </div>
      <textarea
        aria-label="Body"
        value={body}
        onChange={(e) => setBody(e.target.value)}
        placeholder="Details (Markdown supported)"
        rows={4}
        className={`${inputCls} resize-y font-mono text-xs`}
        onKeyDown={(e) => { if (e.key === 'Escape') onCancel() }}
      />
      <div className="flex justify-end gap-2">
        <button type="button" onClick={onCancel} className="rounded px-3 py-1.5 text-xs text-slate-400 hover:text-slate-200">Cancel</button>
        <button
          type="submit"
          disabled={!canSave}
          className="flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
        >
          {saving && <Loader2 size={12} className="animate-spin" />}
          {initial ? 'Save' : 'Add note'}
        </button>
      </div>
    </form>
  )
}
