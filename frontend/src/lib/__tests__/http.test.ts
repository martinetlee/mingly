import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { request, stream, upload, ApiError, formatErrorDetail, parseSSEBlock, isAbortError } from '../http'
import * as session from '../../api/session'

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json', ...headers } })
}

function sseResponse(chunks: string[], status = 200) {
  const enc = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(enc.encode(c))
      controller.close()
    },
  })
  return new Response(body, { status, headers: { 'content-type': 'text/event-stream' } })
}

let fetchMock: ReturnType<typeof vi.fn>

/** Resolve with the rejection reason (fails the test if it resolved). */
const fail = (p: Promise<unknown>): Promise<ApiError> =>
  p.then(() => { throw new Error('expected rejection') }, (e: ApiError) => e)

beforeEach(() => {
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  session.setSessionToken('tok')
})
afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('formatErrorDetail', () => {
  it('handles string, FastAPI 422 arrays and objects', () => {
    expect(formatErrorDetail('Nope', 'x')).toBe('Nope')
    expect(formatErrorDetail([
      { loc: ['body', 'pref'], msg: 'Input should be all, tracked or off', type: 'enum' },
      { loc: ['query', 'limit'], msg: 'must be > 0' },
    ], 'x')).toBe('pref: Input should be all, tracked or off; limit: must be > 0')
    expect(formatErrorDetail({ message: 'bad' }, 'x')).toBe('bad')
    expect(formatErrorDetail(null, 'fallback')).toBe('fallback')
  })
})

describe('request', () => {
  it('sends the session token and parses JSON', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: 1 }))
    await expect(request('/api/x')).resolves.toEqual({ ok: 1 })
    const [, init] = fetchMock.mock.calls[0]
    expect(init.headers['X-Session-Token']).toBe('tok')
  })

  it('normalises 422 detail arrays (no more [object Object])', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: [{ loc: ['body', 'name'], msg: 'Field required' }] }, 422))
    const err = await fail(request('/api/x', { method: 'POST' }))
    expect(err).toBeInstanceOf(ApiError)
    expect(err.message).toBe('name: Field required')
    expect(err.status).toBe(422)
    expect(err.code).toBe('validation')
  })

  it('carries the server code and errors[] from the contract error body', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'The vault file is damaged', code: 'vault_corrupt' }, 409))
    const err = await fail(request('/api/auth/vault/unlock', { method: 'POST' }))
    expect(err.message).toBe('The vault file is damaged')
    expect(err.serverCode).toBe('vault_corrupt')
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'Exists', code: 'organization_exists', existing_id: 7, existing_name: 'Acme' }, 409))
    const dup = await fail(request('/api/organizations', { method: 'POST' }))
    expect(dup.body).toMatchObject({ existing_id: 7, existing_name: 'Acme' })
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'password: Field required', errors: [{ loc: ['body', 'password'] }] }, 422))
    const v = await fail(request('/api/x', { method: 'POST' }))
    expect(v.message).toBe('password: Field required')
    expect(v.errors).toHaveLength(1)
  })

  it('reports an HTML (SPA fallback) response as a missing endpoint', async () => {
    fetchMock.mockResolvedValueOnce(new Response('<!doctype html><html></html>', { status: 200, headers: { 'content-type': 'text/html' } }))
    const err = await fail(request('/api/nope?x=1'))
    expect(err).toBeInstanceOf(ApiError)
    expect(err.code).toBe('html_response')
    expect(err.message).toBe('API endpoint not found: /api/nope')
  })

  it('routes 401 to session expiry except on the public vault/session routes', async () => {
    const spy = vi.spyOn(session, 'notifySessionExpired')
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'no' }, 401))
    const err = await fail(request('/api/chats'))
    expect(err.code).toBe('session_expired')
    expect(spy).toHaveBeenCalledTimes(1)

    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'Incorrect password' }, 401))
    const err2 = await fail(request('/api/auth/vault/unlock', { method: 'POST' }))
    expect(err2.message).toBe('Incorrect password')
    expect(spy).toHaveBeenCalledTimes(1)

    // Protected auth routes (sync contract): 401 on /api/auth/status = session expired
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'Invalid or expired session token' }, 401))
    const err3 = await fail(request('/api/auth/status'))
    expect(err3.code).toBe('session_expired')
    expect(spy).toHaveBeenCalledTimes(2)
  })

  it('retries GETs on transient failures but never retries writes', async () => {
    vi.useFakeTimers()
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ detail: 'busy' }, 503))
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
    const p = request('/api/x')
    await vi.runAllTimersAsync()
    await expect(p).resolves.toEqual({ ok: true })
    expect(fetchMock).toHaveBeenCalledTimes(2)

    fetchMock.mockReset()
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'busy' }, 503))
    const w = fail(request('/api/x', { method: 'POST' }))
    await vi.runAllTimersAsync()
    expect((await w).status).toBe(503)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('rethrows aborts untouched and returns undefined for 204', async () => {
    fetchMock.mockRejectedValueOnce(new DOMException('aborted', 'AbortError'))
    const err = await fail(request('/api/x'))
    expect(isAbortError(err)).toBe(true)
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }))
    await expect(request('/api/x', { method: 'DELETE' })).resolves.toBeUndefined()
  })
})

