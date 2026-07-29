import { useEffect, useState, useCallback, useRef } from 'react'
import { useLocation } from 'react-router-dom'
import { Plus, Search, Building2, Pencil, Trash2, Users, MessageSquare, FileText, RefreshCw, Sparkles, Loader2, ChevronDown, ChevronRight, UserMinus } from 'lucide-react'
import type { OrgSummary, OrgDetail } from '../api/types'
import { getOrganizations, getOrganization, createOrganization, updateOrganization, deleteOrganization, autoDiscoverOrgs, generateOrgContext } from '../api/client'
import PersonCard from '../components/PersonCard'

function OrgCard({
  org,
  selected,
  onClick,
}: {
  org: OrgSummary
  selected: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={selected ? 'true' : undefined}
      className={`flex w-full cursor-pointer items-center gap-3 px-4 py-3 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 focus-visible:ring-inset ${
        selected ? 'bg-indigo-500/15' : 'hover:bg-slate-700/50'
      }`}
    >
      <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg bg-slate-700 text-sm font-medium text-slate-300">
        <Building2 size={16} />
      </div>
      <div className="min-w-0 flex-1">
        <p title={org.name} className={`truncate text-sm font-medium ${selected ? 'text-indigo-300' : 'text-gray-200'}`}>
          {org.name}
        </p>
        <p className="text-xs text-slate-500">
          {org.member_count} member{org.member_count !== 1 ? 's' : ''}
          {org.former_member_count > 0 && `, ${org.former_member_count} former`}
        </p>
      </div>
    </button>
  )
}

