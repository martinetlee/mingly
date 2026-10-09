// People linking: one human's identities across channels in one person.
// Merge suggestions, merges (with undo) and detaching an identity
// (backend/api/people.py, backend/people/linking.py).

import { request } from './client'
import type { ChannelName, Person } from './types'

export type MergeSuggestionReason =
  | 'same_phone'
  | 'same_email'
  | 'same_name_shared_group'
  | 'username_matches_email'

/** Two people who may be the same human (never merged automatically) */
export interface MergeSuggestion {
  id: number
  reason: MergeSuggestionReason | string
  /** 0..1, how likely the pair is one human */
  score: number
  created_at: string | null
  person_a: Person
  person_b: Person
}

/** A merge into a person that can still be undone */
export interface PersonMergeRecord {
  id: number
  into_person_id: number
  /** The removed person's former id */
  from_person_id: number
  from_name: string | null
  /** The identities that moved with the merge */
  identities: { id: number; channel: ChannelName; label: string | null }[]
  /** Linked automatically (shared phone / email) rather than by the user */
  automatic: boolean
  created_at: string | null
  undone_at: string | null
}

export type MergeResult = Person & { merge_id: number }

const post = <T>(path: string, body?: object) =>
  request<T>(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined })

export const getMergeSuggestions = (signal?: AbortSignal) =>
  request<MergeSuggestion[]>('/api/people/merge-suggestions', { signal })

export const getPersonMergeSuggestions = (personId: number, signal?: AbortSignal) =>
  request<MergeSuggestion[]>(`/api/people/${personId}/merge-suggestions`, { signal })

/** Recompute suggestions across everyone */
export const refreshMergeSuggestions = () =>
  post<{ created: number }>('/api/people/merge-suggestions/refresh')

export const dismissMergeSuggestion = (suggestionId: number) =>
  post<{ success: boolean }>(`/api/people/merge-suggestions/${suggestionId}/dismiss`)

export const getPersonMerges = (personId: number, signal?: AbortSignal) =>
  request<PersonMergeRecord[]>(`/api/people/${personId}/merges`, { signal })

/** Merge `otherPersonId` into `personId` (which survives) */
export const mergePerson = (personId: number, otherPersonId: number) =>
  post<MergeResult>(`/api/people/${personId}/merge`, { other_person_id: otherPersonId })

/** Undo a merge; resolves to the restored person (new id) */
export const undoPersonMerge = (mergeId: number) =>
  post<Person>(`/api/people/merges/${mergeId}/undo`)

/** Move one identity to a new person; resolves to that new person */
export const detachIdentity = (personId: number, identityId: number) =>
  post<Person>(`/api/people/${personId}/identities/${identityId}/detach`)

const REASONS: Record<string, string> = {
  same_phone: 'Same phone number',
  same_email: 'Same email address',
  same_name_shared_group: 'Same name in a shared group',
  username_matches_email: 'Username matches an email address',
}

export const suggestionReasonLabel = (reason: string): string => REASONS[reason] ?? reason.replace(/_/g, ' ')

/** The other person of a suggestion involving `personId` */
export const suggestionOther = (s: MergeSuggestion, personId: number): Person =>
  s.person_a.id === personId ? s.person_b : s.person_a
