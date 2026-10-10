/** People (the CRM record), positions and notes. */

import { request } from '../lib/http'
import type { ContactPosition, Conversation, PeoplePage, Person, PersonNote, PersonNoteInput } from './types'

/** Address-book sync of every connector that has one */
export const syncPeople = () =>
  request<{ imported: number; total: number }>('/api/people/sync', { method: 'POST' })

export const getPeoplePage = (
  params: { search?: string; status?: string; important?: boolean; contactsOnly?: boolean; limit?: number; offset?: number } = {},
  signal?: AbortSignal,
) => {
  const qs = new URLSearchParams({
    search: params.search ?? '',
    limit: String(params.limit ?? 100),
    offset: String(params.offset ?? 0),
  })
  if (params.status && params.status !== 'all') qs.set('status', params.status)
  if (params.important) qs.set('important', 'true')
  if (params.contactsOnly) qs.set('contacts_only', 'true')
  return request<PeoplePage>(`/api/people?${qs}`, { signal })
}

/** First page of matches only — use getPeoplePage for lists */
export const getPeople = (search = '', limit = 100, offset = 0, contactsOnly = false) =>
  getPeoplePage({ search, limit, offset, contactsOnly }).then((page) => page.items)

/** Local data only (works offline) */
export const getPerson = (personId: number, signal?: AbortSignal) =>
  request<Person>(`/api/people/${personId}`, { signal })

/** The person's direct conversations on every channel, most recent first */
export const getPersonConversations = (personId: number, signal?: AbortSignal) =>
  request<Conversation[]>(`/api/people/${personId}/conversations`, { signal })

export const updatePerson = (personId: number, data: { display_name?: string | null; twitter_handle?: string; linkedin_url?: string; notes?: string; is_important?: boolean }) =>
  request<Person>(`/api/people/${personId}`, { method: 'PUT', body: JSON.stringify(data) })

export const setPersonImportant = (personId: number, is_important: boolean) =>
  request<Person>(`/api/people/${personId}/important`, {
    method: 'POST',
    body: JSON.stringify({ is_important }),
  })

export const addPosition = (
  personId: number,
  organization: string,
  title?: string,
  is_active = true,
  ended_at?: string,
) =>
  request<ContactPosition>(`/api/people/${personId}/positions`, {
    method: 'POST',
    body: JSON.stringify({ organization, title: title || null, is_active, ended_at: ended_at || null }),
  })

/** `ended_at: ""` clears the end date */
export const updatePosition = (
  personId: number,
  positionId: number,
  data: { title?: string; is_active?: boolean; ended_at?: string | null },
) =>
  request<ContactPosition>(`/api/people/${personId}/positions/${positionId}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  })

export const removePosition = (personId: number, positionId: number) =>
  request<{ success: boolean }>(`/api/people/${personId}/positions/${positionId}`, { method: 'DELETE' })

export const getPositions = (personId: number, signal?: AbortSignal) =>
  request<ContactPosition[]>(`/api/people/${personId}/positions`, { signal })

export const setRelationshipStatus = (personId: number, status: string) =>
  request<Person>(`/api/people/${personId}/relationship-status`, { method: 'POST', body: JSON.stringify({ status }) })

export const resetRelationshipStatus = (personId: number) =>
  request<Person>(`/api/people/${personId}/relationship-status/auto`, { method: 'POST' })

export const refreshRelationshipStatuses = () =>
  request<{ refreshed: number }>('/api/people/refresh-statuses', { method: 'POST' })

export const getPersonNotes = (personId: number, signal?: AbortSignal) =>
  request<import('./types').PersonNote[]>(`/api/people/${personId}/notes`, { signal })

export const createPersonNote = (personId: number, note: import('./types').PersonNoteInput) =>
  request<import('./types').PersonNote>(`/api/people/${personId}/notes`, {
    method: 'POST',
    body: JSON.stringify(note),
  })

export const updatePersonNote = (personId: number, noteId: number, note: Partial<import('./types').PersonNoteInput>) =>
  request<import('./types').PersonNote>(`/api/people/${personId}/notes/${noteId}`, {
    method: 'PUT',
    body: JSON.stringify(note),
  })

export const deletePersonNote = (personId: number, noteId: number) =>
  request<{ success: boolean } | undefined>(`/api/people/${personId}/notes/${noteId}`, { method: 'DELETE' })
