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

/** A person's page (CRM record + every identity) */
export function personUrl(personId: number): string {
  return `/people/${personId}`
}
