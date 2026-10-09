/**
 * In-app URLs keyed by internal ids (schema v2). Build every link to a
 * conversation or a person through these so the query/param names stay in
 * one place.
 */

/** Open a conversation, optionally centred on one of its messages */
export function chatUrl(conversationId: number, messageId?: number | null): string {
  return messageId != null
    ? `/chats?conversationId=${conversationId}&messageId=${messageId}`
    : `/chats?conversationId=${conversationId}`
}

/** Open an email thread in the Mail space, optionally on one of its messages */
export function mailUrl(conversationId: number, messageId?: number | null): string {
  return messageId != null
    ? `/mail?conversationId=${conversationId}&messageId=${messageId}`
    : `/mail?conversationId=${conversationId}`
}

/** A conversation's home: email threads open in Mail, everything else in Chats */
export function conversationUrl(conv: { id: number; kind: string }, messageId?: number | null): string {
  return conv.kind === 'email_thread' ? mailUrl(conv.id, messageId) : chatUrl(conv.id, messageId)
}

/**
 * For links that only know the conversation id (citations, notifications,
 * search hits): /open looks the conversation up and redirects to its
 * Chats or Mail location (pages/OpenConversation).
 */
export function openConversationUrl(conversationId: number, messageId?: number | null): string {
  return messageId != null ? `/open/${conversationId}?messageId=${messageId}` : `/open/${conversationId}`
}

/** A person's page (CRM record + every identity) */
export function personUrl(personId: number): string {
  return `/people/${personId}`
}
