/**
 * The one HTTP transport for the app: JSON requests, SSE streams and uploads.
 *
 * Every authenticated call goes through here so token injection, 401 →
 * session-expiry routing and error normalisation behave identically
 * everywhere. See lib/README.md for usage.
 */
import { getSessionToken, notifySessionExpired } from '../api/session'

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

// Transient statuses worth retrying: rate limits and gateway/availability blips
const RETRYABLE_STATUS = new Set([429, 502, 503, 504])

export type ApiErrorCode =
  | 'http'            // non-2xx with a JSON/text body
  | 'validation'      // FastAPI 422
  | 'session_expired' // 401 on an authenticated route
  | 'html_response'   // got the SPA's HTML instead of JSON (unknown route)
  | 'network'         // fetch/XHR failed before a response arrived
  | 'bad_response'    // 2xx whose body isn't valid JSON

/** Normalised API failure. `message` is always human-readable. */
export class ApiError extends Error {
  readonly status: number
  readonly code: ApiErrorCode
  readonly path: string
  /** Raw `detail` from the response body, if any */
  readonly detail: unknown
  /** Machine-readable `code` from the error body, e.g. "vault_corrupt" */
  readonly serverCode?: string
  /** Field-level FastAPI errors (`errors[]` on 422) */
  readonly errors?: unknown[]
  /** The full parsed JSON error body (extra keys, e.g. `existing_id` on a 409) */
  readonly body?: Record<string, unknown>

  constructor(message: string, opts: {
    status: number; code: ApiErrorCode; path: string; detail?: unknown; serverCode?: string; errors?: unknown[]
    body?: Record<string, unknown>
  }) {
    super(message)
    this.name = 'ApiError'
    this.status = opts.status
    this.code = opts.code
    this.path = opts.path
    this.detail = opts.detail
    this.serverCode = opts.serverCode
    this.errors = opts.errors
    this.body = opts.body
  }
}

// True when a fetch was cancelled via AbortController — callers that pass a
// signal should swallow these instead of surfacing an error state
export const isAbortError = (e: unknown): boolean =>
  (e instanceof DOMException || e instanceof Error) && (e as Error).name === 'AbortError'

export const isSessionExpiredError = (e: unknown): boolean =>
  e instanceof ApiError && e.code === 'session_expired'

/**
 * Turn a FastAPI-style error body into one readable string.
 * - `{detail: "text"}` → text
 * - `{detail: [{loc, msg}, …]}` (422) → "field: msg; field2: msg2"
 * - `{detail: {message|error: …}}` → that string
 */
export function formatErrorDetail(detail: unknown, fallback: string): string {
  if (detail == null || detail === '') return fallback
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const parts = detail.map((d) => {
      if (d && typeof d === 'object' && 'msg' in d) {
        const loc = Array.isArray((d as { loc?: unknown }).loc)
          ? ((d as { loc: unknown[] }).loc).filter((p) => p !== 'body' && p !== 'query' && p !== 'path').join('.')
          : ''
        const msg = String((d as { msg: unknown }).msg)
        return loc ? `${loc}: ${msg}` : msg
      }
      return typeof d === 'string' ? d : JSON.stringify(d)
    })
    return parts.join('; ') || fallback
  }
  if (typeof detail === 'object') {
    const o = detail as Record<string, unknown>
    for (const k of ['message', 'error', 'detail']) {
      if (typeof o[k] === 'string') return o[k] as string
    }
    try { return JSON.stringify(detail) } catch { return fallback }
  }
  return String(detail)
}

// The only routes reachable without a session token (backend sync contract §1).
// A 401 from these is a normal error (e.g. wrong password), not an expired
// session; every other route — including the rest of /api/auth/* — treats
// 401 as "session expired".
const PUBLIC_ROUTES = ['/api/auth/vault-status', '/api/auth/vault/init', '/api/auth/vault/unlock', '/api/auth/vault/reset', '/api/session/validate']

function isAuthRoute(path: string): boolean {
  const p = path.split('?')[0]
  return PUBLIC_ROUTES.includes(p)
}

function looksLikeHtml(contentType: string | null, text: string): boolean {
  if (contentType?.includes('text/html')) return true
  const head = text.trimStart().slice(0, 15).toLowerCase()
  return head.startsWith('<!doctype') || head.startsWith('<html')
}

function htmlError(path: string, status: number): ApiError {
  return new ApiError(`API endpoint not found: ${path.split('?')[0]}`, {
    status: status >= 400 ? status : 404,
    code: 'html_response',
    path,
  })
}

/**
 * Build an ApiError from an error body. The backend contract is
 * `{detail: string, code?: string, errors?: [...]}`; older shapes (422
 * detail arrays, dict details, plain text) are still normalised.
 */
