import { useState, useEffect, useCallback, useMemo } from 'react'
import {
  getCloudOrgs,
  listSharedContacts,
  type SharedContact,
} from '../api/client'

interface DirectoryBrowserProps {
  onSelectContact: (orgId: string, contactId: string) => void
}

export default function DirectoryBrowser({ onSelectContact }: DirectoryBrowserProps) {
  const [orgs, setOrgs] = useState<Array<{ id: string; name: string; domain: string }>>([])
  const [selectedOrgId, setSelectedOrgId] = useState<string | null>(null)
  const [contacts, setContacts] = useState<SharedContact[]>([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')

  // Load orgs
  useEffect(() => {
    getCloudOrgs()
      .then((data) => {
        const active = data.filter((o) => o.status === 'active')
        setOrgs(active)
        if (active.length > 0) {
          setSelectedOrgId(active[0].id)
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  // Load contacts when org changes
  useEffect(() => {
    if (!selectedOrgId) return
    setLoading(true)
    listSharedContacts(selectedOrgId)
      .then((data) => setContacts(Array.isArray(data) ? data : []))
      .catch(() => setContacts([]))
      .finally(() => setLoading(false))
  }, [selectedOrgId])

  const filtered = useMemo(() => {
    if (!search.trim()) return contacts
    const q = search.toLowerCase()
    return contacts.filter(
      (c) =>
        c.name.toLowerCase().includes(q) ||
        (c.telegram_handle && c.telegram_handle.toLowerCase().includes(q))
    )
  }, [contacts, search])

  const handleRefresh = useCallback(() => {
    if (!selectedOrgId) return
    setLoading(true)
    listSharedContacts(selectedOrgId)
      .then((data) => setContacts(Array.isArray(data) ? data : []))
      .catch(() => setContacts([]))
      .finally(() => setLoading(false))
  }, [selectedOrgId])

  if (orgs.length === 0 && !loading) {
    return (
      <div className="p-6 text-center text-sm text-slate-400">
        No active org memberships. Connect to a cloud org in Settings to use the shared directory.
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-3 border-b border-slate-700 px-4 py-3">
        <h2 className="text-sm font-semibold text-slate-200">Shared Contacts</h2>

        {orgs.length > 1 && (
          <select
            value={selectedOrgId || ''}
            onChange={(e) => setSelectedOrgId(e.target.value)}
            className="rounded-lg border border-slate-600 bg-slate-700 px-2 py-1 text-xs text-slate-200 outline-none"
          >
            {orgs.map((o) => (
              <option key={o.id} value={o.id}>
                {o.name}
              </option>
            ))}
          </select>
        )}

        <div className="ml-auto flex items-center gap-2">
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by name or handle..."
            className="w-56 rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-xs text-slate-200 outline-none placeholder:text-slate-500 focus:border-blue-500"
          />
          <button
            onClick={handleRefresh}
            className="rounded-lg border border-slate-600 px-2 py-1.5 text-xs text-slate-400 hover:bg-slate-700 hover:text-slate-200"
            title="Refresh"
          >
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </button>
        </div>
      </div>

      {/* Contact list */}
      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <div className="flex items-center justify-center py-12">
            <div className="h-6 w-6 animate-spin rounded-full border-2 border-blue-400 border-t-transparent" />
          </div>
        ) : filtered.length === 0 ? (
          <div className="py-12 text-center text-sm text-slate-500">
            {search ? 'No contacts match your search.' : 'No shared contacts yet.'}
          </div>
        ) : (
          <div className="divide-y divide-slate-800">
            {filtered.map((contact) => (
              <button
                key={contact.id}
                onClick={() => selectedOrgId && onSelectContact(selectedOrgId, contact.id)}
                className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-slate-800/50 transition-colors"
              >
                {/* Avatar placeholder */}
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-indigo-900/40 text-indigo-300 text-sm font-medium">
                  {contact.name.charAt(0).toUpperCase()}
                </div>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-slate-200">{contact.name}</p>
                  {contact.telegram_handle && (
                    <p className="truncate text-xs text-slate-400">@{contact.telegram_handle}</p>
                  )}
                </div>
                <div className="text-xs text-slate-500">
                  {new Date(contact.created_at).toLocaleDateString()}
                </div>
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Footer stats */}
      {!loading && (
        <div className="border-t border-slate-700 px-4 py-2 text-xs text-slate-500">
          {filtered.length} contact{filtered.length !== 1 ? 's' : ''}
          {search && contacts.length !== filtered.length && ` (of ${contacts.length} total)`}
        </div>
      )}
    </div>
  )
}
