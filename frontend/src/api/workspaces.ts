/** Workspaces and the workspace board. */

import { request } from '../lib/http'
import type { BoardColumn, BoardData, Workspace } from './types'

export const getWorkspaces = () => request<Workspace[]>('/api/workspaces')

export const createWorkspace = (data: { name: string; emoji?: string | null; color?: string; tag_ids: number[]; view_mode?: string }) =>
  request<Workspace>('/api/workspaces', { method: 'POST', body: JSON.stringify(data) })

export const updateWorkspace = (id: number, data: { name?: string; emoji?: string | null; color?: string; tag_ids?: number[]; view_mode?: string }) =>
  request<{ status: string }>(`/api/workspaces/${id}`, { method: 'PUT', body: JSON.stringify(data) })

export const deleteWorkspace = (id: number) =>
  request<void>(`/api/workspaces/${id}`, { method: 'DELETE' })

export const getBoard = (workspaceId: number, boardId?: number) =>
  request<BoardData>(`/api/workspaces/${workspaceId}/board${boardId != null ? `?board_id=${boardId}` : ''}`)

export const addWorkspaceBoard = (workspaceId: number, name: string, scopeTagName?: string) =>
  request<{ id: number; name: string }>(`/api/workspaces/${workspaceId}/boards`, {
    method: 'POST', body: JSON.stringify({ name, scope_tag_name: scopeTagName ?? null }),
  })

export const updateWorkspaceBoard = (
  workspaceId: number, boardId: number,
  data: { name?: string; scope_tag_name?: string; clear_scope?: boolean; position?: number },
) =>
  request<{ status: string }>(`/api/workspaces/${workspaceId}/boards/${boardId}`, { method: 'PUT', body: JSON.stringify(data) })

// Cards on the board's columns always return to backlog (the server deletes
// the columns' stage tags); membership and stream tags are never stripped.
export const deleteWorkspaceBoard = (workspaceId: number, boardId: number) =>
  request<{ status: string }>(`/api/workspaces/${workspaceId}/boards/${boardId}`, { method: 'DELETE' })

export const addBoardColumn = (workspaceId: number, boardId: number, name: string, color?: string) =>
  request<BoardColumn>(`/api/workspaces/${workspaceId}/boards/${boardId}/columns`, { method: 'POST', body: JSON.stringify({ name, color }) })

export const updateBoardColumn = (workspaceId: number, boardId: number, tagId: number, data: { name?: string; color?: string; position?: number }) =>
  request<{ status: string }>(`/api/workspaces/${workspaceId}/boards/${boardId}/columns/${tagId}`, { method: 'PUT', body: JSON.stringify(data) })

export const deleteBoardColumn = (workspaceId: number, boardId: number, tagId: number) =>
  request<{ status: string }>(`/api/workspaces/${workspaceId}/boards/${boardId}/columns/${tagId}`, { method: 'DELETE' })

/** Atomic left-to-right column order (every column of the board); returns the updated board */
export const reorderBoardColumns = (workspaceId: number, boardId: number, tagIds: number[]) =>
  request<BoardData>(`/api/workspaces/${workspaceId}/boards/${boardId}/columns/order`, { method: 'PUT', body: JSON.stringify({ tag_ids: tagIds }) })

/** Returns the committed board for `boardId` — the newest snapshot */
export const moveBoardConversation = (workspaceId: number, boardId: number, conversationId: number, toTagId: number | null) =>
  request<BoardData>(`/api/workspaces/${workspaceId}/move`, { method: 'POST', body: JSON.stringify({ board_id: boardId, conversation_id: conversationId, to_tag_id: toTagId }) })
