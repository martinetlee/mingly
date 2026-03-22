import { useState, useCallback } from 'react'
import {
  getCloudProfile,
  createCloudOrg,
  joinCloudOrg,
  getCloudOrgMembers,
  getCloudOrgPending,
  updateCloudOrgMember,
  removeCloudOrgMember,
} from '../api/client'
import { useCloudOrgs, type CloudOrg } from '../hooks/useCloudOrgs'

interface OrgMember {
  user_id: string
  email: string
  role: string
  status: string
  joined_at: string
}

interface PendingRequest {
  user_id: string
  email: string
  requested_at: string
}

interface VerifiedEmail {
  id: string
  email: string
  domain: string
  verified_at: string
}

type View = 'list' | 'detail' | 'create'

export default function OrgManagement() {
  const { orgs, loading, error: fetchError, refetch: fetchOrgs } = useCloudOrgs()
  const [error, setError] = useState<string | null>(fetchError)
  const [view, setView] = useState<View>('list')

  // Create org state
  const [orgName, setOrgName] = useState('')
  const [orgDomain, setOrgDomain] = useState('')
  const [verifiedDomains, setVerifiedDomains] = useState<string[]>([])
  const [creating, setCreating] = useState(false)

  // Detail view state
  const [selectedOrg, setSelectedOrg] = useState<CloudOrg | null>(null)
  const [members, setMembers] = useState<OrgMember[]>([])
  const [pending, setPending] = useState<PendingRequest[]>([])
  const [membersLoading, setMembersLoading] = useState(false)
  const [actionLoading, setActionLoading] = useState<string | null>(null)

  const fetchVerifiedDomains = useCallback(async () => {
    try {
      const profile = await getCloudProfile()
      const domains = [...new Set(profile.emails.map((e: VerifiedEmail) => e.domain))]
      setVerifiedDomains(domains)
      if (domains.length > 0 && !orgDomain) {
        setOrgDomain(domains[0])
      }
    } catch {
      // Ignore — domains are optional hints
    }
  }, [orgDomain])

  const openCreateView = useCallback(async () => {
    setView('create')
    await fetchVerifiedDomains()
  }, [fetchVerifiedDomains])

  const handleCreateOrg = useCallback(async () => {
    if (!orgName.trim() || !orgDomain.trim()) return
    setCreating(true)
    setError(null)
    try {
      await createCloudOrg(orgName.trim(), orgDomain.trim())
      setOrgName('')
      setOrgDomain('')
      setView('list')
      await fetchOrgs()
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to create organization'
      setError(msg)
    } finally {
      setCreating(false)
    }
  }, [orgName, orgDomain, fetchOrgs])

  const handleJoinOrg = useCallback(async (orgId: string) => {
    setActionLoading(orgId)
    setError(null)
    try {
      await joinCloudOrg(orgId)
      await fetchOrgs()
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to join organization'
      setError(msg)
    } finally {
      setActionLoading(null)
    }
  }, [fetchOrgs])

  const openOrgDetail = useCallback(async (org: CloudOrg) => {
    setSelectedOrg(org)
    setView('detail')
    setMembersLoading(true)
    try {
      const [memberData, pendingData] = await Promise.all([
        getCloudOrgMembers(org.id),
        org.role === 'admin' ? getCloudOrgPending(org.id) : Promise.resolve([]),
      ])
      setMembers(Array.isArray(memberData) ? memberData : [])
      setPending(Array.isArray(pendingData) ? pendingData : [])
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to load members'
      setError(msg)
    } finally {
      setMembersLoading(false)
    }
  }, [])

  const handleApproveMember = useCallback(async (orgId: string, userId: string) => {
    setActionLoading(userId)
    try {
      await updateCloudOrgMember(orgId, userId, { status: 'active' })
      // Refresh
      if (selectedOrg) await openOrgDetail(selectedOrg)
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to approve member'
      setError(msg)
    } finally {
      setActionLoading(null)
    }
  }, [selectedOrg, openOrgDetail])

  const handleDenyMember = useCallback(async (orgId: string, userId: string) => {
    setActionLoading(userId)
    try {
      await updateCloudOrgMember(orgId, userId, { status: 'revoked' })
      if (selectedOrg) await openOrgDetail(selectedOrg)
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to deny member'
      setError(msg)
    } finally {
      setActionLoading(null)
    }
  }, [selectedOrg, openOrgDetail])

  const handleUpdateRole = useCallback(async (orgId: string, userId: string, role: string) => {
    setActionLoading(userId)
    try {
      await updateCloudOrgMember(orgId, userId, { role })
      if (selectedOrg) await openOrgDetail(selectedOrg)
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to update role'
      setError(msg)
    } finally {
      setActionLoading(null)
    }
  }, [selectedOrg, openOrgDetail])

  const handleRemoveMember = useCallback(async (orgId: string, userId: string) => {
    setActionLoading(userId)
    try {
      await removeCloudOrgMember(orgId, userId)
      if (selectedOrg) await openOrgDetail(selectedOrg)
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to remove member'
      setError(msg)
    } finally {
      setActionLoading(null)
    }
  }, [selectedOrg, openOrgDetail])

  if (loading) {
    return (
      <section className="mb-6">
        <h3 className="mb-2 text-sm font-medium text-slate-300">Organizations</h3>
        <p className="text-xs text-slate-500">Loading...</p>
      </section>
    )
  }

  return (
    <section className="mb-6">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-medium text-slate-300">Organizations</h3>
        {view !== 'list' && (
          <button
            onClick={() => { setView('list'); setSelectedOrg(null); setError(null) }}
            className="text-xs text-blue-400 hover:text-blue-300"
          >
            Back to list
          </button>
        )}
      </div>
      <p className="mb-3 text-xs text-slate-400">
        Create or join organizations based on your verified email domains.
      </p>

      {error && (
        <div className="mb-3 rounded-lg bg-red-900/40 border border-red-700/50 px-3 py-2 text-xs text-red-300">
          {error}
        </div>
      )}

      {/* ── List View ── */}
      {view === 'list' && (
        <>
          {orgs.length > 0 ? (
            <div className="space-y-2 mb-3">
              {orgs.map((org) => (
                <div
                  key={org.id}
                  className="flex items-center justify-between rounded-lg border border-slate-700 bg-slate-800/50 px-3 py-2.5 cursor-pointer hover:bg-slate-800"
                  onClick={() => openOrgDetail(org)}
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-sm font-medium text-slate-200">{org.name}</span>
                      <span className="shrink-0 rounded bg-slate-700 px-1.5 py-0.5 text-[10px] text-slate-400">
                        {org.role}
                      </span>
                      {org.status === 'pending' && (
                        <span className="shrink-0 rounded bg-amber-900/50 border border-amber-700/50 px-1.5 py-0.5 text-[10px] text-amber-300">
                          PENDING
                        </span>
                      )}
                    </div>
                    <div className="mt-0.5 flex items-center gap-2 text-[11px] text-slate-500">
                      <span>{org.domain}</span>
                      <span>{org.member_count} member{org.member_count !== 1 ? 's' : ''}</span>
                    </div>
                  </div>
                  <svg className="h-4 w-4 text-slate-500" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 4.5l7.5 7.5-7.5 7.5" />
                  </svg>
                </div>
              ))}
            </div>
          ) : (
            <p className="mb-3 text-xs text-slate-500 italic">
              No organizations yet. Create one or join an existing org.
            </p>
          )}

          <button
            onClick={openCreateView}
            className="rounded-lg border border-dashed border-slate-600 px-4 py-2 text-xs font-medium text-slate-400 hover:border-slate-500 hover:text-slate-300"
          >
            + Create Organization
          </button>
        </>
      )}

      {/* ── Create View ── */}
      {view === 'create' && (
        <div className="rounded-lg border border-slate-700 bg-slate-800 p-4">
          <h4 className="mb-3 text-sm font-medium text-slate-200">Create Organization</h4>

          <label className="mb-1 block text-xs font-medium text-slate-400">
            Organization name
          </label>
          <input
            type="text"
            value={orgName}
            onChange={(e) => setOrgName(e.target.value)}
            placeholder="Acme Corp"
            className="mb-3 w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-sm text-slate-100 outline-none placeholder:text-slate-500 focus:border-blue-500"
          />

          <label className="mb-1 block text-xs font-medium text-slate-400">
            Domain (from your verified emails)
          </label>
          {verifiedDomains.length > 0 ? (
            <select
              value={orgDomain}
              onChange={(e) => setOrgDomain(e.target.value)}
              className="mb-4 w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-sm text-slate-100 outline-none focus:border-blue-500"
            >
              {verifiedDomains.map((d) => (
                <option key={d} value={d}>{d}</option>
              ))}
            </select>
          ) : (
            <input
              type="text"
              value={orgDomain}
              onChange={(e) => setOrgDomain(e.target.value)}
              placeholder="acme.com"
              className="mb-4 w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-sm text-slate-100 outline-none placeholder:text-slate-500 focus:border-blue-500"
            />
          )}

          <div className="flex gap-2">
            <button
              onClick={handleCreateOrg}
              disabled={!orgName.trim() || !orgDomain.trim() || creating}
              className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {creating ? 'Creating...' : 'Create'}
            </button>
            <button
              onClick={() => { setView('list'); setOrgName(''); setOrgDomain('') }}
              className="rounded-lg border border-slate-600 px-4 py-2 text-sm text-slate-400 hover:bg-slate-700"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* ── Detail View ── */}
      {view === 'detail' && selectedOrg && (
        <div className="rounded-lg border border-slate-700 bg-slate-800 p-4">
          <div className="mb-4">
            <h4 className="text-sm font-medium text-slate-200">{selectedOrg.name}</h4>
            <div className="mt-0.5 flex items-center gap-2 text-xs text-slate-500">
              <span>{selectedOrg.domain}</span>
              <span>Your role: <span className="text-slate-400">{selectedOrg.role}</span></span>
            </div>
          </div>

          {membersLoading ? (
            <p className="text-xs text-slate-500">Loading members...</p>
          ) : (
            <>
              {/* Pending requests (admin only) */}
              {selectedOrg.role === 'admin' && pending.length > 0 && (
                <div className="mb-4">
                  <h5 className="mb-2 text-xs font-medium text-amber-400">
                    Pending Requests ({pending.length})
                  </h5>
                  <div className="space-y-1.5">
                    {pending.map((p) => (
                      <div
                        key={p.user_id}
                        className="flex items-center justify-between rounded-lg border border-amber-700/30 bg-amber-900/20 px-3 py-2"
                      >
                        <div>
                          <span className="text-sm text-slate-200">{p.email}</span>
                          <span className="ml-2 text-[11px] text-slate-500">
                            {new Date(p.requested_at).toLocaleDateString()}
                          </span>
                        </div>
                        <div className="flex gap-1.5">
                          <button
                            onClick={() => handleApproveMember(selectedOrg.id, p.user_id)}
                            disabled={actionLoading === p.user_id}
                            className="rounded bg-green-700 px-2.5 py-1 text-xs font-medium text-white hover:bg-green-600 disabled:opacity-50"
                          >
                            Approve
                          </button>
                          <button
                            onClick={() => handleDenyMember(selectedOrg.id, p.user_id)}
                            disabled={actionLoading === p.user_id}
                            className="rounded bg-red-700 px-2.5 py-1 text-xs font-medium text-white hover:bg-red-600 disabled:opacity-50"
                          >
                            Deny
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Members list */}
              <h5 className="mb-2 text-xs font-medium text-slate-400">
                Members ({members.length})
              </h5>
              <div className="space-y-1.5">
                {members.map((m) => (
                  <div
                    key={m.user_id}
                    className="flex items-center justify-between rounded-lg border border-slate-700/50 bg-slate-800/50 px-3 py-2"
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm text-slate-200">{m.email}</span>
                        <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${
                          m.role === 'admin'
                            ? 'bg-blue-900/50 border border-blue-700/50 text-blue-300'
                            : 'bg-slate-700 text-slate-400'
                        }`}>
                          {m.role}
                        </span>
                      </div>
                      <span className="text-[11px] text-slate-500">
                        Joined {new Date(m.joined_at).toLocaleDateString()}
                      </span>
                    </div>

                    {/* Admin actions */}
                    {selectedOrg.role === 'admin' && (
                      <div className="flex items-center gap-1.5">
                        {m.role === 'member' && (
                          <button
                            onClick={() => handleUpdateRole(selectedOrg.id, m.user_id, 'admin')}
                            disabled={actionLoading === m.user_id}
                            className="rounded px-2 py-1 text-xs text-blue-400 hover:bg-blue-900/30 disabled:opacity-50"
                            title="Promote to admin"
                          >
                            Promote
                          </button>
                        )}
                        {m.role === 'admin' && (
                          <button
                            onClick={() => handleUpdateRole(selectedOrg.id, m.user_id, 'member')}
                            disabled={actionLoading === m.user_id}
                            className="rounded px-2 py-1 text-xs text-slate-400 hover:bg-slate-700 disabled:opacity-50"
                            title="Demote to member"
                          >
                            Demote
                          </button>
                        )}
                        <button
                          onClick={() => handleRemoveMember(selectedOrg.id, m.user_id)}
                          disabled={actionLoading === m.user_id}
                          className="rounded px-2 py-1 text-xs text-red-400 hover:bg-red-900/30 disabled:opacity-50"
                          title="Remove member"
                        >
                          Remove
                        </button>
                      </div>
                    )}
                  </div>
                ))}
                {members.length === 0 && (
                  <p className="text-xs text-slate-500 italic">No members yet.</p>
                )}
              </div>
            </>
          )}
        </div>
      )}
    </section>
  )
}