describe('SSE', () => {
  it('parses event/data/id fields, multi-line data and comments', () => {
    expect(parseSSEBlock('event: chunk\ndata: {"a":1}')).toEqual({ event: 'chunk', data: '{"a":1}', id: undefined })
    expect(parseSSEBlock(': keepalive')).toBeNull()
    expect(parseSSEBlock('data: line1\ndata: line2\nid: 7')).toEqual({ event: 'message', data: 'line1\nline2', id: '7' })
    expect(parseSSEBlock('data:{"nospace":true}')?.data).toBe('{"nospace":true}')
  })

  it('streams events split across arbitrary chunk boundaries', async () => {
    fetchMock.mockResolvedValueOnce(sseResponse([
      'event: start\nda', 'ta: {"n":1}\n\n',
      ': ping\n\n',
      'data: not-json\n\n',
      'event: chunk\r\ndata: {"n":2}\r\n\r\ndata: {"n":3}',
    ]))
    const got = []
    for await (const ev of stream<{ n: number }>('/api/s', { method: 'POST', body: { q: 1 } })) got.push(ev)
    expect(got).toEqual([
      { event: 'start', data: { n: 1 }, id: undefined },
      { event: 'chunk', data: { n: 2 }, id: undefined },
      { event: 'message', data: { n: 3 }, id: undefined },
    ])
    const [, init] = fetchMock.mock.calls[0]
    expect(init.method).toBe('POST')
    expect(init.body).toBe('{"q":1}')
    expect(init.headers['Content-Type']).toBe('application/json')
    expect(init.headers['X-Session-Token']).toBe('tok')
  })

  it('routes 401 to session expiry and throws ApiError on HTTP errors', async () => {
    const spy = vi.spyOn(session, 'notifySessionExpired')
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'x' }, 401))
    const gen = stream('/api/s')
    await expect(gen.next()).rejects.toMatchObject({ code: 'session_expired' })
    expect(spy).toHaveBeenCalled()

    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'No LLM configured' }, 400))
    await expect(stream('/api/s').next()).rejects.toMatchObject({ message: 'No LLM configured', status: 400 })
  })
})

describe('upload', () => {
  class FakeXHR {
    static last: FakeXHR
    status = 0
    statusText = ''
    responseText = ''
    headers: Record<string, string> = {}
    upload: { onprogress: ((e: { lengthComputable: boolean; loaded: number; total: number }) => void) | null } = { onprogress: null }
    onload: (() => void) | null = null
    onerror: (() => void) | null = null
    onabort: (() => void) | null = null
    constructor() { FakeXHR.last = this }
    open() {}
    setRequestHeader(k: string, v: string) { this.headers[k] = v }
    getResponseHeader() { return 'application/json' }
    send() {}
    abort() { this.onabort?.() }
    respond(status: number, body: unknown) {
      this.status = status
      this.responseText = JSON.stringify(body)
      this.onload?.()
    }
  }

  beforeEach(() => vi.stubGlobal('XMLHttpRequest', FakeXHR))

  it('reports progress, sends the token and resolves JSON', async () => {
    const progress = vi.fn()
    const { promise } = upload<{ ok: boolean }>('/api/up', new FormData(), { onProgress: progress })
    FakeXHR.last.upload.onprogress?.({ lengthComputable: true, loaded: 5, total: 10 })
    FakeXHR.last.respond(200, { ok: true })
    await expect(promise).resolves.toEqual({ ok: true })
    expect(progress).toHaveBeenCalledWith(0.5)
    expect(FakeXHR.last.headers['X-Session-Token']).toBe('tok')
  })

  it('routes 401 to session expiry (previously swallowed)', async () => {
    const spy = vi.spyOn(session, 'notifySessionExpired')
    const { promise } = upload('/api/up', new FormData())
    FakeXHR.last.respond(401, { detail: 'x' })
    await expect(promise).rejects.toMatchObject({ code: 'session_expired' })
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('normalises errors and rejects with AbortError on abort', async () => {
    const a = upload('/api/up', new FormData())
    FakeXHR.last.respond(413, { detail: 'File too large' })
    await expect(a.promise).rejects.toMatchObject({ message: 'File too large', status: 413 })
    const b = upload('/api/up', new FormData())
    b.abort()
    const err = await b.promise.catch((e: ApiError) => e)
    expect(isAbortError(err)).toBe(true)
  })
})