export default function Organizations() {
  const location = useLocation()
  const [orgs, setOrgs] = useState<OrgSummary[]>([])
  const [selected, setSelected] = useState<OrgDetail | null>(null)
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [editingContext, setEditingContext] = useState(false)
  const [contextDraft, setContextDraft] = useState('')
  const [editingName, setEditingName] = useState(false)
  const [nameDraft, setNameDraft] = useState('')
  const [generatingContext, setGeneratingContext] = useState(false)
  const [showFormerMembers, setShowFormerMembers] = useState(false)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const [sidebarWidth, setSidebarWidth] = useState(300)
  const dragging = useRef(false)
  const pendingSelectRef = useRef<number | null>(null)

  // Monotonic guards: rapid searches / org clicks must resolve to the latest
  // request, not the slowest response
  const orgsSeqRef = useRef(0)
  const selectSeqRef = useRef(0)

  const loadOrgs = useCallback(async (q = '') => {
    const seq = ++orgsSeqRef.current
    const data = await getOrganizations(q)
    if (seq !== orgsSeqRef.current) return
    setOrgs(data)
    setLoading(false)
  }, [])

  // Parse URL param for deep-link selection: ?select=<orgId>
  useEffect(() => {
    const params = new URLSearchParams(location.search)
    const selectParam = params.get('select')
    if (selectParam) {
      const orgId = parseInt(selectParam, 10)
      if (!isNaN(orgId)) {
        pendingSelectRef.current = orgId
      }
    }
    loadOrgs()
  }, [loadOrgs, location.search])

  // When orgs load, auto-select the org from URL params if pending
  useEffect(() => {
    if (orgs.length === 0 || pendingSelectRef.current === null) return
    const orgId = pendingSelectRef.current
    pendingSelectRef.current = null
    const target = orgs.find((o) => o.id === orgId)
    if (target) {
      getOrganization(target.id).then((detail) => {
        setSelected(detail)
        setEditingContext(false)
        setEditingName(false)
        setShowFormerMembers(false)
      })
    }
  }, [orgs])

  useEffect(() => {
    const timer = setTimeout(() => loadOrgs(search), 200)
    return () => clearTimeout(timer)
  }, [search, loadOrgs])

  const handleSelect = async (org: OrgSummary) => {
    const seq = ++selectSeqRef.current
    const detail = await getOrganization(org.id)
    if (seq !== selectSeqRef.current) return
    setSelected(detail)
    setEditingContext(false)
    setEditingName(false)
    setShowFormerMembers(false)
  }

  const handleCreate = async () => {
    if (!newName.trim()) return
    try {
      const detail = await createOrganization(newName.trim())
      setSelected(detail)
      setNewName('')
      setShowCreate(false)
      await loadOrgs(search)
    } catch {
      // already exists
    }
  }

  const handleDiscover = async () => {
    await autoDiscoverOrgs()
    await loadOrgs(search)
  }

  const handleSaveContext = async () => {
    if (!selected) return
    const updated = await updateOrganization(selected.id, { context: contextDraft })
    setSelected(updated)
    setEditingContext(false)
    await loadOrgs(search)
  }

  const handleSaveName = async () => {
    if (!selected || !nameDraft.trim()) return
    const updated = await updateOrganization(selected.id, { name: nameDraft.trim() })
    setSelected(updated)
    setEditingName(false)
    await loadOrgs(search)
  }

  const handleGenerateContext = async () => {
    if (!selected || generatingContext) return
    setGeneratingContext(true)
    try {
      const updated = await generateOrgContext(selected.id)
      setSelected(updated)
      await loadOrgs(search)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to generate context'
      setErrorMsg(msg)
      setTimeout(() => setErrorMsg(null), 5000)
    } finally {
      setGeneratingContext(false)
    }
  }

  const handleDelete = async () => {
    if (!selected) return
    await deleteOrganization(selected.id)
    setSelected(null)
    await loadOrgs(search)
  }

  const onDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    dragging.current = true
    const startX = e.clientX
    const startWidth = sidebarWidth
    const onMove = (e: MouseEvent) => {
      if (!dragging.current) return
      setSidebarWidth(Math.min(480, Math.max(220, startWidth + e.clientX - startX)))
    }
    const onUp = () => {
      dragging.current = false
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }, [sidebarWidth])

  return (
    <div className="flex flex-1 overflow-hidden relative">
      {/* Error toast */}
      {errorMsg && (
        <div className="absolute top-4 right-4 z-50 flex items-center gap-2 rounded-lg bg-red-900/90 px-4 py-2.5 text-sm text-red-200 shadow-lg border border-red-700/50">
          <span>{errorMsg}</span>
          <button onClick={() => setErrorMsg(null)} className="ml-1 text-red-400 hover:text-red-200">✕</button>
        </div>
      )}
      {/* Sidebar */}
      <div className="flex flex-shrink-0 flex-col border-r border-slate-700/50 bg-slate-800 relative" style={{ width: sidebarWidth }}>
        <div
          onMouseDown={onDragStart}
          className="absolute right-0 top-0 h-full w-1 cursor-col-resize hover:bg-indigo-500 transition-colors z-10"
        />

        <div className="flex items-center justify-between border-b border-slate-700/50 px-4 py-3">
          <h2 className="text-sm font-semibold text-gray-100">Organizations</h2>
          <div className="flex items-center gap-1">
            <button
              onClick={handleDiscover}
              title="Auto-discover from contacts"
              className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-700 transition-colors"
            >
              <RefreshCw size={13} />
            </button>
            <button
              onClick={() => setShowCreate((v) => !v)}
              title="Create organization"
              className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-700 transition-colors"
            >
              <Plus size={14} />
            </button>
          </div>
        </div>

        {showCreate && (
          <div className="border-b border-slate-700/50 px-3 py-3 flex gap-2">
            <input
              autoFocus
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') handleCreate(); if (e.key === 'Escape') setShowCreate(false) }}
              placeholder="Organization name..."
              className="flex-1 rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-sm text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500"
            />
            <button
              onClick={handleCreate}
              disabled={!newName.trim()}
              className="rounded-lg bg-indigo-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-600 disabled:opacity-40 transition-colors"
            >
              Add
            </button>
          </div>
        )}

        <div className="border-b border-slate-700/50 px-3 py-3">
          <div className="relative">
            <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-500" />
            <input
              type="text"
              placeholder="Search..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full rounded-lg border border-slate-600 bg-slate-700 pl-8 pr-3 py-1.5 text-sm text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500/30"
            />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto">
          {loading ? (
            <p className="px-4 py-6 text-center text-sm text-slate-500">Loading...</p>
          ) : orgs.length === 0 ? (
            <div className="px-4 py-6 text-center">
              <p className="text-sm text-slate-500">No organizations</p>
              <p className="mt-1 text-xs text-slate-600">Add positions to contacts or create one manually</p>
            </div>
          ) : (
            orgs.map((org) => (
              <OrgCard
                key={org.id}
                org={org}
                selected={selected?.id === org.id}
                onClick={() => handleSelect(org)}
              />
            ))
          )}
        </div>
      </div>

      {/* Detail */}
      <div className="flex flex-1 flex-col overflow-hidden bg-slate-900">
        {selected ? (
          <div className="flex-1 overflow-y-auto px-6 py-6 max-w-3xl">
            {/* Header */}
            <div className="flex items-start justify-between mb-6">
              <div className="flex items-center gap-3">
                <div className="flex h-14 w-14 items-center justify-center rounded-xl bg-slate-700 text-slate-300">
                  <Building2 size={24} />
                </div>
                <div>
                  {editingName ? (
                    <input
                      autoFocus
                      value={nameDraft}
                      onChange={(e) => setNameDraft(e.target.value)}
                      onKeyDown={(e) => { if (e.key === 'Enter') handleSaveName(); if (e.key === 'Escape') setEditingName(false) }}
                      onBlur={handleSaveName}
                      className="bg-slate-700 border border-slate-600 rounded px-2 py-0.5 text-lg font-bold text-gray-100 outline-none focus:border-indigo-500"
                    />
                  ) : (
                    <h1
                      className="text-lg font-bold text-gray-100 cursor-pointer hover:text-indigo-300 transition-colors group inline-flex items-center gap-1.5"
                      onClick={() => { setEditingName(true); setNameDraft(selected.name) }}
                    >
                      {selected.name}
                      <Pencil size={12} className="opacity-0 group-hover:opacity-100 text-slate-500 transition-opacity" />
                    </h1>
                  )}
                  <p className="text-xs text-slate-500">{selected.members.length} member{selected.members.length !== 1 ? 's' : ''}</p>
                </div>
              </div>
              <button
                onClick={handleDelete}
                title="Delete organization"
                className="rounded p-1.5 text-slate-500 hover:text-rose-400 transition-colors"
              >
                <Trash2 size={14} />
              </button>
            </div>

            {/* Context */}
            <div className="rounded-xl bg-slate-800 border border-slate-700/50 p-4 mb-4">
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-xs font-medium uppercase tracking-wider text-slate-500 flex items-center gap-1.5">
                  <FileText size={11} />
                  Context
                </h3>
                {!editingContext && (
                  <div className="flex items-center gap-2">
                    <button
                      onClick={handleGenerateContext}
                      disabled={generatingContext}
                      title="Generate context with AI"
                      className="text-xs text-violet-400 hover:text-violet-300 transition-colors flex items-center gap-1 disabled:opacity-40"
                    >
                      {generatingContext ? <Loader2 size={10} className="animate-spin" /> : <Sparkles size={10} />}
                      Generate
                    </button>
                    <button
                      onClick={() => { setEditingContext(true); setContextDraft(selected.context || '') }}
                      className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors flex items-center gap-1"
                    >
                      <Pencil size={10} />
                      Edit
                    </button>
                  </div>
                )}
              </div>
              {editingContext ? (
                <div className="flex flex-col gap-2">
                  <textarea
                    autoFocus
                    value={contextDraft}
                    onChange={(e) => setContextDraft(e.target.value)}
                    rows={5}
                    placeholder="Add context about this organization..."
                    className="w-full bg-slate-700 border border-slate-600 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none focus:border-indigo-500 resize-none"
                  />
                  <div className="flex gap-2">
                    <button
                      onClick={handleSaveContext}
                      className="rounded bg-indigo-500 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-600 transition-colors"
                    >
                      Save
                    </button>
                    <button
                      onClick={() => setEditingContext(false)}
                      className="rounded px-3 py-1 text-xs text-slate-400 hover:text-slate-200 transition-colors"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              ) : selected.context ? (
                <p className="text-sm text-gray-200 whitespace-pre-wrap leading-relaxed">{selected.context}</p>
              ) : (
                <p className="text-xs text-slate-600 italic">No context yet. Click Edit to add information about this organization.</p>
              )}
            </div>

            {/* Members */}
            <div className="rounded-xl bg-slate-800 border border-slate-700/50 p-4 mb-4">
              <h3 className="text-xs font-medium uppercase tracking-wider text-slate-500 flex items-center gap-1.5 mb-3">
                <Users size={11} />
                Members ({selected.members.length})
              </h3>
              {selected.members.length === 0 ? (
                <p className="text-xs text-slate-600 italic">No members. Add positions to contacts referencing this organization.</p>
              ) : (
                <div className="flex flex-col gap-1">
                  {selected.members.map((m) => (
                    <PersonCard
                      key={m.contact_id}
                      telegramId={m.telegram_id}
                      name={m.name}
                      username={m.username}
                      contactId={m.contact_id}
                      extraBadge={
                        m.title ? (
                          <span title={m.title} className="flex-shrink-0 rounded-full bg-slate-700 px-2 py-0.5 text-[10px] text-slate-400">
                            {m.title}
                          </span>
                        ) : undefined
                      }
                    />
                  ))}
                </div>
              )}
            </div>

            {/* Former Members */}
            {(selected.former_members?.length ?? 0) > 0 && (
              <div className="rounded-xl bg-slate-800 border border-slate-700/50 p-4 mb-4">
                <button
                  onClick={() => setShowFormerMembers((v) => !v)}
                  className="flex w-full items-center gap-1.5 text-left"
                >
                  {showFormerMembers ? (
                    <ChevronDown size={11} className="text-slate-500 flex-shrink-0" />
                  ) : (
                    <ChevronRight size={11} className="text-slate-500 flex-shrink-0" />
                  )}
                  <UserMinus size={11} className="text-slate-500 flex-shrink-0" />
                  <span className="text-xs font-medium uppercase tracking-wider text-slate-500">
                    Former Members ({selected.former_members.length})
                  </span>
                </button>
                {showFormerMembers && (
                  <div className="flex flex-col gap-1 mt-3">
                    {selected.former_members.map((m) => (
                      <PersonCard
                        key={m.contact_id}
                        telegramId={m.telegram_id}
                        name={m.name}
                        username={m.username}
                        contactId={m.contact_id}
                        muted
                        extraBadge={
                          <div className="flex items-center gap-2">
                            {m.ended_at && (
                              <span className="text-[10px] text-slate-600">
                                left {new Date(m.ended_at).toLocaleDateString()}
                              </span>
                            )}
                            {m.title && (
                              <span className="flex-shrink-0 rounded-full bg-slate-700/50 px-2 py-0.5 text-[10px] text-slate-500">
                                {m.title}
                              </span>
                            )}
                          </div>
                        }
                      />
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* Related chats */}
            {selected.related_chats.length > 0 && (
              <div className="rounded-xl bg-slate-800 border border-slate-700/50 p-4">
                <h3 className="text-xs font-medium uppercase tracking-wider text-slate-500 flex items-center gap-1.5 mb-3">
                  <MessageSquare size={11} />
                  Related Groups ({selected.related_chats.length})
                </h3>
                <div className="flex flex-col gap-2">
                  {selected.related_chats.map((rc) => (
                    <div key={rc.chat_id} className="rounded-lg bg-slate-700/30 border border-slate-700 p-3">
                      <div className="flex items-center gap-2 mb-1">
                        <MessageSquare size={12} className="text-slate-500" />
                        <span className="text-sm font-medium text-gray-200">{rc.chat_name}</span>
                        <span className="text-[10px] text-slate-500 rounded bg-slate-700 px-1">{rc.chat_type}</span>
                      </div>
                      {rc.last_message ? (
                        <div className="ml-5">
                          <p title={`${rc.last_message.sender_name ? `${rc.last_message.sender_name}: ` : ''}${rc.last_message.text || '[media]'}`} className="text-xs text-slate-400 truncate">
                            {rc.last_message.sender_name && (
                              <span className="text-slate-500">{rc.last_message.sender_name}: </span>
                            )}
                            {rc.last_message.text || '[media]'}
                          </p>
                          {rc.last_message.date && (
                            <p className="text-[10px] text-slate-600 mt-0.5">
                              {new Date(rc.last_message.date).toLocaleString()}
                            </p>
                          )}
                        </div>
                      ) : (
                        <p className="ml-5 text-xs text-slate-600 italic">No messages</p>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        ) : (
          <div className="flex flex-1 flex-col items-center justify-center text-slate-500">
            <Building2 size={32} className="mb-2 text-slate-600" />
            <p className="text-sm">Select an organization to view details</p>
          </div>
        )}
      </div>
    </div>
  )
}