function errorFromBody(text: string, contentType: string | null, status: number, statusText: string, path: string): ApiError {
  if (looksLikeHtml(contentType, text)) return htmlError(path, status)
  let detail: unknown = text || undefined
  let serverCode: string | undefined
  let errors: unknown[] | undefined
  let parsedBody: Record<string, unknown> | undefined
  try {
    const body = JSON.parse(text)
    if (body && typeof body === 'object' && !Array.isArray(body)) {
      parsedBody = body
      detail = 'detail' in body ? body.detail : body
      if (typeof body.code === 'string') serverCode = body.code
      if (Array.isArray(body.errors)) errors = body.errors
    } else {
      detail = body
    }
  } catch {
    // plain-text body
  }
  const fallback = statusText || `Request failed (${status})`
  return new ApiError(formatErrorDetail(detail, fallback), {
    status,
    code: status === 422 ? 'validation' : 'http',
    path,
    detail,
    serverCode,
    errors,
    body: parsedBody,
  })
}

/** Build an ApiError from a non-OK response (reads the body). */
async function errorFromResponse(res: Response, path: string): Promise<ApiError> {
  const text = await res.text().catch(() => '')
  return errorFromBody(text, res.headers.get('content-type'), res.status, res.statusText, path)
}

function authHeaders(extra?: HeadersInit): Record<string, string> {
  const headers: Record<string, string> = { ...(extra as Record<string, string> | undefined) }
  const token = getSessionToken()
  if (token) headers['X-Session-Token'] = token
  return headers
}

/**
 * 401 handling shared by every transport: outside /api/auth/* a 401 means the
 * session is gone, so route it to the global expiry handler.
 */
function sessionExpired(path: string, skip?: boolean): ApiError {
  if (!skip && !isAuthRoute(path)) notifySessionExpired()
  return new ApiError('Session expired', { status: 401, code: 'session_expired', path })
}

export interface RequestOptions extends RequestInit {
  /**
   * Retry attempts for transient failures. Defaults to 3 for GET and 1
   * (no retry) for anything else — retrying writes could double-send.
   */
  attempts?: number
  /** Don't fire the global session-expired handler on 401 (still throws). */
  skipSessionExpiry?: boolean
}

/**
 * JSON request with session token. A 401 calls notifySessionExpired() except
 * on the public vault/session routes listed in PUBLIC_ROUTES. Resolves to the parsed body (undefined for
 * 204/empty). Throws ApiError (or the AbortError when `signal` aborts).
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { attempts, skipSessionExpiry, ...init } = options
  const isForm = typeof FormData !== 'undefined' && init.body instanceof FormData
  const headers = authHeaders({
    ...(isForm ? {} : { 'Content-Type': 'application/json' }),
    ...(init.headers as Record<string, string> | undefined),
  })
  const method = (init.method ?? 'GET').toUpperCase()
  const maxAttempts = attempts ?? (method === 'GET' ? 3 : 1)
  let lastError: Error = new ApiError('Request failed', { status: 0, code: 'network', path })

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    let res: Response
    try {
      res = await fetch(path, { ...init, headers })
    } catch (e) {
      // A deliberate cancellation must not be retried or converted
      if (isAbortError(e)) throw e
      lastError = new ApiError(
        e instanceof Error && e.message ? `Network error: ${e.message}` : 'Network error',
        { status: 0, code: 'network', path },
      )
      if (attempt < maxAttempts) {
        await sleep(500 * attempt)
        continue
      }
      throw lastError
    }
    if (res.status === 401) {
      if (isAuthRoute(path)) throw await errorFromResponse(res, path)
      throw sessionExpired(path, skipSessionExpiry)
    }
    if (!res.ok) {
      if (RETRYABLE_STATUS.has(res.status) && attempt < maxAttempts) {
        const retryAfter = parseInt(res.headers.get('Retry-After') ?? '', 10)
        await sleep(Number.isNaN(retryAfter) ? 500 * attempt : Math.min(retryAfter * 1000, 5000))
        continue
      }
      throw await errorFromResponse(res, path)
    }
    if (res.status === 204) return undefined as T
    const text = await res.text()
    if (!text) return undefined as T
    try {
      return JSON.parse(text) as T
    } catch {
      if (looksLikeHtml(res.headers.get('content-type'), text)) throw htmlError(path, res.status)
      throw new ApiError(`Invalid response from ${path.split('?')[0]}`, { status: res.status, code: 'bad_response', path })
    }
  }
  throw lastError
}

/** Authenticated GET returning a Blob (file downloads). */
export async function requestBlob(path: string, signal?: AbortSignal): Promise<Blob> {
  const res = await fetch(path, { headers: authHeaders(), signal })
  if (res.status === 401) throw sessionExpired(path)
  if (!res.ok) throw await errorFromResponse(res, path)
  return res.blob()
}

// ---------------------------------------------------------------------------
// Server-Sent Events
// ---------------------------------------------------------------------------

export interface SSEEvent<T = Record<string, unknown>> {
  /** `event:` field; "message" when absent */
  event: string
  /** JSON-parsed `data:` payload (multi-line data joined with "\n") */
  data: T
  /** `id:` field, if present */
  id?: string
}

