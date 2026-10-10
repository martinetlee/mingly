/** Organizations and the user's own organizations. */

import { request } from '../lib/http'
import type { OrgDetail, OrgSummary } from './types'

export const getOrganizations = (search = '', signal?: AbortSignal) =>
  request<OrgSummary[]>(`/api/organizations?search=${encodeURIComponent(search)}`, { signal })

export const createOrganization = (name: string) =>
  request<OrgDetail>('/api/organizations', { method: 'POST', body: JSON.stringify({ name }) })

export const getOrganization = (orgId: number, signal?: AbortSignal) =>
  request<OrgDetail>(`/api/organizations/${orgId}`, { signal })

/** With `merge: true` a rename onto an existing name merges INTO that org — the result has the other org's id */
export const updateOrganization = (orgId: number, data: { name?: string; context?: string; merge?: boolean }) =>
  request<OrgDetail>(`/api/organizations/${orgId}`, { method: 'PUT', body: JSON.stringify(data) })

export const mergeOrganization = (orgId: number, intoId: number) =>
  request<OrgDetail & { moved_positions: number; merged_duplicate_positions: number }>(`/api/organizations/${orgId}/merge`, {
    method: 'POST', body: JSON.stringify({ into_id: intoId }),
  })

/** detach (default): members keep the position as history; delete: their positions here are removed */
export const deleteOrganization = (orgId: number, mode: 'detach' | 'delete' = 'detach') =>
  request<{ success: boolean; mode: string; positions_detached: number; positions_deleted: number }>(
    `/api/organizations/${orgId}?mode=${mode}`, { method: 'DELETE' },
  )

export const autoDiscoverOrgs = () =>
  request<{ created: number; total: number }>('/api/organizations/auto-discover', { method: 'POST' })

export const generateOrgContext = (orgId: number) =>
  request<OrgDetail>(`/api/organizations/${orgId}/generate-context`, { method: 'POST' })

export const getUserOrganizations = () =>
  request<{ organizations: string[] }>('/api/settings/prefs/user-organizations')

export const setUserOrganizations = (organizations: string[]) =>
  request<{ success: boolean; organizations: string[] }>('/api/settings/prefs/user-organizations', {
    method: 'PUT',
    body: JSON.stringify({ organizations }),
  })
