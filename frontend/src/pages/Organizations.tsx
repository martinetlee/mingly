import { useEffect, useState, useCallback, useRef } from 'react'
import { useLocation } from 'react-router-dom'
import { Plus, Search, Building2, Pencil, Trash2, Users, MessageSquare, FileText, RefreshCw, Sparkles, Loader2, ChevronDown, ChevronRight, UserMinus, GitMerge } from 'lucide-react'
import type { OrgSummary, OrgDetail } from '../api/types'
import { getOrganizations, getOrganization, createOrganization, updateOrganization, deleteOrganization, autoDiscoverOrgs, generateOrgContext, ApiError } from '../api/client'
import { useLatestQuery } from '../hooks/useLatestQuery'
import { useDebouncedValue } from '../hooks/useDebouncedQuery'
import { useAsyncAction } from '../hooks/useAsyncAction'
import { invalidateAllPeople } from '../hooks/usePersonSummary'
import { useConfirm } from '../components/ui'
import { errorMessage, reportError, toast } from '../lib/toast'
import { fmtDateTime, parseServerDate } from '../utils/time'
import PersonCard from '../components/PersonCard'
import MergeOrgDialog from '../components/MergeOrgDialog'

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

/** 409 `organization_exists` → the org that already holds the name */
function existingOrg(e: unknown): { id: number; name: string } | null {
  if (!(e instanceof ApiError) || e.status !== 409 || e.serverCode !== 'organization_exists') return null
  const id = Number(e.body?.existing_id)
  return Number.isFinite(id) ? { id, name: String(e.body?.existing_name ?? '') } : null
}

/** End dates are calendar dates: show them in UTC so they don't shift a day. */
function fmtEndDate(iso: string): string {
  const d = parseServerDate(iso)
  return d ? d.toLocaleDateString(undefined, { timeZone: 'UTC' }) : iso
}

