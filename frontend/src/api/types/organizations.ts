/** Organizations. */

import type { ConversationKind } from './conversations'

export interface OrgMember {
  person_id: number
  position_id: number
  name: string
  username: string | null
  title: string | null
  ended_at: string | null
}

export interface OrgRelatedChat {
  conversation_id: number
  conversation_title: string
  conversation_kind: ConversationKind
  last_message: {
    text: string | null
    sender_name: string | null
    date: string | null
  } | null
}

export interface OrgSummary {
  id: number
  name: string
  context: string | null
  member_count: number
  former_member_count: number
}

export interface OrgDetail {
  id: number
  name: string
  context: string | null
  members: OrgMember[]
  former_members: OrgMember[]
  related_conversations: OrgRelatedChat[]
}
