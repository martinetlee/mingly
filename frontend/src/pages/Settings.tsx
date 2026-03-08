import { useState, useEffect, useCallback, useRef, type ChangeEvent } from 'react'
import { getPrefs, setNotifPref as apiSetNotifPref } from '../api/client'

// ---------------------------------------------------------------------------
// API helpers (self-contained so we can handle SSE streaming)
// ---------------------------------------------------------------------------

async function apiFetch<T>(
  method: string,
  path: string,
  body?: object,
  signal?: AbortSignal,
): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    signal,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail ?? res.statusText)
  }
  return res.json()
}

async function* streamSSE(
  url: string,
  signal?: AbortSignal,
): AsyncGenerator<Record<string, unknown>> {
  const res = await fetch(url, { signal })
  if (!res.ok || !res.body) throw new Error('Stream unavailable')
  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += dec.decode(value, { stream: true })
      const parts = buf.split('\n\n')
      buf = parts.pop() ?? ''
      for (const part of parts) {
        const line = part.trim()
        if (line.startsWith('data: ')) {
          try {
            yield JSON.parse(line.slice(6))
          } catch {
            // skip malformed chunk
          }
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}

function sleep(ms: number) {
  return new Promise<void>((r) => setTimeout(r, ms))
}

// ---------------------------------------------------------------------------
// Model catalogue
// ---------------------------------------------------------------------------

interface ModelDef {
  id: string
  name: string
  detail: string
  type: 'local' | 'cloud'
  provider: string
  ollamaModel?: string
  available: boolean
}

const MODELS: ModelDef[] = [
  {
    id: 'qwen25',
    name: 'Qwen 2.5',
    detail: '7B · Runs locally · ~4 GB download',
    type: 'local',
    provider: 'ollama',
    ollamaModel: 'qwen2.5:7b',
    available: true,
  },
  {
    id: 'claude',
    name: 'Claude',
    detail: 'Anthropic · Cloud',
    type: 'cloud',
    provider: 'anthropic',
    available: false,
  },
  {
    id: 'openai',
    name: 'OpenAI GPT-4o',
    detail: 'OpenAI · Cloud',
    type: 'cloud',
    provider: 'openai',
    available: false,
  },
  {
    id: 'grok',
    name: 'Grok',
    detail: 'xAI · Cloud',
    type: 'cloud',
    provider: 'xai',
    available: false,
  },
]

// ---------------------------------------------------------------------------
// Setup flow types
// ---------------------------------------------------------------------------

type SetupPhase =
  | 'install-binary'
  | 'start-server'
  | 'pull-model'
  | 'registering'
  | 'done'
  | 'error'

interface SetupState {
  phase: SetupPhase
  label: string
  pct: number
  error?: string
}

type NotifPref = 'all' | 'tracked' | 'off'

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function Settings() {
  // Orchestrator state
  const [orchestratorId, setOrchestratorId] = useState<string | null>(null)
  const [pendingModelId, setPendingModelId] = useState<string | null>(null)
  const [loadingCfg, setLoadingCfg] = useState(true)

  // Download modal state
  const [setup, setSetup] = useState<SetupState | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  // Notification preference
  const [notifPref, setNotifPref] = useState<NotifPref>('tracked')

  useEffect(() => {
    apiFetch<{ orchestrator_name: string }>('GET', '/api/llm/orchestrator')
      .then(({ orchestrator_name }) => {
        if (orchestrator_name === 'local') setOrchestratorId('qwen25')
      })
      .catch(() => {})
      .finally(() => setLoadingCfg(false))

    // Load saved notification preference from backend
    getPrefs()
      .then(({ notif_pref }) => setNotifPref(notif_pref as NotifPref))
      .catch(() => {})
  }, [])

  // ── Orchestrator selection ──────────────────────────────────────────────

  const handleModelSelectChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const id = e.target.value
    if (!id || id === orchestratorId) {
      setPendingModelId(null)
      return
    }
    setPendingModelId(id)
  }

  const handleCancelPending = () => {
    setPendingModelId(null)
  }

  const handleConfirmSelection = () => {
    const model = MODELS.find((m) => m.id === pendingModelId)
    if (!model) return
    if (model.type === 'local') {
      // Start download/setup flow
      const ctrl = new AbortController()
      abortRef.current = ctrl
      setSetup({ phase: 'install-binary', label: 'Checking…', pct: 0 })
      runSetup(model, ctrl.signal)
    }
    // cloud: do nothing (coming soon)
  }

  // ── Core setup task ────────────────────────────────────────────────────

  const runSetup = useCallback(async (model: ModelDef, signal: AbortSignal) => {
    const update = (phase: SetupPhase, label: string, pct: number, error?: string) =>
      setSetup({ phase, label, pct, error })

    try {
      const status = await apiFetch<{
        binary_found: boolean
        server_running: boolean
      }>('GET', '/api/llm/ollama/install-status', undefined, signal)

      if (!status.binary_found) {
        update('install-binary', 'Downloading Ollama runtime…', 0)
        const { job_id } = await apiFetch<{ job_id: number }>(
          'POST', '/api/llm/ollama/install', undefined, signal,
        )
        for await (const ev of streamSSE(`/api/llm/jobs/${job_id}`, signal)) {
          if (ev.status === 'error')
            throw new Error((ev.error as string) || 'Binary install failed')
          if (typeof ev.total === 'number' && ev.total > 0 && typeof ev.downloaded === 'number') {
            update('install-binary', 'Downloading Ollama runtime…',
              Math.round((ev.downloaded / ev.total) * 100))
          } else if (ev.status === 'extracting') {
            update('install-binary', 'Extracting…', 99)
          }
          if (ev.status === 'done') break
        }
      }

      if (!status.server_running) {
        update('start-server', 'Starting Ollama server…', 0)
        await apiFetch('POST', '/api/llm/ollama/start', undefined, signal)
        for (let i = 0; i < 30; i++) {
          await sleep(500)
          if (signal.aborted) throw new DOMException('Cancelled', 'AbortError')
          const s = await apiFetch<{ server_running: boolean }>(
            'GET', '/api/llm/ollama/install-status', undefined, signal,
          )
          if (s.server_running) break
          if (i === 29) throw new Error('Ollama server did not start in time')
        }
      }

      update('pull-model', `Downloading ${model.name} model…`, 0)
      const { job_id: pullId } = await apiFetch<{ job_id: number }>(
        'POST', '/api/llm/ollama/pull', { model: model.ollamaModel }, signal,
      )
      for await (const ev of streamSSE(`/api/llm/jobs/${pullId}`, signal)) {
        if (ev.status === 'error')
          throw new Error((ev.error as string) || 'Model pull failed')
        if (typeof ev.total === 'number' && ev.total > 0 && typeof ev.completed === 'number') {
          update('pull-model', `Downloading ${model.name} model…`,
            Math.round((ev.completed / ev.total) * 100))
        }
        if (ev.status === 'done') break
      }

      update('registering', 'Configuring AI model…', 0)
      try {
        await apiFetch('POST', '/api/llm/instances', {
          name: 'local',
          provider: model.provider,
          model: model.ollamaModel,
          base_url: 'http://localhost:11434/v1',
        }, signal)
      } catch (e) {
        if (!(e instanceof Error && e.message.toLowerCase().includes('already'))) throw e
      }
      await apiFetch('PUT', '/api/llm/orchestrator', {
        orchestrator_name: 'local',
        orchestrator_prompt: '',
      }, signal)

      setOrchestratorId(model.id)
      setPendingModelId(null)
      update('done', '', 100)
    } catch (e) {
      if (e instanceof DOMException && e.name === 'AbortError') {
        setSetup(null)
        return
      }
      setSetup((prev) => ({
        ...prev!,
        phase: 'error',
        error: e instanceof Error ? e.message : 'Setup failed',
      }))
    }
  }, [])

  const handleCancelDownload = () => {
    abortRef.current?.abort()
    abortRef.current = null
    setSetup(null)
  }

  const handleRetrySetup = () => {
    const model = MODELS.find((m) => m.id === pendingModelId)
    if (!model) return
    const ctrl = new AbortController()
    abortRef.current = ctrl
    setSetup({ phase: 'install-binary', label: 'Checking…', pct: 0 })
    runSetup(model, ctrl.signal)
  }

  const handleCloseDone = () => {
    setSetup(null)
    setPendingModelId(null)
  }

  // ── Notifications ──────────────────────────────────────────────────────

  const handleNotifPrefChange = async (e: ChangeEvent<HTMLSelectElement>) => {
    const pref = e.target.value as NotifPref
    setNotifPref(pref)
    apiSetNotifPref(pref).catch(() => {})
  }

  // ── Derived ────────────────────────────────────────────────────────────

  const pendingModel = MODELS.find((m) => m.id === pendingModelId) ?? null
  const selectValue = pendingModelId ?? orchestratorId ?? ''
  const inProgress = setup !== null && !['done', 'error'].includes(setup.phase)
  const isIndeterminate = setup?.phase === 'start-server' || setup?.phase === 'registering'

  if (loadingCfg) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <p className="text-sm text-gray-400">Loading…</p>
      </div>
    )
  }

  return (
    <div className="flex flex-1 flex-col overflow-y-auto p-8">
      <h1 className="mb-6 text-lg font-semibold text-slate-100">Settings</h1>

      {/* ── AI Models ── */}
      <section className="mb-8 max-w-xl">
        <h2 className="mb-1 text-sm font-medium text-slate-300">AI Models</h2>
        <p className="mb-4 text-xs text-slate-400">
          Select the orchestrator model that powers AI features in Mingly.
        </p>

        <div className="overflow-hidden rounded-xl border border-gray-200 bg-white p-4">
          <label className="mb-1.5 block text-xs font-medium text-gray-600">
            Orchestrator LLM
          </label>
          <select
            value={selectValue}
            onChange={handleModelSelectChange}
            className="w-full rounded-lg border border-gray-200 bg-white px-3 py-2.5 text-sm text-gray-900 outline-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100 cursor-pointer"
          >
            <option value="" disabled>Select a model…</option>
            {MODELS.map((m) => (
              <option key={m.id} value={m.id} disabled={!m.available}>
                {m.name}
                {m.type === 'local' && m.available ? ' (Local)' : ''}
                {!m.available ? ' — Coming soon' : ''}
              </option>
            ))}
          </select>

          {/* Inline confirm when pending change */}
          {pendingModelId && pendingModelId !== orchestratorId && (
            <div className="mt-3 flex items-center justify-between rounded-lg bg-blue-50 px-3 py-2.5">
              <span className="text-sm text-blue-800">
                {pendingModel?.type === 'local'
                  ? `Set up ${pendingModel?.name} locally?`
                  : `Switch to ${pendingModel?.name}?`}
              </span>
              <div className="flex gap-2">
                <button
                  onClick={handleCancelPending}
                  className="rounded-lg px-3 py-1.5 text-xs font-medium text-gray-600 hover:bg-white"
                >
                  Cancel
                </button>
                <button
                  onClick={handleConfirmSelection}
                  disabled={!pendingModel?.available}
                  className="rounded-lg bg-blue-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-600 disabled:opacity-50"
                >
                  {pendingModel?.type === 'local' ? 'Download & Set up' : 'Apply'}
                </button>
              </div>
            </div>
          )}

          {/* Current status */}
          {orchestratorId && !pendingModelId && (
            <p className="mt-2 text-xs text-green-600">
              ✓ {MODELS.find((m) => m.id === orchestratorId)?.name} is active
            </p>
          )}
        </div>
      </section>

      {/* ── Notifications ── */}
      <section className="max-w-xl">
        <h2 className="mb-1 text-sm font-medium text-slate-300">Notifications</h2>
        <p className="mb-4 text-xs text-slate-400">
          Get notified when new messages arrive.
        </p>

        <div className="overflow-hidden rounded-xl border border-gray-200 bg-white p-4">
          <label className="mb-1.5 block text-xs font-medium text-gray-600">
            Notify me about
          </label>
          <select
            value={notifPref}
            onChange={handleNotifPrefChange}
            className="w-full rounded-lg border border-gray-200 bg-white px-3 py-2.5 text-sm text-gray-900 outline-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100 cursor-pointer"
          >
            <option value="all">All new messages</option>
            <option value="tracked">Tracked chats only</option>
            <option value="off">Off</option>
          </select>

          {notifPref !== 'off' && (
            <p className="mt-2 text-xs text-green-600">✓ Native notifications enabled</p>
          )}
        </div>
      </section>

      {/* ── Download modal ── */}
      {setup && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-sm rounded-2xl bg-white p-6 shadow-xl">

            {/* In progress */}
            {inProgress && (
              <>
                <h3 className="mb-1 text-base font-semibold text-gray-900">
                  Setting up {pendingModel?.name}…
                </h3>
                <p className="mb-4 text-sm text-gray-500">{setup.label}</p>

                {isIndeterminate ? (
                  <div className="h-2 overflow-hidden rounded-full bg-gray-100">
                    <div className="h-full animate-pulse rounded-full bg-blue-300" />
                  </div>
                ) : (
                  <>
                    <div className="mb-1 h-2 overflow-hidden rounded-full bg-gray-100">
                      <div
                        className="h-full rounded-full bg-blue-500 transition-all duration-300"
                        style={{ width: `${setup.pct}%` }}
                      />
                    </div>
                    <p className="text-right text-xs text-gray-400">{setup.pct}%</p>
                  </>
                )}

                <button
                  onClick={handleCancelDownload}
                  className="mt-4 w-full rounded-lg border border-gray-200 py-2 text-sm text-gray-500 hover:bg-gray-50"
                >
                  Cancel
                </button>
              </>
            )}

            {/* Done */}
            {setup.phase === 'done' && (
              <div className="text-center">
                <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-green-100 text-xl text-green-500">
                  ✓
                </div>
                <h3 className="mb-1 text-base font-semibold text-gray-900">
                  {pendingModel?.name ?? 'Model'} is ready
                </h3>
                <p className="mb-5 text-sm text-gray-500">
                  Your local AI model is set up and active as the orchestrator.
                </p>
                <button
                  onClick={handleCloseDone}
                  className="w-full rounded-lg bg-blue-500 py-2.5 text-sm font-medium text-white hover:bg-blue-600"
                >
                  Done
                </button>
              </div>
            )}

            {/* Error */}
            {setup.phase === 'error' && (
              <>
                <div className="mb-3 flex h-10 w-10 items-center justify-center rounded-full bg-red-100 text-lg text-red-500">
                  ✕
                </div>
                <h3 className="mb-2 text-base font-semibold text-gray-900">Setup failed</h3>
                <p className="mb-5 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-600">
                  {setup.error}
                </p>
                <div className="flex gap-3">
                  <button
                    onClick={handleCancelDownload}
                    className="flex-1 rounded-lg border border-gray-200 py-2.5 text-sm text-gray-600 hover:bg-gray-50"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleRetrySetup}
                    className="flex-1 rounded-lg bg-blue-500 py-2.5 text-sm font-medium text-white hover:bg-blue-600"
                  >
                    Retry
                  </button>
                </div>
              </>
            )}

          </div>
        </div>
      )}
    </div>
  )
}
