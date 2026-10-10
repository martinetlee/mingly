import { useCallback, useEffect, useState } from 'react'
import { Plus, Link2, Copy, Trash2, ChevronDown, ChevronUp } from 'lucide-react'
import type { Conversation, InviteLink } from '../../../api/types'
import { getInviteLinks, createInviteLink, revokeInviteLink } from '../../../api/client'
import { reportError, toast } from '../../../lib/toast'
import { fmtShortDate } from '../../../utils/time'

export default function InviteLinksSection({ chat }: { chat: Conversation }) {
  const [expanded, setExpanded] = useState(false)
  const [links, setLinks] = useState<InviteLink[]>([])
  const [loading, setLoading] = useState(false)
  const [creating, setCreating] = useState(false)
  const [showCreate, setShowCreate] = useState(false)
  const [newTitle, setNewTitle] = useState('')
  const [newLimit, setNewLimit] = useState('')

  const loadLinks = useCallback(() => {
    setLoading(true)
    getInviteLinks(chat.id)
      .then(setLinks)
      .catch(() => setLinks([]))
      .finally(() => setLoading(false))
  }, [chat.id])

  useEffect(() => {
    if (expanded) loadLinks()
  }, [expanded, loadLinks])

  const handleCreate = async () => {
    setCreating(true)
    try {
      const link = await createInviteLink(chat.id, {
        title: newTitle || undefined,
        usage_limit: newLimit ? parseInt(newLimit, 10) : undefined,
      })
      setLinks((prev) => [...prev, link])
      setShowCreate(false)
      setNewTitle('')
      setNewLimit('')
    } catch (e) { reportError(e, 'Create invite link failed') }
    setCreating(false)
  }

  const handleRevoke = async (link: string) => {
    try {
      await revokeInviteLink(chat.id, link)
      setLinks((prev) => prev.filter((l) => l.link !== link))
    } catch (e) { reportError(e, 'Revoke failed') }
  }

  const handleCopy = (link: string) => {
    navigator.clipboard.writeText(link).then(() => toast.success('Link copied'), (e) => reportError(e, 'Copy failed'))
  }

  return (
    <div className="px-4 py-4 border-t border-slate-700/50 flex flex-col gap-2">
      <button onClick={() => setExpanded((v) => !v)} className="flex items-center gap-1.5 text-xs font-medium text-slate-400 uppercase tracking-wider hover:text-slate-300 transition-colors">
        <Link2 size={11} />
        Invite Links
        {expanded ? <ChevronUp size={11} className="ml-auto" /> : <ChevronDown size={11} className="ml-auto" />}
      </button>
      {expanded && (
        <div className="flex flex-col gap-2">
          {loading ? (
            <p className="text-xs text-slate-500">Loading...</p>
          ) : links.length === 0 ? (
            <p className="text-xs text-slate-500">No invite links</p>
          ) : (
            <div className="flex flex-col gap-1.5 max-h-40 overflow-y-auto">
              {links.map((l) => (
                <div key={l.link} className="rounded bg-slate-700/50 px-2 py-1.5 text-[11px] flex flex-col gap-1">
                  <div className="flex items-center gap-1">
                    <span className="text-slate-300 truncate flex-1" title={l.link}>
                      {l.title || l.link.split('/').pop()}
                    </span>
                    <button onClick={() => handleCopy(l.link)} title="Copy" className="text-slate-500 hover:text-slate-300">
                      <Copy size={10} />
                    </button>
                    <button onClick={() => handleRevoke(l.link)} title="Revoke" className="text-slate-500 hover:text-red-400">
                      <Trash2 size={10} />
                    </button>
                  </div>
                  <div className="flex gap-2 text-[10px] text-slate-500">
                    <span>{l.usage} uses{l.usage_limit ? ` / ${l.usage_limit}` : ''}</span>
                    {l.expire_date && <span>Expires: {fmtShortDate(l.expire_date)}</span>}
                  </div>
                </div>
              ))}
            </div>
          )}

          {showCreate ? (
            <div className="rounded border border-slate-600 bg-slate-700/50 p-2 flex flex-col gap-1.5">
              <input
                autoFocus
                value={newTitle}
                onChange={(e) => setNewTitle(e.target.value)}
                placeholder="Link title (optional)"
                className="w-full rounded bg-slate-700 border border-slate-600 px-2 py-1 text-xs text-slate-200 outline-none focus:border-indigo-500"
              />
              <input
                value={newLimit}
                onChange={(e) => setNewLimit(e.target.value.replace(/\D/g, ''))}
                placeholder="Usage limit (optional)"
                className="w-full rounded bg-slate-700 border border-slate-600 px-2 py-1 text-xs text-slate-200 outline-none focus:border-indigo-500"
              />
              <div className="flex gap-1.5">
                <button onClick={handleCreate} disabled={creating} className="flex-1 rounded bg-indigo-500 px-2 py-1 text-[10px] font-medium text-white hover:bg-indigo-600 disabled:opacity-40">
                  {creating ? '...' : 'Create'}
                </button>
                <button onClick={() => setShowCreate(false)} className="rounded px-2 py-1 text-[10px] text-slate-400 hover:text-slate-200">
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <button onClick={() => setShowCreate(true)} className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200 transition-colors">
              <Plus size={12} /> New link
            </button>
          )}
        </div>
      )}
    </div>
  )
}