export default function Organizations() {
  const location = useLocation()
  const confirm = useConfirm()
  const [search, setSearch] = useState('')
  const debouncedSearch = useDebouncedValue(search.trim(), 200)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [editingContext, setEditingContext] = useState(false)
  const [contextDraft, setContextDraft] = useState('')
  const [editingName, setEditingName] = useState(false)
  const [nameDraft, setNameDraft] = useState('')
  const [nameError, setNameError] = useState<string | null>(null)
  const [savingContext, setSavingContext] = useState(false)
  const [generating, setGenerating] = useState<Set<number>>(new Set())
  const [showFormerMembers, setShowFormerMembers] = useState(false)
  const [mergeSource, setMergeSource] = useState<OrgDetail | null>(null)
  const [sidebarWidth, setSidebarWidth] = useState(300)
  const dragging = useRef(false)
  const savingNameRef = useRef(false)

  // Latest-only list and detail, each bound to its own key
  const orgsQ = useLatestQuery(
    ['orgs', debouncedSearch],
    (signal) => getOrganizations(debouncedSearch, signal),
    { keepPreviousData: true },
  )
  const orgs = orgsQ.data ?? []
  const loading = orgsQ.data === undefined && orgsQ.loading
  const detailQ = useLatestQuery(
    ['org', selectedId],
    (signal) => getOrganization(selectedId as number, signal),
    { enabled: selectedId != null },
  )
  const selected = detailQ.data ?? null

  const selectedIdRef = useRef(selectedId)
  selectedIdRef.current = selectedId

  // Deep link: ?select=<orgId>
  useEffect(() => {
    const orgId = parseInt(new URLSearchParams(location.search).get('select') ?? '', 10)
    if (!isNaN(orgId)) setSelectedId(orgId)
  }, [location.search])

  // Per-org UI resets on every selection change
  useEffect(() => {
    setEditingContext(false)
    setEditingName(false)
    setNameError(null)
    setShowFormerMembers(false)
  }, [selectedId])

  /** Show `detail` (optionally switching to it) — never over a different org. */
  const showDetail = useCallback((detail: OrgDetail, select = false) => {
    if (select) {
      selectedIdRef.current = detail.id
      setSelectedId(detail.id)
    }
    if (selectedIdRef.current === detail.id) detailQ.setData(detail)
  }, [detailQ])

  const refreshList = useCallback(() => { void orgsQ.refetch() }, [orgsQ])

  const handleSelect = (org: OrgSummary) => setSelectedId(org.id)

  const handleCreate = async () => {
    const name = newName.trim()
    if (!name) return
    try {
      const detail = await createOrganization(name)
      showDetail(detail, true)
      setNewName('')
      setShowCreate(false)
      refreshList()
    } catch (e) {
      const existing = existingOrg(e)
      if (existing) {
        toast.info(`"${existing.name || name}" already exists — showing it`)
        setSelectedId(existing.id)
        setNewName('')
        setShowCreate(false)
      } else {
        reportError(e, 'Couldn\'t create organization')
      }
    }
  }

  const discover = useAsyncAction(async () => {
    const r = await autoDiscoverOrgs()
    refreshList()
    invalidateAllPeople()
    toast.success(r.created ? `Created ${r.created} organization${r.created !== 1 ? 's' : ''} from positions` : 'No new organizations found')
  }, { errorContext: 'Auto-discover failed' })

  const handleSaveContext = async () => {
    if (!selected || savingContext) return
    const id = selected.id
    setSavingContext(true)
    try {
      const updated = await updateOrganization(id, { context: contextDraft })
      showDetail(updated)
      if (selectedIdRef.current === id) setEditingContext(false)
      refreshList()
    } catch (e) {
      // Stay in edit mode with the draft intact
      reportError(e, 'Couldn\'t save context')
    } finally {
      setSavingContext(false)
    }
  }

  const handleSaveName = async () => {
    if (!selected || savingNameRef.current) return
    const id = selected.id
    const oldName = selected.name
    const name = nameDraft.trim()
    if (!name || name === oldName) { setEditingName(false); setNameError(null); return }
    savingNameRef.current = true
    setNameError(null)
    try {
      const updated = await updateOrganization(id, { name })
      showDetail(updated)
      if (selectedIdRef.current === id) setEditingName(false)
      refreshList()
      invalidateAllPeople()
    } catch (e) {
      const existing = existingOrg(e)
      if (!existing) {
        if (selectedIdRef.current === id) setNameError(errorMessage(e))
        return
      }
      // Renaming onto another org's name: offer to merge into it
      const merged = await confirm({
        title: `Merge into "${existing.name}"?`,
        message: `An organization named "${existing.name}" already exists. Merging moves every member and position of "${oldName}" into it and deletes "${oldName}". People with positions at both keep one.`,
        confirmLabel: 'Merge',
        onConfirm: async () => {
          const result = await updateOrganization(id, { name, merge: true })
          if (selectedIdRef.current === id) showDetail(result, true)
        },
      })
      if (merged) {
        setEditingName(false)
        refreshList()
        invalidateAllPeople()
        toast.success(`Merged "${oldName}" into "${existing.name}"`)
      }
    } finally {
      savingNameRef.current = false
    }
  }

  const handleGenerateContext = async () => {
    if (!selected || generating.has(selected.id)) return
    const id = selected.id
    setGenerating((prev) => new Set(prev).add(id))
    try {
      const updated = await generateOrgContext(id)
      // Applied only if this org is still the one on screen
      showDetail(updated)
      refreshList()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        toast.info('The context was edited while generating — your edit was kept')
        if (selectedIdRef.current === id) void detailQ.refetch()
      } else {
        reportError(e, 'Couldn\'t generate context')
      }
    } finally {
      setGenerating((prev) => { const next = new Set(prev); next.delete(id); return next })
    }
  }

  const handleDelete = async () => {
    if (!selected) return
    const org = selected
    const current = org.members.length
    const former = org.former_members.length
    const people = current + former
    const base = {
      title: `Delete "${org.name}"?`,
      message: people > 0
        ? `${current} current and ${former} former member${people !== 1 ? 's' : ''} have a position here. By default their positions are kept as history (no longer linked to an organization).`
        : 'No one has a position at this organization.',
      confirmLabel: 'Delete',
      destructive: true,
      onConfirm: (checked: boolean) => deleteOrganization(org.id, checked ? 'delete' : 'detach'),
    }
    const ok = people > 0
      ? (await confirm({ ...base, checkbox: { label: 'Also delete their positions at this organization' } })).confirmed
      : await confirm(base)
    if (!ok) return
    if (selectedIdRef.current === org.id) setSelectedId(null)
    refreshList()
    invalidateAllPeople()
    toast.success(`Deleted "${org.name}"`)
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
              onClick={() => discover.run()}
              disabled={discover.pending}
              title="Auto-discover from contacts"
              className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-700 transition-colors disabled:opacity-50"
            >
              <RefreshCw size={13} className={discover.pending ? 'animate-spin' : ''} />
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
          ) : orgsQ.error != null && orgs.length === 0 ? (
            <div className="px-4 py-6 text-center">
              <p className="text-sm text-rose-400">Couldn't load organizations: {errorMessage(orgsQ.error)}</p>
              <button onClick={refreshList} className="mt-2 text-xs text-indigo-400 hover:text-indigo-300">Retry</button>
            </div>
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
                selected={selectedId === org.id}
                onClick={() => handleSelect(org)}
              />
            ))
          )}
        </div>
      </div>

      {/* Detail */}
      <div className="flex flex-1 flex-col overflow-hidden bg-slate-900">
        {selectedId != null && !selected ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-2 text-sm text-slate-500">
            {detailQ.error != null ? (
              <>
                <p className="text-rose-400">Couldn't load organization: {errorMessage(detailQ.error)}</p>
                <button onClick={() => detailQ.refetch()} className="text-xs text-indigo-400 hover:text-indigo-300">Retry</button>
              </>
            ) : (
              <Loader2 size={16} className="animate-spin" />
            )}
          </div>
        ) : selected ? (
          <div className="flex-1 overflow-y-auto px-6 py-6 max-w-3xl">
            {/* Header */}
            <div className="flex items-start justify-between mb-6">
              <div className="flex items-center gap-3">
                <div className="flex h-14 w-14 items-center justify-center rounded-xl bg-slate-700 text-slate-300">
                  <Building2 size={24} />
                </div>
                <div>
                  {editingName ? (
                    <div>
                      <input
                        autoFocus
                        aria-label="Organization name"
                        value={nameDraft}
                        onChange={(e) => { setNameDraft(e.target.value); setNameError(null) }}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') handleSaveName()
                          if (e.key === 'Escape') { e.stopPropagation(); setEditingName(false); setNameError(null) }
                        }}
                        onBlur={() => { if (!nameError) handleSaveName() }}
                        className={`bg-slate-700 border rounded px-2 py-0.5 text-lg font-bold text-gray-100 outline-none focus:border-indigo-500 ${nameError ? 'border-rose-500/60' : 'border-slate-600'}`}
                      />
                      {nameError && <p role="alert" className="mt-1 text-xs text-rose-400">{nameError}</p>}
                    </div>
                  ) : (
                    <h1
                      className="text-lg font-bold text-gray-100 cursor-pointer hover:text-indigo-300 transition-colors group inline-flex items-center gap-1.5"
                      onClick={() => { setEditingName(true); setNameDraft(selected.name); setNameError(null) }}
                    >
                      {selected.name}
                      <Pencil size={12} className="opacity-0 group-hover:opacity-100 text-slate-500 transition-opacity" />
                    </h1>
                  )}
                  <p className="text-xs text-slate-500">{selected.members.length} member{selected.members.length !== 1 ? 's' : ''}</p>
                </div>
              </div>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => setMergeSource(selected)}
                  title="Merge into another organization"
                  className="rounded p-1.5 text-slate-500 hover:text-indigo-300 transition-colors"
                >
                  <GitMerge size={14} />
                </button>
                <button
                  onClick={handleDelete}
                  title="Delete organization"
                  className="rounded p-1.5 text-slate-500 hover:text-rose-400 transition-colors"
                >
                  <Trash2 size={14} />
                </button>
              </div>
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
                      disabled={generating.has(selected.id)}
                      title="Generate context with AI"
                      className="text-xs text-violet-400 hover:text-violet-300 transition-colors flex items-center gap-1 disabled:opacity-40"
                    >
                      {generating.has(selected.id) ? <Loader2 size={10} className="animate-spin" /> : <Sparkles size={10} />}
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
                      disabled={savingContext}
                      className="rounded bg-indigo-500 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-600 transition-colors disabled:opacity-50"
                    >
                      {savingContext ? 'Saving…' : 'Save'}
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
                <p className="text-xs text-slate-600 italic">No members. Add positions to people referencing this organization.</p>
              ) : (
                <div className="flex flex-col gap-1">
                  {selected.members.map((m) => (
                    <PersonCard
                      key={m.person_id}
                      personId={m.person_id}
                      name={m.name}
                      username={m.username}
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
                        key={m.person_id}
                        personId={m.person_id}
                        name={m.name}
                        username={m.username}
                        muted
                        extraBadge={
                          <div className="flex items-center gap-2">
                            {m.ended_at && (
                              <span className="text-[10px] text-slate-600">
                                left {fmtEndDate(m.ended_at)}
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
            {selected.related_conversations.length > 0 && (
              <div className="rounded-xl bg-slate-800 border border-slate-700/50 p-4">
                <h3 className="text-xs font-medium uppercase tracking-wider text-slate-500 flex items-center gap-1.5 mb-3">
                  <MessageSquare size={11} />
                  Related Groups ({selected.related_conversations.length})
                </h3>
                <div className="flex flex-col gap-2">
                  {selected.related_conversations.map((rc) => (
                    <div key={rc.conversation_id} className="rounded-lg bg-slate-700/30 border border-slate-700 p-3">
                      <div className="flex items-center gap-2 mb-1">
                        <MessageSquare size={12} className="text-slate-500" />
                        <span className="text-sm font-medium text-gray-200">{rc.conversation_title}</span>
                        <span className="text-[10px] text-slate-500 rounded bg-slate-700 px-1">{rc.conversation_kind.replace('_', ' ')}</span>
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
                              {fmtDateTime(rc.last_message.date)}
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

      {mergeSource && (
        <MergeOrgDialog
          source={mergeSource}
          onClose={() => setMergeSource(null)}
          onMerged={(into, moved) => {
            const from = mergeSource.name
            setMergeSource(null)
            if (selectedIdRef.current === mergeSource.id) showDetail(into, true)
            refreshList()
            invalidateAllPeople()
            toast.success(`Merged "${from}" into "${into.name}" (${moved} position${moved !== 1 ? 's' : ''} moved)`)
          }}
        />
      )}
    </div>
  )
}
