/**
 * Mentions name a person by a channel handle (a Telegram user id or a public
 * @username). They link to /mention/:channel/:externalId (the backend renders
 * Telegram mentions in text_html that way); pages/Mention resolves the
 * handle to an internal id and redirects.
 */
import { ApiError, getPersonForTelegramUser, resolveTelegramUsername } from '../api/client'
import { chatUrl, personUrl } from './routes'

/** In-app link for a mention: externalId is a user id, or `@username` */
export function mentionUrl(channel: string, externalId: string): string {
  return `/mention/${encodeURIComponent(channel)}/${encodeURIComponent(externalId)}`
}

/** Where a mention points in the app, or null when nothing stored matches */
export async function resolveMention(channel: string, externalId: string, signal?: AbortSignal): Promise<string | null> {
  if (channel !== 'telegram') return null
  try {
    if (externalId.startsWith('@')) {
      const r = await resolveTelegramUsername(externalId.slice(1), signal)
      if (r.person_id != null) return personUrl(r.person_id)
      if (r.conversation_id != null) return chatUrl(r.conversation_id)
      return null
    }
    const { person_id } = await getPersonForTelegramUser(externalId, signal)
    return personUrl(person_id)
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null
    throw e
  }
}
