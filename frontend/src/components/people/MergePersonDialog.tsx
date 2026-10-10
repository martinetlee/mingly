import { useState } from 'react'
import { GitMerge, Loader2, User } from 'lucide-react'
import type { Person } from '../../api/types'
import { getPeoplePage } from '../../api/client'
import { mergePerson, type MergeResult } from '../../api/peopleLinks'
import { useDebouncedQuery } from '../../hooks/useDebouncedQuery'
import { useAsyncAction } from '../../hooks/useAsyncAction'
import { errorMessage } from '../../lib/toast'
import ChannelIcon from '../channels/ChannelIcon'
import { Modal, modalButton } from '../ui'

/**
 * Pick a person to merge into `person`. The picked person's linked accounts,
 * positions, notes and catch-up links move to `person`, empty fields are
 * filled from it, and it is removed (the merge can be undone).
 */
export default function MergePersonDialog({
  person,
  onClose,
  onMerged,
}: {
  person: Person
  onClose: () => void
  onMerged: (result: MergeResult, other: Person) => void
}) {
  const [query, setQuery] = useState('')
  const [otherId, setOtherId] = useState<number | null>(null)
  const { data, pending } = useDebouncedQuery(
    query,
    (q, signal) => getPeoplePage({ search: q, limit: 20 }, signal),
    { minLength: 1, delayMs: 150 },
  )
  const candidates = (data?.items ?? []).filter((p) => p.id !== person.id && !p.is_self)
  const other = candidates.find((p) => p.id === otherId) ?? null

  const merge = useAsyncAction(async (picked: Person) => {
    onMerged(await mergePerson(person.id, picked.id), picked)
  })

  return (
    <Modal
      open
      onClose={onClose}
      size="md"
      icon={<GitMerge size={16} className="text-indigo-400" />}
      title={`Merge into ${person.name}`}
      footer={
        <>
          <button onClick={onClose} className={modalButton.secondary}>Cancel</button>
          <button
            onClick={() => other != null && merge.run(other)}
            disabled={other == null || merge.pending}
            className={modalButton.primary}
          >
            {merge.pending ? <Loader2 size={12} className="inline animate-spin" /> : 'Merge'}
          </button>
        </>
      }
    >
      <div className="flex flex-col gap-3 px-5 py-4">
        <p className="text-xs text-slate-400">
          Pick the person who is really {person.name}. Their linked accounts, positions and notes move here, and
          they are removed. You can undo this from the person page.
        </p>
        <input
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search people…"
          aria-label="Search people"
          className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-sm text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500"
        />
        <div className="max-h-56 overflow-y-auto rounded-lg border border-slate-700" role="listbox" aria-label="People">
          {candidates.length === 0 ? (
            <p className="px-3 py-4 text-center text-xs text-slate-500">
              {pending ? 'Searching…' : query.trim() ? 'No one else matches' : 'Type a name, username or phone'}
            </p>
          ) : candidates.map((p) => (
            <button
              key={p.id}
              role="option"
              aria-selected={p.id === otherId}
              onClick={() => setOtherId(p.id)}
              className={`flex w-full items-center gap-2 px-3 py-2 text-left text-xs transition-colors ${
                p.id === otherId ? 'bg-indigo-500/20 text-indigo-300' : 'text-slate-300 hover:bg-slate-700/50'
              }`}
            >
              <User size={12} className="flex-shrink-0 text-slate-500" />
              <span className="flex-1 truncate">
                {p.name}
                {p.primary_position && (
                  <span className="ml-1.5 text-[10px] text-slate-500">{p.primary_position.organization}</span>
                )}
              </span>
              <span className="flex items-center gap-1">
                {p.identities.map((i) => <ChannelIcon key={i.id} channel={i.channel} size={11} />)}
              </span>
            </button>
          ))}
        </div>
        {other && (
          <p className="text-xs text-slate-300">
            Merge <strong>{other.name}</strong> ({other.identities.length} linked account
            {other.identities.length !== 1 ? 's' : ''}) into <strong>{person.name}</strong>.
          </p>
        )}
        {merge.error != null && <p role="alert" className="text-xs text-rose-400">{errorMessage(merge.error)}</p>}
      </div>
    </Modal>
  )
}
