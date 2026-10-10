/** Tags, workspaces and the workspace board. */

import type { Conversation } from './conversations'

export interface Tag {
  id: number
  name: string
  color: string
  category: string | null
  is_default: boolean
  conversation_count?: number
}

export interface Workspace {
  id: number
  name: string
  emoji: string | null
  color: string
  match_mode: 'any' | 'all'
  view_mode: 'list' | 'kanban'
  position: number
  tag_ids: number[]
  member_count: number
  unallocated_count: number
  main_board_id?: number | null
}

export interface BoardColumn {
  tag_id: number
  name: string
  color: string
  position: number
  conversations: Conversation[]
}

export interface WorkspaceBoard {
  id: number
  name: string
  scope_tag_id: number | null
  scope_tag_name: string | null
  position: number
  unallocated_count: number
}

export interface BoardData {
  workspace: { id: number; name: string; emoji: string | null; color: string; view_mode: string }
  boards: WorkspaceBoard[]
  board_id: number | null
  columns: BoardColumn[]
  unallocated: Conversation[]
  backlog: { active: Conversation[]; cooling: Conversation[]; dormant: Conversation[] }
  classified_conversation_ids: number[]
}

export interface TagSuggestion {
  name: string
  color: string
  reason: string
}
