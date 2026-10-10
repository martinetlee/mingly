/** Tags and tag suggestions. */

import { request } from '../lib/http'
import type { Tag, TagSuggestion } from './types'

export const getTags = () =>
  request<Tag[]>('/api/tags')

export const createTag = (name: string, color: string = 'slate', category?: string) =>
  request<Tag>('/api/tags', {
    method: 'POST',
    body: JSON.stringify({ name, color, category: category || null }),
  })

export const updateTag = (tag_id: number, data: { name?: string; color?: string; category?: string | null }) =>
  request<Tag>(`/api/tags/${tag_id}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  })

export const deleteTag = (tag_id: number) =>
  request<{ success: boolean }>(`/api/tags/${tag_id}`, { method: 'DELETE' })

export const getConversationTags = (conversationId: number) =>
  request<Tag[]>(`/api/conversations/${conversationId}/tags`)

export const addTagToConversation = (conversationId: number, tag_id: number) =>
  request<{ success: boolean }>(`/api/conversations/${conversationId}/tags`, {
    method: 'POST',
    body: JSON.stringify({ tag_id }),
  })

export const removeTagFromConversation = (conversationId: number, tag_id: number) =>
  request<{ success: boolean }>(`/api/conversations/${conversationId}/tags/${tag_id}`, { method: 'DELETE' })

export const suggestTags = (conversationId: number) =>
  request<TagSuggestion[]>(`/api/conversations/${conversationId}/tags/suggest`, { method: 'POST' })
