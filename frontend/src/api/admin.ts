/** Group administration (capability `admin_moderation`). */

import { request } from '../lib/http'
import type { AdminActionLog, AdminPermissions, GroupSettings, InviteLink } from './types'

export const promoteAdmin = (conversationId: number, identityId: number, permissions: AdminPermissions) =>
  request<{ success: boolean }>(`/api/admin/${conversationId}/promote`, {
    method: 'POST',
    body: JSON.stringify({ identity_id: identityId, permissions }),
  })

export const demoteAdmin = (conversationId: number, identityId: number) =>
  request<{ success: boolean }>(`/api/admin/${conversationId}/demote`, {
    method: 'POST',
    body: JSON.stringify({ identity_id: identityId }),
  })

export const banMember = (conversationId: number, identityId: number) =>
  request<{ success: boolean }>(`/api/admin/${conversationId}/ban`, {
    method: 'POST',
    body: JSON.stringify({ identity_id: identityId }),
  })

export const restrictMember = (conversationId: number, identityId: number, restrictions: Record<string, boolean | number | null>) =>
  request<{ success: boolean }>(`/api/admin/${conversationId}/restrict`, {
    method: 'POST',
    body: JSON.stringify({ identity_id: identityId, ...restrictions }),
  })

export const approveMember = (conversationId: number, identityId: number) =>
  request<{ success: boolean }>(`/api/admin/${conversationId}/approve`, {
    method: 'POST',
    body: JSON.stringify({ identity_id: identityId }),
  })

export const getInviteLinks = (conversationId: number) =>
  request<InviteLink[]>(`/api/admin/${conversationId}/invite-links`)

export const createInviteLink = (conversationId: number, options?: { expire_date?: number; usage_limit?: number; title?: string; request_needed?: boolean }) =>
  request<InviteLink>(`/api/admin/${conversationId}/invite-links`, {
    method: 'POST',
    body: JSON.stringify(options ?? {}),
  })

export const revokeInviteLink = (conversationId: number, link: string) =>
  request<{ success: boolean }>(`/api/admin/${conversationId}/invite-links/revoke`, {
    method: 'POST',
    body: JSON.stringify({ link }),
  })

export const getGroupSettings = (conversationId: number) =>
  request<GroupSettings>(`/api/admin/${conversationId}/settings`)

export const updateGroupSettings = (conversationId: number, settings: { title?: string; about?: string }) =>
  request<{ success: boolean; updated: string[] }>(`/api/admin/${conversationId}/settings`, {
    method: 'PUT',
    body: JSON.stringify(settings),
  })

export const getAdminActionLog = (conversationId: number, limit = 50) =>
  request<AdminActionLog[]>(`/api/admin/${conversationId}/action-log?limit=${limit}`)
