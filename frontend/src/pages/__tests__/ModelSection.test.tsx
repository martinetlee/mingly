import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'

// followLlmJob (real) reads the job stream through lib/http's `stream`
vi.mock('../../lib/http', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/http')>()),
  stream: vi.fn(),
}))
vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  apiFetch: vi.fn(),
}))

import { stream } from '../../lib/http'
import { apiFetch } from '../../api/client'
import ModelSection from '../settings/ModelSection'

const mockStream = vi.mocked(stream)
const mockApiFetch = vi.mocked(apiFetch)

function jobEvents(events: Record<string, unknown>[]) {
  return (async function* () {
    for (const data of events) yield { event: 'message', data, id: undefined }
  })() as unknown as ReturnType<typeof stream>
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApiFetch.mockImplementation((async (method: string, path: string) => {
    if (method === 'GET' && path === '/api/llm/orchestrator') return { orchestrator_name: '' }
    if (method === 'GET' && path === '/api/llm/instances') return { instances: [] }
    if (method === 'GET' && path === '/api/llm/ollama/status') return { running: true, missing_model: null }
    if (method === 'GET' && path === '/api/llm/ollama/models') return { models: [] }
    if (method === 'GET' && path === '/api/llm/ollama/install-status') return { binary_found: true, server_running: true }
    if (method === 'POST' && path === '/api/llm/ollama/pull') return { job_id: 7 }
    return {}
  }) as typeof apiFetch)
})
afterEach(cleanup)

async function startDownload() {
  render(<ModelSection />)
  const select = screen.getByLabelText('Orchestrator LLM') as HTMLSelectElement
  await waitFor(() => expect(select.disabled).toBe(false))
  fireEvent.change(select, { target: { value: 'qwen25-7b' } })
  fireEvent.click(screen.getByRole('button', { name: 'Download & Set up' }))
}

function expectNotActivated() {
  const calls = mockApiFetch.mock.calls.map(([m, p]) => `${m} ${p}`)
  expect(calls).not.toContain('PUT /api/llm/orchestrator')
  expect(calls).not.toContain('POST /api/llm/instances')
  expect(screen.queryByText(/is ready/)).toBeNull()
}

describe('ModelSection download', () => {
  it('a pull stream reporting {error} (HTTP 200) fails the setup and does not activate the model', async () => {
    mockStream.mockReturnValue(jobEvents([
      { status: 'pulling', total: 100, completed: 10 },
      { error: 'no space left on device' },
    ]))
    await startDownload()
    expect(await screen.findByText('Setup failed')).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toContain('Model download failed: no space left on device')
    expectNotActivated()
  })

  it('a pull stream that ends without "done" fails the setup and does not activate the model', async () => {
    mockStream.mockReturnValue(jobEvents([
      { status: 'pulling', total: 100, completed: 40 },
    ]))
    await startDownload()
    expect(await screen.findByText('Setup failed')).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toMatch(/ended unexpectedly/)
    expectNotActivated()
  })

  it('a "done" stream whose model is not actually installed still fails', async () => {
    mockStream.mockReturnValue(jobEvents([{ status: 'done' }]))
    await startDownload()
    expect(await screen.findByText('Setup failed')).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toMatch(/not installed/)
    expectNotActivated()
  })
})
