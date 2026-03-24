import { useState, useEffect, useCallback } from 'react'
import {
  getSharedContact,
  updateSharedContact,
  listContactLinks,
  createContactLink,
  updateContactLink,
  deleteContactLink,
  listGroupChats,
  listDirectory,
  createSharedOrg,
  type SharedContact,
  type ContactLink,
  type GroupChat,
  type SharedOrg,
} from '../api/client'

interface ContactDetailProps {
  orgId: string
  contactId: string
  onBack: () => void
}

const RELATIONSHIP_OPTIONS = [
  { value: '', label: 'None' },
  { value: 'client', label: 'Client' },
  { value: 'vendor', label: 'Vendor' },
  { value: 'partner', label: 'Partner' },
  { value: 'prospect', label: 'Prospect' },
] as const

export default function ContactDetail({ orgId, contactId, onBack }: ContactDetailProps) {
  const [contact, setContact] = useState<SharedContact | null>(null)
  const [links, setLinks] = useState<ContactLink[]>([])
  const [groupChats, setGroupChats] = useState<GroupChat[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Inline editing
  const [editingName, setEditingName] = useState(false)
  const [editName, setEditName] = useState('')
  const [editingHandle, setEditingHandle] = useState(false)
  const [editHandle, setEditHandle] = useState('')
  const [saving, setSaving] = useState(false)

  // Add link form
  const [showAddLink, setShowAddLink] = useState(false)
  const [directory, setDirectory] = useState<SharedOrg[]>([])
  const [linkOrgId, setLinkOrgId] = useState('')
  const [linkNewOrgName, setLinkNewOrgName] = useState('')
  const [linkPosition, setLinkPosition] = useState('')
  const [linkRelationship, setLinkRelationship] = useState('')

  // Edit link
  const [editingLinkId, setEditingLinkId] = useState<string | null>(null)
  const [editLinkPosition, setEditLinkPosition] = useState('')
  const [editLinkRelationship, setEditLinkRelationship] = useState('')

  const loadData = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [c, l, g] = await Promise.all([
        getSharedContact(orgId, contactId),
        listContactLinks(orgId, contactId),
        listGroupChats(orgId, contactId).catch(() => []),
      ])
      setContact(c)
      setLinks(Array.isArray(l) ? l : [])
      setGroupChats(Array.isArray(g) ? g : [])
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to load contact')
    } finally {
      setLoading(false)
    }
  }, [orgId, contactId])

  useEffect(() => {
    loadData()
  }, [loadData])

  // Save name
  const handleSaveName = useCallback(async () => {
    if (!contact || !editName.trim()) return
    setSaving(true)
    try {
      const updated = await updateSharedContact(orgId, contactId, { name: editName.trim() })
      setContact(updated)
      setEditingName(false)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to update name')
    } finally {
      setSaving(false)
    }
  }, [orgId, contactId, editName, contact])

  // Save handle
  const handleSaveHandle = useCallback(async () => {
    if (!contact) return
    setSaving(true)
    try {
      const updated = await updateSharedContact(orgId, contactId, { telegram_handle: editHandle.trim() })
      setContact(updated)
      setEditingHandle(false)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to update handle')
    } finally {
      setSaving(false)
    }
  }, [orgId, contactId, editHandle, contact])

  // Add link
  const handleOpenAddLink = useCallback(async () => {
    setShowAddLink(true)
    try {
      const dir = await listDirectory(orgId)
      setDirectory(Array.isArray(dir) ? dir : [])
    } catch {
      setDirectory([])
    }
  }, [orgId])

  const handleCreateLink = useCallback(async () => {
    setSaving(true)
    setError(null)
    try {
      let targetOrgId = linkOrgId

      if (!targetOrgId && linkNewOrgName.trim()) {
        const newOrg = await createSharedOrg(orgId, { name: linkNewOrgName.trim() })
        targetOrgId = newOrg.id
      }

      if (!targetOrgId) {
        setError('Select or create an external organization.')
        setSaving(false)
        return
      }

      const data: { shared_org_id: string; position?: string; relationship_context?: string } = {
        shared_org_id: targetOrgId,
      }
      if (linkPosition.trim()) data.position = linkPosition.trim()
      if (linkRelationship) data.relationship_context = linkRelationship

      await createContactLink(orgId, contactId, data)
      setShowAddLink(false)
      setLinkOrgId('')
      setLinkNewOrgName('')
      setLinkPosition('')
      setLinkRelationship('')

      // Reload links
      const l = await listContactLinks(orgId, contactId)
      setLinks(Array.isArray(l) ? l : [])
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to create link')
    } finally {
      setSaving(false)
    }
  }, [orgId, contactId, linkOrgId, linkNewOrgName, linkPosition, linkRelationship])

  // Update link
  const handleSaveLink = useCallback(async (linkId: string) => {
    const originalLink = links.find((l) => l.id === linkId)
    if (!originalLink) return

    const data: { position?: string; relationship_context?: string } = {}
    const trimmedPosition = editLinkPosition.trim()
    const originalPosition = originalLink.position || ''
    const originalRelationship = originalLink.relationship_context || ''

    if (trimmedPosition !== originalPosition) data.position = trimmedPosition
    if (editLinkRelationship !== originalRelationship) data.relationship_context = editLinkRelationship

    if (Object.keys(data).length === 0) {
      setEditingLinkId(null)
      return
    }

    setSaving(true)
    try {
      await updateContactLink(orgId, contactId, linkId, data)
      setEditingLinkId(null)

      const l = await listContactLinks(orgId, contactId)
      setLinks(Array.isArray(l) ? l : [])
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to update link')
    } finally {
      setSaving(false)
    }
  }, [orgId, contactId, editLinkPosition, editLinkRelationship, links])

  // Delete link
  const handleDeleteLink = useCallback(async (linkId: string) => {
    if (!confirm('Remove this org link?')) return
    try {
      await deleteContactLink(orgId, contactId, linkId)
      setLinks((prev) => prev.filter((l) => l.id !== linkId))
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to remove link')
    }
  }, [orgId, contactId])

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-blue-400 border-t-transparent" />
      </div>
    )
  }

  if (!contact) {
    return (
      <div className="p-6">
        <button onClick={onBack} className="mb-4 text-xs text-blue-400 hover:text-blue-300">
          &larr; Back
        </button>
        <p className="text-sm text-red-400">{error || 'Contact not found.'}</p>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full overflow-y-auto">
      {/* Header */}
      <div className="flex items-center gap-3 border-b border-slate-700 px-4 py-3">
        <button onClick={onBack} className="text-xs text-blue-400 hover:text-blue-300">
          &larr; Back
        </button>
        <h2 className="text-sm font-semibold text-slate-200">Contact Detail</h2>
      </div>

      <div className="p-4 space-y-6">
        {/* Error banner */}
        {error && (
          <div className="rounded-lg bg-red-900/40 border border-red-700/50 px-3 py-2 text-xs text-red-300">
            {error}
            <button onClick={() => setError(null)} className="ml-2 text-red-400 hover:text-red-200">dismiss</button>
          </div>
        )}

        {/* Contact info */}
        <div className="rounded-xl border border-slate-700 bg-slate-800 p-4">
          <div className="flex items-start gap-4">
            <div className="flex h-14 w-14 shrink-0 items-center justify-center rounded-full bg-indigo-900/40 text-indigo-300 text-xl font-semibold">
              {contact.name.charAt(0).toUpperCase()}
            </div>
            <div className="flex-1 min-w-0 space-y-3">
              {/* Name */}
              <div>
                <label className="block text-[10px] font-medium uppercase tracking-wide text-slate-500 mb-1">Name</label>
                {editingName ? (
                  <div className="flex gap-2">
                    <input
                      type="text"
                      value={editName}
                      onChange={(e) => setEditName(e.target.value)}
                      className="flex-1 rounded border border-slate-600 bg-slate-700 px-2 py-1 text-sm text-slate-100 outline-none focus:border-blue-500"
                      autoFocus
                      onKeyDown={(e) => { if (e.key === 'Enter') handleSaveName(); if (e.key === 'Escape') setEditingName(false) }}
                    />
                    <button onClick={handleSaveName} disabled={saving} className="rounded bg-blue-600 px-2 py-1 text-xs text-white hover:bg-blue-500 disabled:opacity-50">Save</button>
                    <button onClick={() => setEditingName(false)} className="rounded border border-slate-600 px-2 py-1 text-xs text-slate-400 hover:bg-slate-700">Cancel</button>
                  </div>
                ) : (
                  <p className="text-sm text-slate-200 cursor-pointer hover:text-blue-300" onClick={() => { setEditName(contact.name); setEditingName(true) }}>
                    {contact.name}
                    <span className="ml-2 text-[10px] text-slate-500">(click to edit)</span>
                  </p>
                )}
              </div>

              {/* Telegram handle */}
              <div>
                <label className="block text-[10px] font-medium uppercase tracking-wide text-slate-500 mb-1">Telegram Handle</label>
                {editingHandle ? (
                  <div className="flex gap-2">
                    <div className="flex flex-1 items-center rounded border border-slate-600 bg-slate-700 px-2 text-sm text-slate-400">
                      @
                      <input
                        type="text"
                        value={editHandle}
                        onChange={(e) => setEditHandle(e.target.value)}
                        className="flex-1 bg-transparent py-1 text-sm text-slate-100 outline-none"
                        autoFocus
                        onKeyDown={(e) => { if (e.key === 'Enter') handleSaveHandle(); if (e.key === 'Escape') setEditingHandle(false) }}
                      />
                    </div>
                    <button onClick={handleSaveHandle} disabled={saving} className="rounded bg-blue-600 px-2 py-1 text-xs text-white hover:bg-blue-500 disabled:opacity-50">Save</button>
                    <button onClick={() => setEditingHandle(false)} className="rounded border border-slate-600 px-2 py-1 text-xs text-slate-400 hover:bg-slate-700">Cancel</button>
                  </div>
                ) : (
                  <p className="text-sm text-slate-200 cursor-pointer hover:text-blue-300" onClick={() => { setEditHandle(contact.telegram_handle || ''); setEditingHandle(true) }}>
                    {contact.telegram_handle ? `@${contact.telegram_handle}` : '(not set)'}
                    <span className="ml-2 text-[10px] text-slate-500">(click to edit)</span>
                  </p>
                )}
              </div>

              {/* Meta */}
              <div className="flex gap-4 text-[10px] text-slate-500">
                <span>Telegram ID: {contact.telegram_id}</span>
                <span>Added: {new Date(contact.created_at).toLocaleDateString()}</span>
                <span>Updated: {new Date(contact.updated_at).toLocaleDateString()}</span>
              </div>
            </div>
          </div>
        </div>

        {/* Org Links */}
        <div className="rounded-xl border border-slate-700 bg-slate-800 p-4">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wide">Organization Links</h3>
            <button
              onClick={handleOpenAddLink}
              className="rounded-lg border border-blue-700/50 bg-blue-900/30 px-2 py-1 text-[10px] font-medium text-blue-300 hover:bg-blue-900/50"
            >
              + Add Link
            </button>
          </div>

          {links.length === 0 && !showAddLink && (
            <p className="text-xs text-slate-500">No org links yet.</p>
          )}

          <div className="space-y-2">
            {links.map((link) => (
              <div key={link.id} className="rounded-lg border border-slate-700 bg-slate-900/50 p-3">
                {editingLinkId === link.id ? (
                  <div className="space-y-2">
                    <div>
                      <label className="block text-[10px] text-slate-500 mb-1">Position</label>
                      <input
                        type="text"
                        value={editLinkPosition}
                        onChange={(e) => setEditLinkPosition(e.target.value)}
                        className="w-full rounded border border-slate-600 bg-slate-700 px-2 py-1 text-xs text-slate-100 outline-none"
                      />
                    </div>
                    <div>
                      <label className="block text-[10px] text-slate-500 mb-1">Relationship</label>
                      <select
                        value={editLinkRelationship}
                        onChange={(e) => setEditLinkRelationship(e.target.value)}
                        className="w-full rounded border border-slate-600 bg-slate-700 px-2 py-1 text-xs text-slate-100 outline-none"
                      >
                        {RELATIONSHIP_OPTIONS.map((o) => (
                          <option key={o.value} value={o.value}>{o.label}</option>
                        ))}
                      </select>
                    </div>
                    <div className="flex gap-2">
                      <button onClick={() => handleSaveLink(link.id)} disabled={saving} className="rounded bg-blue-600 px-2 py-1 text-[10px] text-white hover:bg-blue-500 disabled:opacity-50">Save</button>
                      <button onClick={() => setEditingLinkId(null)} className="rounded border border-slate-600 px-2 py-1 text-[10px] text-slate-400 hover:bg-slate-700">Cancel</button>
                    </div>
                  </div>
                ) : (
                  <div className="flex items-center justify-between">
                    <div>
                      <span className="text-xs font-medium text-slate-200">Org: {link.shared_org_id.substring(0, 8)}...</span>
                      {link.position && <span className="ml-2 text-xs text-slate-400">{link.position}</span>}
                      {link.relationship_context && (
                        <span className="ml-2 rounded bg-slate-700 px-1.5 py-0.5 text-[10px] text-slate-300">{link.relationship_context}</span>
                      )}
                    </div>
                    <div className="flex gap-1">
                      <button
                        onClick={() => { setEditingLinkId(link.id); setEditLinkPosition(link.position || ''); setEditLinkRelationship(link.relationship_context || '') }}
                        className="rounded px-1.5 py-0.5 text-[10px] text-blue-400 hover:bg-blue-900/30"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => handleDeleteLink(link.id)}
                        className="rounded px-1.5 py-0.5 text-[10px] text-red-400 hover:bg-red-900/30"
                      >
                        Remove
                      </button>
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>

          {/* Add link form */}
          {showAddLink && (
            <div className="mt-3 rounded-lg border border-slate-600 bg-slate-900/50 p-3 space-y-2">
              {directory.length > 0 && (
                <div>
                  <label className="block text-[10px] text-slate-500 mb-1">Existing organization</label>
                  <select
                    value={linkOrgId}
                    onChange={(e) => { setLinkOrgId(e.target.value); if (e.target.value) setLinkNewOrgName('') }}
                    className="w-full rounded border border-slate-600 bg-slate-700 px-2 py-1 text-xs text-slate-100 outline-none"
                  >
                    <option value="">-- Select --</option>
                    {directory.map((o) => (
                      <option key={o.id} value={o.id}>{o.name}{o.domain ? ` (${o.domain})` : ''}</option>
                    ))}
                  </select>
                </div>
              )}
              <div>
                <label className="block text-[10px] text-slate-500 mb-1">{directory.length > 0 ? 'Or create new' : 'Organization name'}</label>
                <input
                  type="text"
                  value={linkNewOrgName}
                  onChange={(e) => { setLinkNewOrgName(e.target.value); if (e.target.value) setLinkOrgId('') }}
                  placeholder="e.g. Acme Corp"
                  disabled={!!linkOrgId}
                  className="w-full rounded border border-slate-600 bg-slate-700 px-2 py-1 text-xs text-slate-100 outline-none placeholder:text-slate-500 disabled:opacity-50"
                />
              </div>
              <div>
                <label className="block text-[10px] text-slate-500 mb-1">Position</label>
                <input
                  type="text"
                  value={linkPosition}
                  onChange={(e) => setLinkPosition(e.target.value)}
                  placeholder="e.g. VP Sales"
                  className="w-full rounded border border-slate-600 bg-slate-700 px-2 py-1 text-xs text-slate-100 outline-none placeholder:text-slate-500"
                />
              </div>
              <div>
                <label className="block text-[10px] text-slate-500 mb-1">Relationship</label>
                <select
                  value={linkRelationship}
                  onChange={(e) => setLinkRelationship(e.target.value)}
                  className="w-full rounded border border-slate-600 bg-slate-700 px-2 py-1 text-xs text-slate-100 outline-none"
                >
                  {RELATIONSHIP_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                  ))}
                </select>
              </div>
              <div className="flex gap-2">
                <button onClick={handleCreateLink} disabled={saving || (!linkOrgId && !linkNewOrgName.trim())} className="rounded bg-blue-600 px-2 py-1 text-[10px] text-white hover:bg-blue-500 disabled:opacity-50">
                  Create Link
                </button>
                <button onClick={() => setShowAddLink(false)} className="rounded border border-slate-600 px-2 py-1 text-[10px] text-slate-400 hover:bg-slate-700">Cancel</button>
              </div>
            </div>
          )}
        </div>

        {/* Group Chats */}
        <div className="rounded-xl border border-slate-700 bg-slate-800 p-4">
          <h3 className="mb-3 text-xs font-semibold text-slate-300 uppercase tracking-wide">Group Chat Memberships</h3>
          {groupChats.length === 0 ? (
            <p className="text-xs text-slate-500">No group chat memberships recorded.</p>
          ) : (
            <div className="space-y-1.5">
              {groupChats.map((gc) => (
                <div key={gc.id} className="flex items-center gap-2 rounded-lg border border-slate-700 bg-slate-900/50 px-3 py-2">
                  <svg className="h-3.5 w-3.5 shrink-0 text-slate-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
                  </svg>
                  <span className="text-xs text-slate-200">{gc.chat_title}</span>
                  <span className="ml-auto text-[10px] text-slate-500">{new Date(gc.added_at).toLocaleDateString()}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
