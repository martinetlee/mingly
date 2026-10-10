/**
 * Tag helpers shared by the chat info panel: create-or-reuse a tag by name,
 * and readable errors for the tag API's 409s.
 */
import type { Tag } from '../../api/types'
import { ApiError, createTag, getTags } from '../../api/client'
import { errorMessage } from '../../lib/toast'

/** The server's uniqueness rule: case- and whitespace-insensitive */
export const normTagName = (name: string) => name.trim().replace(/\s+/g, ' ').toLowerCase()

/**
 * Create a tag, or reuse the existing one when the name is taken (409).
 * Any other failure (validation, network, …) is thrown to the caller.
 */
export async function createOrFindTag(name: string, color: string, known: Tag[]): Promise<{ tag: Tag; created: boolean }> {
  try {
    return { tag: await createTag(name, color), created: true }
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 409)) throw e
    const existingId = typeof e.body?.existing_id === 'number' ? e.body.existing_id : null
    const matches = (t: Tag) => (existingId != null ? t.id === existingId : normTagName(t.name) === normTagName(name))
    const found = known.find(matches) ?? (await getTags()).find(matches)
    if (!found) throw e
    return { tag: found, created: false }
  }
}

/** Message for a failed tag operation (stage tags belong to boards) */
export function tagErrorMessage(e: unknown, tagName: string): string {
  if (e instanceof ApiError && e.serverCode === 'stage_tag_locked') {
    return `“${tagName}” is a board column tag — change it from the board`
  }
  return errorMessage(e)
}
