/** People (the CRM record), their identities, positions and notes. */

import type { ChannelName } from './channels'

export interface ContactPosition {
  id: number
  person_id?: number
  /** null = detached (org deleted with mode=detach; the name is kept as history) */
  organization_id: number | null
  organization: string
  title: string | null
  is_active: boolean
  ended_at: string | null
}

export type RelationshipStatus = 'new' | 'active' | 'nurturing' | 'stalled' | 'cold' | 'needs_reply'

/** One handle of a person on one channel (backend identity_out) */
export interface Identity {
  id: number
  person_id: number
  channel: ChannelName
  /** The channel's own id (Telegram user id, email address…) — data, never a key */
  external_id: string
  display_name: string | null
  first_name?: string | null
  last_name?: string | null
  username: string | null
  phone: string | null
  bio?: string | null
  avatar_url: string | null
  is_contact: boolean
  is_bot?: boolean
  is_deleted?: boolean
  is_self?: boolean
}

/** The CRM entity (backend person_out); CRM routes add positions & relationship insight */
export interface Person {
  id: number
  name: string
  /** User-set override; null = derived from identities */
  display_name: string | null
  avatar_url?: string | null
  /** In any channel's address book */
  is_contact: boolean
  is_important: boolean
  is_self: boolean
  identities: Identity[]
  twitter_handle: string | null
  linkedin_url: string | null
  notes: string | null
  relationship_status: RelationshipStatus | null
  relationship_status_manual: boolean
  last_interaction_at: string | null
  last_outgoing_at: string | null
  last_incoming_at: string | null
  positions?: ContactPosition[]
  primary_position?: { title: string | null; organization: string; organization_id?: number | null } | null
  note_count?: number
  /** GET /api/people/{id} only: the person's direct conversations on every channel */
  direct_conversation_ids?: number[]
  status_explanation?: string
  recommended_action?: string
  status_source?: 'auto' | 'manual'
  attention_reason?: string
  days_since_interaction?: number | null
  days_since_outgoing?: number | null
  days_since_incoming?: number | null
}

/** GET /api/people — server-side filtered page */
export interface PeoplePage {
  items: Person[]
  total: number
  limit: number
  offset: number
  /** Matches of `search` alone per status ("none" = no status) */
  status_counts: Record<string, number>
  /** Important people matching `search` (Important tab badge) */
  important_count?: number
}

export type PersonNoteKind = 'context' | 'due_diligence' | 'risk' | 'relationship' | 'todo'

export interface PersonNoteCitation {
  conversation_id: number
  message_id: number
}

export interface PersonNote {
  id: number
  person_id: number
  kind: PersonNoteKind
  title: string
  /** Markdown */
  body: string
  citations: PersonNoteCitation[]
  author: 'agent' | 'user'
  agent_label: string | null
  created_at: string
  updated_at: string
}

export interface PersonNoteInput {
  kind: PersonNoteKind
  title: string
  body: string
  citations?: PersonNoteCitation[]
}
