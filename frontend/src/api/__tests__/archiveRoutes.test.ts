import { describe, it, expect, vi, afterEach } from 'vitest'
import { archiveConversation, getArchiveStatus, pauseArchive, startArchive, setArchived } from '../client'

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })

afterEach(() => vi.unstubAllGlobals())

function stubFetch(body: unknown = {}) {
  const fetchMock = vi.fn(async () => json(body))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('archive routes', () => {
  it('"Archive full history" posts to /archive-history, never the archive-folder toggle', async () => {
    const coverage = { history_complete: false, message_count: 3, oldest_seq: 1, newest_contiguous_seq: null }
    const fetchMock = stubFetch({ queued: true, coverage })
    await expect(archiveConversation(42)).resolves.toEqual({ queued: true, coverage })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/conversations/42/archive-history')
    expect(init.method).toBe('POST')
  })

  it('the archive-folder toggle keeps its own /archive route', async () => {
    const fetchMock = stubFetch({ conversation_id: 5, is_archived: true })
    await setArchived(5, true)
    expect((fetchMock.mock.calls[0] as unknown as [string])[0]).toBe('/api/conversations/5/archive')
  })

  it('DM archive status/start/pause routes', async () => {
    const fetchMock = stubFetch({})
    await getArchiveStatus()
    await startArchive()
    await pauseArchive()
    const calls = fetchMock.mock.calls as unknown as [string, RequestInit][]
    expect(calls.map(([u, i]) => `${i.method ?? 'GET'} ${u}`)).toEqual([
      'GET /api/archive/status', 'POST /api/archive/start', 'POST /api/archive/pause',
    ])
  })
})
