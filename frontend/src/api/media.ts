/** Media downloads. */

import { request } from '../lib/http'

/** Fetch one attachment from its channel; resolves to the served URL */
export const downloadAttachment = (attachmentId: number, signal?: AbortSignal) =>
  request<{ url: string; kind: string }>(`/api/media-download/${attachmentId}`, {
    method: 'POST',
    signal,
  })
