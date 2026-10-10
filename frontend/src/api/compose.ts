/** Sending messages and files. */

import { request, upload } from '../lib/http'
import type { Message, SendFilesResponse } from './types'

export interface SendOptions {
  /** Forum topic to post into (1/omitted = General) */
  topicId?: number | null
  /** One per user send; a retry with the same nonce returns the first result instead of sending twice */
  clientNonce?: string
}

export const sendMessage = (conversationId: number, text: string, replyToMessageId?: number, opts: SendOptions = {}) =>
  request<{ success: boolean; message_id: number; message?: Message }>('/api/compose/send', {
    method: 'POST',
    body: JSON.stringify({
      conversation_id: conversationId, text, reply_to_message_id: replyToMessageId,
      topic_id: opts.topicId ?? undefined,
      client_nonce: opts.clientNonce,
    }),
  })

export const editMessage = (messageId: number, text: string) =>
  request<{ success: boolean; message?: Message }>('/api/compose/edit', {
    method: 'POST',
    body: JSON.stringify({ message_id: messageId, text }),
  })

export const deleteMessages = (conversationId: number, messageIds: number[], revoke = false) =>
  request<{ success: boolean }>('/api/compose/delete', {
    method: 'POST',
    body: JSON.stringify({ conversation_id: conversationId, message_ids: messageIds, revoke }),
  })

export const forwardMessages = (fromConversationId: number, messageIds: number[], toConversationId: number) =>
  request<{ success: boolean }>('/api/compose/forward', {
    method: 'POST',
    body: JSON.stringify({ from_conversation_id: fromConversationId, message_ids: messageIds, to_conversation_id: toConversationId }),
  })

export function sendFiles(
  conversationId: number,
  files: File[],
  text?: string,
  replyToMessageId?: number,
  onProgress?: (fraction: number) => void,
  opts: SendOptions & { signal?: AbortSignal } = {},
): { promise: Promise<SendFilesResponse>; abort: () => void } {
  const formData = new FormData()
  formData.append('conversation_id', String(conversationId))
  if (text) formData.append('text', text)
  if (replyToMessageId != null) formData.append('reply_to_message_id', String(replyToMessageId))
  if (opts.topicId != null) formData.append('topic_id', String(opts.topicId))
  if (opts.clientNonce) formData.append('client_nonce', opts.clientNonce)
  for (const file of files) {
    formData.append('files', file)
  }
  // 200 = all sent, 207 = partial (see `files`); 400 (none sent) rejects with ApiError, body.files has per-file errors
  return upload<SendFilesResponse>('/api/compose/send-files', formData, { onProgress, signal: opts.signal })
}
