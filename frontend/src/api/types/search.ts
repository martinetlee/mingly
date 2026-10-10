/** Global search. */

import type { Conversation } from './conversations'
import type { Person } from './people'

export interface SearchMessageResult {
  message_id: number
  conversation_id: number
  conversation_title: string
  sender_name: string | null
  subject?: string | null
  text: string
  date: string
}

export interface SearchOrgResult {
  id: number
  name: string
  context: string | null
}

export interface GlobalSearchResults {
  messages: SearchMessageResult[]
  conversations: Conversation[]
  people: Person[]
  organizations: SearchOrgResult[]
}
