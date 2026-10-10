/** Helpers shared by the domain API modules (on top of lib/http). */

import { request, stream } from '../lib/http'

// True when an error came from a Telegram rate limit surfaced by the backend
// (429 detail: "Telegram rate limit — retry in Ns")
export const isRateLimitError = (e: unknown): boolean =>
  e instanceof Error && e.message.includes('rate limit')

/**
 * Method-first JSON helper (formerly duplicated in Settings/LlmSetup).
 * Same semantics as request(): token header, GET retries, 401 → session expiry.
 */
export const apiFetch = <T>(method: string, path: string, body?: object, signal?: AbortSignal): Promise<T> =>
  request<T>(path, {
    method,
    body: body ? JSON.stringify(body) : undefined,
    signal,
  })

/** SSE helper yielding only the JSON `data` payloads (formerly duplicated in Settings/LlmSetup). */
export async function* streamSSE(path: string, signal?: AbortSignal): AsyncGenerator<Record<string, unknown>> {
  for await (const ev of stream<Record<string, unknown>>(path, { signal })) {
    yield ev.data
  }
}