export interface StreamOptions {
  method?: string
  /** JSON-serialised when not a string/FormData */
  body?: unknown
  signal?: AbortSignal
  headers?: Record<string, string>
}

/** Parse one SSE block (lines between blank lines). Returns null for comments/empty. */
export function parseSSEBlock(block: string): { event: string; data: string; id?: string } | null {
  let event = 'message'
  let id: string | undefined
  const data: string[] = []
  let sawData = false
  for (const rawLine of block.split('\n')) {
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
    if (!line || line.startsWith(':')) continue
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') event = value
    else if (field === 'data') { data.push(value); sawData = true }
    else if (field === 'id') id = value
  }
  if (!sawData) return null
  return { event, data: data.join('\n'), id }
}

/**
 * Open an SSE endpoint (GET or POST) and yield parsed events. Malformed JSON
 * payloads are skipped. Ends when the server closes the stream; throws
 * ApiError on HTTP errors and the AbortError when `signal` aborts.
 */
export async function* stream<T = Record<string, unknown>>(
  path: string,
  opts: StreamOptions = {},
): AsyncGenerator<SSEEvent<T>> {
  const { method = 'GET', body, signal } = opts
  const isRaw = typeof body === 'string' || (typeof FormData !== 'undefined' && body instanceof FormData)
  const headers = authHeaders({
    Accept: 'text/event-stream',
    ...(body !== undefined && !isRaw ? { 'Content-Type': 'application/json' } : {}),
    ...opts.headers,
  })
  let res: Response
  try {
    res = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : isRaw ? (body as BodyInit) : JSON.stringify(body),
      signal,
    })
  } catch (e) {
    if (isAbortError(e)) throw e
    throw new ApiError('Network error', { status: 0, code: 'network', path })
  }
  if (res.status === 401) throw sessionExpired(path)
  if (!res.ok) throw await errorFromResponse(res, path)
  if (!res.body) throw new ApiError('Stream unavailable', { status: res.status, code: 'bad_response', path })
  if (res.headers.get('content-type')?.includes('text/html')) throw htmlError(path, res.status)

  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  const flush = function* (blocks: string[]): Generator<SSEEvent<T>> {
    for (const block of blocks) {
      const parsed = parseSSEBlock(block)
      if (!parsed) continue
      try {
        yield { event: parsed.event, data: JSON.parse(parsed.data) as T, id: parsed.id }
      } catch {
        // skip malformed chunk
      }
    }
  }
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += dec.decode(value, { stream: true }).replace(/\r\n/g, '\n')
      const blocks = buf.split('\n\n')
      buf = blocks.pop() ?? ''
      yield* flush(blocks)
    }
    buf += dec.decode()
    if (buf.trim()) yield* flush([buf])
  } finally {
    // Cancel (not just release) so an early `break` by the consumer closes
    // the HTTP stream instead of leaving the server generating into the void
    reader.cancel().catch(() => {})
  }
}

// ---------------------------------------------------------------------------
// Uploads (XHR for progress events)
// ---------------------------------------------------------------------------

export interface UploadOptions {
  method?: string
  /** Called with 0..1 as the request body uploads */
  onProgress?: (fraction: number) => void
  signal?: AbortSignal
}

export interface UploadHandle<T> {
  promise: Promise<T>
  abort: () => void
}

/**
 * Multipart upload with progress. Same token/401/error semantics as
 * request(). Aborting rejects with an AbortError DOMException.
 */
export function upload<T>(path: string, formData: FormData, opts: UploadOptions = {}): UploadHandle<T> {
  const xhr = new XMLHttpRequest()
  const promise = new Promise<T>((resolve, reject) => {
    xhr.open(opts.method ?? 'POST', path)
    const token = getSessionToken()
    if (token) xhr.setRequestHeader('X-Session-Token', token)

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && opts.onProgress) opts.onProgress(e.loaded / e.total)
    }

    xhr.onload = () => {
      const text = xhr.responseText
      if (xhr.status === 401 && !isAuthRoute(path)) {
        reject(sessionExpired(path))
        return
      }
      const contentType = xhr.getResponseHeader('content-type')
      if (xhr.status >= 200 && xhr.status < 300) {
        if (!text) { resolve(undefined as T); return }
        try {
          resolve(JSON.parse(text) as T)
        } catch {
          reject(looksLikeHtml(contentType, text)
            ? htmlError(path, xhr.status)
            : new ApiError('Invalid response', { status: xhr.status, code: 'bad_response', path }))
        }
        return
      }
      reject(errorFromBody(text, contentType, xhr.status, xhr.statusText || `Upload failed (${xhr.status})`, path))
    }

    xhr.onerror = () => reject(new ApiError('Network error', { status: 0, code: 'network', path }))
    xhr.onabort = () => reject(new DOMException('Upload cancelled', 'AbortError'))

    if (opts.signal) {
      if (opts.signal.aborted) { xhr.abort(); return }
      opts.signal.addEventListener('abort', () => xhr.abort(), { once: true })
    }
    xhr.send(formData)
  })
  return { promise, abort: () => xhr.abort() }
}
