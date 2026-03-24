import { useState, useEffect, useCallback } from 'react'
import {
  getCloudOrgs,
  listDirectory,
  createSharedOrg,
  getSharedOrgDetail,
  type SharedOrg,
  type SharedOrgDetail,
} from '../api/client'

export default function DirectoryList() {
  const [orgs, setOrgs] = useState<Array<{ id: string; name: string; domain: string }>>([])
  const [selectedOrgId, setSelectedOrgId] = useState<string | null>(null)
  const [directory, setDirectory] = useState<SharedOrg[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Create shared org form
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [newDomain, setNewDomain] = useState('')
  const [newWebsite, setNewWebsite] = useState('')
  const [creating, setCreating] = useState(false)

  // Detail view
  const [selectedSharedOrg, setSelectedSharedOrg] = useState<SharedOrgDetail | null>(null)
  const [loadingDetail, setLoadingDetail] = useState(false)

  useEffect(() => {
    getCloudOrgs()
      .then((data) => {
        const active = data.filter((o) => o.status === 'active')
        setOrgs(active)
        if (active.length > 0) setSelectedOrgId(active[0].id)
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  const loadDirectory = useCallback(async () => {
    if (!selectedOrgId) return
    setLoading(true)
    try {
      const dir = await listDirectory(selectedOrgId)
      setDirectory(Array.isArray(dir) ? dir : [])
    } catch {
      setDirectory([])
    } finally {
      setLoading(false)
    }
  }, [selectedOrgId])

  useEffect(() => {
    loadDirectory()
  }, [loadDirectory])

  const handleCreate = useCallback(async () => {
    if (!selectedOrgId || !newName.trim()) return
    setCreating(true)
    setError(null)
    try {
      const data: { name: string; domain?: string; website?: string } = { name: newName.trim() }
      if (newDomain.trim()) data.domain = newDomain.trim()
      if (newWebsite.trim()) data.website = newWebsite.trim()
      await createSharedOrg(selectedOrgId, data)
      setShowCreate(false)
      setNewName('')
      setNewDomain('')
      setNewWebsite('')
      await loadDirectory()
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to create org')
    } finally {
      setCreating(false)
    }
  }, [selectedOrgId, newName, newDomain, newWebsite, loadDirectory])

  const handleViewDetail = useCallback(async (sharedOrgId: string) => {
    if (!selectedOrgId) return
    setLoadingDetail(true)
    try {
      const detail = await getSharedOrgDetail(selectedOrgId, sharedOrgId)
      setSelectedSharedOrg(detail)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to load org details')
    } finally {
      setLoadingDetail(false)
    }
  }, [selectedOrgId])

  if (orgs.length === 0 && !loading) {
    return (
      <div className="p-6 text-center text-sm text-slate-400">
        No active org memberships. Connect to a cloud org in Settings.
      </div>
    )
  }

  // Detail view
  if (selectedSharedOrg) {
    return (
      <div className="flex flex-col h-full">
        <div className="flex items-center gap-3 border-b border-slate-700 px-4 py-3">
          <button onClick={() => setSelectedSharedOrg(null)} className="text-xs text-blue-400 hover:text-blue-300">
            &larr; Back
          </button>
          <h2 className="text-sm font-semibold text-slate-200">{selectedSharedOrg.name}</h2>
        </div>
        <div className="p-4 space-y-4">
          <div className="rounded-xl border border-slate-700 bg-slate-800 p-4">
            <div className="grid grid-cols-2 gap-3 text-xs">
              <div>
                <span className="text-slate-500">Domain:</span>
                <span className="ml-2 text-slate-200">{selectedSharedOrg.domain || 'N/A'}</span>
              </div>
              <div>
                <span className="text-slate-500">Website:</span>
                <span className="ml-2 text-slate-200">{selectedSharedOrg.website || 'N/A'}</span>
              </div>
              <div>
                <span className="text-slate-500">Contacts:</span>
                <span className="ml-2 text-slate-200">{selectedSharedOrg.contact_count ?? 0}</span>
              </div>
              <div>
                <span className="text-slate-500">Created:</span>
                <span className="ml-2 text-slate-200">{new Date(selectedSharedOrg.created_at).toLocaleDateString()}</span>
              </div>
            </div>
          </div>

          {selectedSharedOrg.contacts && selectedSharedOrg.contacts.length > 0 && (
            <div className="rounded-xl border border-slate-700 bg-slate-800 p-4">
              <h3 className="mb-3 text-xs font-semibold text-slate-300 uppercase tracking-wide">Linked Contacts</h3>
              <div className="space-y-1.5">
                {selectedSharedOrg.contacts.map((c) => (
                  <div key={c.id} className="flex items-center gap-2 rounded-lg border border-slate-700 bg-slate-900/50 px-3 py-2">
                    <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-indigo-900/40 text-indigo-300 text-xs font-medium">
                      {c.name.charAt(0).toUpperCase()}
                    </div>
                    <div className="min-w-0 flex-1">
                      <p className="text-xs font-medium text-slate-200">{c.name}</p>
                      {c.telegram_handle && <p className="text-[10px] text-slate-400">@{c.telegram_handle}</p>}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-3 border-b border-slate-700 px-4 py-3">
        <h2 className="text-sm font-semibold text-slate-200">External Organizations</h2>

        {orgs.length > 1 && (
          <select
            value={selectedOrgId || ''}
            onChange={(e) => setSelectedOrgId(e.target.value)}
            className="rounded-lg border border-slate-600 bg-slate-700 px-2 py-1 text-xs text-slate-200 outline-none"
          >
            {orgs.map((o) => (
              <option key={o.id} value={o.id}>{o.name}</option>
            ))}
          </select>
        )}

        <div className="ml-auto">
          <button
            onClick={() => setShowCreate(!showCreate)}
            className="rounded-lg border border-blue-700/50 bg-blue-900/30 px-3 py-1.5 text-xs font-medium text-blue-300 hover:bg-blue-900/50"
          >
            + New Org
          </button>
        </div>
      </div>

      {/* Error */}
      {error && (
        <div className="mx-4 mt-3 rounded-lg bg-red-900/40 border border-red-700/50 px-3 py-2 text-xs text-red-300">
          {error}
          <button onClick={() => setError(null)} className="ml-2 text-red-400 hover:text-red-200">dismiss</button>
        </div>
      )}

      {/* Create form */}
      {showCreate && (
        <div className="mx-4 mt-3 rounded-xl border border-slate-600 bg-slate-800 p-4 space-y-2">
          <h3 className="text-xs font-medium text-slate-300 mb-2">Create External Organization</h3>
          <input
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="Organization name *"
            className="w-full rounded border border-slate-600 bg-slate-700 px-2 py-1.5 text-xs text-slate-100 outline-none placeholder:text-slate-500 focus:border-blue-500"
          />
          <input
            type="text"
            value={newDomain}
            onChange={(e) => setNewDomain(e.target.value)}
            placeholder="Domain (optional)"
            className="w-full rounded border border-slate-600 bg-slate-700 px-2 py-1.5 text-xs text-slate-100 outline-none placeholder:text-slate-500 focus:border-blue-500"
          />
          <input
            type="text"
            value={newWebsite}
            onChange={(e) => setNewWebsite(e.target.value)}
            placeholder="Website (optional)"
            className="w-full rounded border border-slate-600 bg-slate-700 px-2 py-1.5 text-xs text-slate-100 outline-none placeholder:text-slate-500 focus:border-blue-500"
          />
          <div className="flex gap-2">
            <button onClick={handleCreate} disabled={creating || !newName.trim()} className="rounded bg-blue-600 px-3 py-1.5 text-xs text-white hover:bg-blue-500 disabled:opacity-50">
              {creating ? 'Creating...' : 'Create'}
            </button>
            <button onClick={() => setShowCreate(false)} className="rounded border border-slate-600 px-3 py-1.5 text-xs text-slate-400 hover:bg-slate-700">Cancel</button>
          </div>
        </div>
      )}

      {/* Directory list */}
      <div className="flex-1 overflow-y-auto">
        {loading || loadingDetail ? (
          <div className="flex items-center justify-center py-12">
            <div className="h-6 w-6 animate-spin rounded-full border-2 border-blue-400 border-t-transparent" />
          </div>
        ) : directory.length === 0 ? (
          <div className="py-12 text-center text-sm text-slate-500">
            No external organizations yet. Create one to start linking contacts.
          </div>
        ) : (
          <div className="divide-y divide-slate-800">
            {directory.map((org) => (
              <button
                key={org.id}
                onClick={() => handleViewDetail(org.id)}
                className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-slate-800/50 transition-colors"
              >
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-emerald-900/40 text-emerald-300 text-sm font-medium">
                  {org.name.charAt(0).toUpperCase()}
                </div>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-slate-200">{org.name}</p>
                  <p className="truncate text-xs text-slate-400">
                    {org.domain || 'No domain'}
                    {org.contact_count != null && ` \u00B7 ${org.contact_count} contact${org.contact_count !== 1 ? 's' : ''}`}
                  </p>
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
