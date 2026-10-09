/**
 * "Message this person": their most recent one-to-one conversation on any
 * channel (GET /api/people/{id}/conversations). Only when they have none — and
 * only if they're reachable on Telegram, the one channel that can create
 * conversations today — start a Telegram one. `messengersOnly` (chat-only
 * entry points such as the Chats space's new-chat dialog) skips email threads.
 */
import type { Conversation, Identity } from '../api/types'
import { getPersonConversations, startPrivateChat } from '../api/client'

export class NoConversationError extends Error {
  constructor() {
    super('No conversation with this person yet')
    this.name = 'NoConversationError'
  }
}

/** Can a direct conversation be started with someone with these identities? */
export function canStartConversation(identities: Pick<Identity, 'channel'>[]): boolean {
  return identities.some((i) => i.channel === 'telegram')
}

export async function openDirectConversation(
  personId: number, opts: { canStart: boolean; messengersOnly?: boolean },
): Promise<Conversation> {
  const existing = await getPersonConversations(personId)
  const found = opts.messengersOnly ? existing.find((c) => c.kind !== 'email_thread') : existing[0]
  if (found) return found
  if (!opts.canStart) throw new NoConversationError()
  return startPrivateChat(personId)
}
