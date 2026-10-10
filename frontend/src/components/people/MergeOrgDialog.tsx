import { useState } from 'react'
import { Building2, GitMerge, Loader2 } from 'lucide-react'
import type { OrgDetail } from '../../api/types'
import { getOrganizations, mergeOrganization } from '../../api/client'
import { useDebouncedQuery } from '../../hooks/useDebouncedQuery'
import { useAsyncAction } from '../../hooks/useAsyncAction'
import { errorMessage } from '../../lib/toast'
import { Modal, modalButton } from '../ui'

/**
 * Pick an organization to merge `source` into. Every member and position of
 * `source` moves to the target and `source` is deleted.
 */
export default function MergeOrgDialog({
  source,
  onClose,
  onMerged,
}: {
  source: OrgDetail
  onClose: () => void
  onMerged: (into: OrgDetail, movedPositions: number) => void
}) {
  const [query, setQuery] = useState('')
  const [targetId, setTargetId] = useState<number | null>(null)
  const { data, pending } = useDebouncedQuery(query, (q, signal) => getOrganizations(q, signal), {
    minLength: 0, delayMs: 150,
  })
  const candidates = (data ?? []).filter((o) => o.id !== source.id)
  const target = candidates.find((o) => o.id === targetId) ?? null

  const merge = useAsyncAction(async (intoId: number) => {
    const result = await mergeOrganization(source.id, intoId)
    onMerged(result, result.moved_positions)
  })

  const members = source.members.length + source.former_members.length

  return (
    <Modal
      open
      onClose={onClose}
      size="md"
      icon={<GitMerge size={16} className="text-indigo-400" />}
      title={`Merge "${source.name}" into…`}
      footer={
        <>
          <button onClick={onClose} className={modalButton.secondary}>Cancel</button>
          <button
            onClick={() => targetId != null && merge.run(targetId)}
            disabled={targetId == null || merge.pending}
            className={modalButton.primary}
          >
            {merge.pending ? <Loader2 size={12} className="inline animate-spin" /> : 'Merge'}
          </button>
        </>
      }
    >
      <div className="flex flex-col gap-3 px-5 py-4">
        <p className="text-xs text-slate-400">
          {members} member{members !== 1 ? 's' : ''} and their positions move to the selected organization, and
          "{source.name}" is deleted. People with positions at both keep one.
        </p>
        <input
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search organizations…"
          aria-label="Search organizations"
          className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-sm text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500"
        />
        <div className="max-h-56 overflow-y-auto rounded-lg border border-slate-700" role="listbox" aria-label="Organizations">
          {candidates.length === 0 ? (
            <p className="px-3 py-4 text-center text-xs text-slate-500">{pending ? 'Searching…' : 'No other organizations'}</p>
          ) : candidates.map((o) => (
            <button
              key={o.id}
              role="option"
              aria-selected={o.id === targetId}
              onClick={() => setTargetId(o.id)}
              className={`flex w-full items-center gap-2 px-3 py-2 text-left text-xs transition-colors ${
                o.id === targetId ? 'bg-indigo-500/20 text-indigo-300' : 'text-slate-300 hover:bg-slate-700/50'
              }`}
            >
              <Building2 size={12} className="flex-shrink-0 text-slate-500" />
              <span className="flex-1 truncate">{o.name}</span>
              <span className="text-[10px] text-slate-500">{o.member_count} member{o.member_count !== 1 ? 's' : ''}</span>
            </button>
          ))}
        </div>
        {target && <p className="text-xs text-slate-300">Merge into <strong>{target.name}</strong>.</p>}
        {merge.error != null && <p role="alert" className="text-xs text-rose-400">{errorMessage(merge.error)}</p>}
      </div>
    </Modal>
  )
}
