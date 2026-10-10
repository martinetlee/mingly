/** LLM providers, API keys and background model jobs. */

import { request } from '../lib/http'
import type { LlmKeyProvider, LlmKeyStatus } from './types'
import { streamSSE } from './base'

/**
 * Follow a background LLM job (/api/llm/jobs/{id}: Ollama install or model
 * pull) to completion. Throws on `status: 'error'`, on any inline `error`
 * field (Ollama reports pull failures with HTTP 200), and when the stream
 * ends without `status: 'done'`, so a failed pull can never look like success.
 */
export async function followLlmJob(
  jobId: number,
  onProgress: (ev: Record<string, unknown>) => void,
  signal?: AbortSignal,
  failureLabel = 'Job failed',
): Promise<void> {
  for await (const ev of streamSSE(`/api/llm/jobs/${jobId}`, signal)) {
    if (ev.status === 'error' || ev.error) {
      throw new Error(typeof ev.error === 'string' && ev.error ? `${failureLabel}: ${ev.error}` : failureLabel)
    }
    if (ev.status === 'done') return
    onProgress(ev)
  }
  throw new Error(`${failureLabel}: the progress stream ended unexpectedly`)
}

export const getLlmKeys = () =>
  request<import('./types').LlmKeyStatus>('/api/llm/keys')

export const setLlmKey = (provider: import('./types').LlmKeyProvider, apiKey: string) =>
  request<{ provider: string; key_set: boolean }>(`/api/llm/keys/${provider}`, {
    method: 'PUT',
    body: JSON.stringify({ api_key: apiKey }),
  })

export const deleteLlmKey = (provider: import('./types').LlmKeyProvider) =>
  request<{ provider: string; key_set: boolean }>(`/api/llm/keys/${provider}`, { method: 'DELETE' })
