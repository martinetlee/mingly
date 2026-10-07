import { useCallback, useEffect, useRef, useState, type ChangeEvent } from 'react'
import { Check, X } from 'lucide-react'
import { apiFetch, followLlmJob } from '../../api/client'
import type { OllamaStatus } from '../../api/types'
import { Modal, modalButton } from '../../components/ui'
import { reportError } from '../../lib/toast'
import { Card, Section, labelClass, selectClass } from './shared'

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
    id: 'qwen35-4b',
    name: 'Qwen 3.5 · 4B',
    detail: '4B · Runs locally · ~3.2 GB download',
    type: 'local',
    provider: 'ollama',
    ollamaModel: 'qwen3.5:4b',
    available: true,
  },
  {
    id: 'qwen25-7b',
    name: 'Qwen 2.5 · 7B',
    detail: '7B · Runs locally · ~4.7 GB download',
    type: 'local',
    provider: 'ollama',
    ollamaModel: 'qwen2.5:7b',
    available: true,
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

const isDownloaded = (model: ModelDef | null | undefined, downloaded: string[]) =>
  !!(model?.ollamaModel && downloaded.some((n) => n === model.ollamaModel || n.startsWith(model.ollamaModel + ':')))

/** "AI Models": orchestrator choice, local Ollama server controls and the download/setup dialog. */
export default function ModelSection() {
  // Orchestrator state
  const [orchestratorId, setOrchestratorId] = useState<string | null>(null)
  const [pendingModelId, setPendingModelId] = useState<string | null>(null)
  const [loadingCfg, setLoadingCfg] = useState(true)

  // Download modal state
  const [setup, setSetup] = useState<SetupState | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  // Ollama server status
  const [ollamaRunning, setOllamaRunning] = useState<boolean | null>(null)
  const [missingModel, setMissingModel] = useState<string | null>(null)
  const [ollamaAction, setOllamaAction] = useState<'stopping' | 'restarting' | null>(null)

  // Ollama models already downloaded on disk
  const [downloadedModels, setDownloadedModels] = useState<string[]>([])

  const refreshOllamaStatus = useCallback(async () => {
    try {
      const { running, missing_model } = await apiFetch<OllamaStatus>('GET', '/api/llm/ollama/status')
      setOllamaRunning(running)
      setMissingModel(missing_model)
      if (running) {
        try {
          const { models } = await apiFetch<{ models: { name: string }[] }>('GET', '/api/llm/ollama/models')
          setDownloadedModels(models.map((m) => m.name))
        } catch {
          // Optional: without the list, a switch re-runs the (idempotent) download flow
        }
      }
    } catch {
      // Status endpoint unreachable: show the server as stopped (the Start button retries)
      setOllamaRunning(false)
      setMissingModel(null)
    }
  }, [])

  useEffect(() => {
    Promise.all([
      apiFetch<{ orchestrator_name: string }>('GET', '/api/llm/orchestrator'),
      apiFetch<{ instances: { name: string; model: string }[] }>('GET', '/api/llm/instances'),
    ])
      .then(([{ orchestrator_name }, { instances }]) => {
        if (orchestrator_name) {
          const inst = instances.find((i) => i.name === orchestrator_name)
          const matched = inst
            ? MODELS.find((m) => m.ollamaModel === inst.model)
            : null
          setOrchestratorId(matched?.id ?? null)
        }
      })
      .catch((e) => reportError(e, 'Failed to load AI model settings'))
      .finally(() => setLoadingCfg(false))

    refreshOllamaStatus()
    // Cancel an in-flight download if the page goes away
    return () => abortRef.current?.abort()
  }, [refreshOllamaStatus])

  // ── Orchestrator selection ──────────────────────────────────────────────

  const handleModelSelectChange = (e: ChangeEvent<HTMLSelectElement>) => {
    const id = e.target.value
    if (!id || id === orchestratorId) {
      setPendingModelId(null)
      return
    }
    setPendingModelId(id)
  }

  const handleStopOllama = async () => {
    setOllamaAction('stopping')
    try {
      await apiFetch('POST', '/api/llm/ollama/stop')
      await sleep(800)
      await refreshOllamaStatus()
    } catch (e) {
      reportError(e, "Couldn't stop Ollama")
    } finally {
      setOllamaAction(null)
    }
  }

  const handleRestartOllama = async () => {
    setOllamaAction('restarting')
    try {
      await apiFetch('POST', '/api/llm/ollama/restart')
      // Poll until it's back up (up to 15s)
      for (let i = 0; i < 30; i++) {
        await sleep(500)
        const { running } = await apiFetch<OllamaStatus>('GET', '/api/llm/ollama/status').catch(() => ({ running: false, missing_model: null }))
        if (running) { setOllamaRunning(true); break }
      }
    } catch (e) {
      reportError(e, "Couldn't restart Ollama")
    } finally {
      setOllamaAction(null)
      refreshOllamaStatus()
    }
  }

  const registerAsOrchestrator = async (model: ModelDef, signal?: AbortSignal) => {
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
  }

  const handleSwitchDirect = async (model: ModelDef) => {
    setSetup({ phase: 'registering', label: 'Switching model…', pct: 0 })
    try {
      await registerAsOrchestrator(model)
      setOrchestratorId(model.id)
      setPendingModelId(null)
      setSetup({ phase: 'done', label: '', pct: 100 })
    } catch (e) {
      setSetup((prev) => ({
        ...prev!,
        phase: 'error',
        error: e instanceof Error ? e.message : 'Switch failed',
      }))
    }
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
        await followLlmJob(job_id, (ev) => {
          if (typeof ev.total === 'number' && ev.total > 0 && typeof ev.downloaded === 'number') {
            update('install-binary', 'Downloading Ollama runtime…',
              Math.round((ev.downloaded / ev.total) * 100))
          } else if (ev.status === 'extracting') {
            update('install-binary', 'Extracting…', 99)
          }
        }, signal, 'Ollama runtime install failed')
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
      await followLlmJob(pullId, (ev) => {
        if (typeof ev.total === 'number' && ev.total > 0 && typeof ev.completed === 'number') {
          update('pull-model', `Downloading ${model.name} model…`,
            Math.round((ev.completed / ev.total) * 100))
        }
      }, signal, 'Model download failed')

      // A "done" stream is not proof: confirm Ollama really has the model before switching to it
      const { models: installed } = await apiFetch<{ models: { name: string }[] }>(
        'GET', '/api/llm/ollama/models', undefined, signal,
      )
      const names = installed.map((m) => m.name)
      setDownloadedModels(names)
      if (!isDownloaded(model, names)) {
        throw new Error(`Model download finished but ${model.ollamaModel} is not installed. Check your disk space and connection, then retry.`)
      }

      update('registering', 'Configuring AI model…', 0)
      await registerAsOrchestrator(model, signal)

      setOrchestratorId(model.id)
      setPendingModelId(null)
      update('done', '', 100)
      refreshOllamaStatus()
    } catch (e) {
      if (e instanceof Error && e.name === 'AbortError') {
        setSetup(null)
        return
      }
      setSetup((prev) => ({
        ...prev!,
        phase: 'error',
        error: e instanceof Error ? e.message : 'Setup failed',
      }))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshOllamaStatus])

  const startSetup = (model: ModelDef) => {
    abortRef.current?.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl
    setSetup({ phase: 'install-binary', label: 'Checking…', pct: 0 })
    runSetup(model, ctrl.signal)
  }

  const handleConfirmSelection = () => {
    const model = MODELS.find((m) => m.id === pendingModelId)
    if (!model || model.type !== 'local') return
    // Already on disk → just switch the orchestrator; otherwise full download + setup
    if (isDownloaded(model, downloadedModels)) handleSwitchDirect(model)
    else startSetup(model)
  }

  const handleCancelDownload = () => {
    abortRef.current?.abort()
    abortRef.current = null
    setSetup(null)
  }

  const handleDownloadMissing = () => {
    const model = MODELS.find((m) => m.id === orchestratorId)
    if (model) startSetup(model)
  }

  const handleRetrySetup = () => {
    const model = MODELS.find((m) => m.id === (pendingModelId ?? orchestratorId))
    if (model) startSetup(model)
  }

  const handleCloseDone = () => {
    setSetup(null)
    setPendingModelId(null)
  }

  // ── Derived ────────────────────────────────────────────────────────────

  const pendingModel = MODELS.find((m) => m.id === pendingModelId) ?? null
  const setupModel = pendingModel ?? MODELS.find((m) => m.id === orchestratorId) ?? null
  const selectValue = pendingModelId ?? orchestratorId ?? ''
  const inProgress = setup !== null && !['done', 'error'].includes(setup.phase)
  const isIndeterminate = setup?.phase === 'start-server' || setup?.phase === 'registering'
  const currentName = MODELS.find((m) => m.id === orchestratorId)?.name

  return (
    <Section
      title="AI Models"
      description="Select the orchestrator model that powers AI features in Common Defense."
      className="mb-8"
    >
      <Card>
        <label className={labelClass} htmlFor="orchestrator-select">Orchestrator LLM</label>
        <select
          id="orchestrator-select"
          value={selectValue}
          onChange={handleModelSelectChange}
          disabled={loadingCfg}
          className={selectClass}
        >
          <option value="" disabled>{loadingCfg ? 'Loading…' : 'Select a model…'}</option>
          {MODELS.map((m) => (
            <option key={m.id} value={m.id} disabled={!m.available}>
              {m.name}
              {m.type === 'local' && m.available ? ' (Local)' : ''}
              {!m.available ? ' — Coming soon' : ''}
            </option>
          ))}
        </select>

        {/* Inline confirm when pending change */}
        {pendingModel && pendingModelId !== orchestratorId && (() => {
          const alreadyDownloaded = isDownloaded(pendingModel, downloadedModels)
          const confirmLabel = alreadyDownloaded ? 'Switch' : 'Download & Set up'
          const confirmHint = alreadyDownloaded
            ? `Switch to ${pendingModel.name}?`
            : `Download & set up ${pendingModel.name} locally?`
          return (
            <div className="mt-3 flex items-center justify-between rounded-lg bg-blue-900/40 border border-blue-700/50 px-3 py-2.5">
              <span className="text-sm text-blue-300">{confirmHint}</span>
              <div className="flex gap-2">
                <button
                  onClick={() => setPendingModelId(null)}
                  className="rounded-lg px-3 py-1.5 text-xs font-medium text-slate-400 hover:bg-slate-700 hover:text-slate-200"
                >
                  Cancel
                </button>
                <button
                  onClick={handleConfirmSelection}
                  disabled={!pendingModel.available || inProgress}
                  className="rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
                >
                  {confirmLabel}
                </button>
              </div>
            </div>
          )
        })()}

        {/* Current status */}
        {orchestratorId && !pendingModelId && (
          <div className="mt-3 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <span className={`inline-block h-1.5 w-1.5 rounded-full ${ollamaRunning ? (missingModel ? 'bg-amber-400' : 'bg-emerald-400') : 'bg-slate-500'}`} />
              <p className={`text-xs ${ollamaRunning ? (missingModel ? 'text-amber-400' : 'text-emerald-400') : 'text-slate-500'}`}>
                {ollamaRunning
                  ? missingModel
                    ? `${currentName} not downloaded`
                    : `${currentName} is running`
                  : 'Ollama server stopped'}
              </p>
            </div>
            <div className="flex items-center gap-1.5">
              {ollamaRunning && missingModel && (
                <button
                  onClick={handleDownloadMissing}
                  disabled={ollamaAction !== null || inProgress}
                  title="Download the configured model"
                  className="rounded px-2 py-1 text-xs text-amber-400 hover:bg-amber-900/30 hover:text-amber-300 disabled:opacity-40 transition-colors"
                >
                  Download
                </button>
              )}
              {ollamaRunning && (
                <button
                  onClick={handleRestartOllama}
                  disabled={ollamaAction !== null}
                  title="Restart Ollama server"
                  className="rounded px-2 py-1 text-xs text-slate-400 hover:bg-slate-700 hover:text-slate-200 disabled:opacity-40 transition-colors"
                >
                  {ollamaAction === 'restarting' ? 'Restarting…' : 'Restart'}
                </button>
              )}
              {ollamaRunning && (
                <button
                  onClick={handleStopOllama}
                  disabled={ollamaAction !== null}
                  title="Stop Ollama server"
                  className="rounded px-2 py-1 text-xs text-rose-400 hover:bg-rose-900/30 hover:text-rose-300 disabled:opacity-40 transition-colors"
                >
                  {ollamaAction === 'stopping' ? 'Stopping…' : 'Stop'}
                </button>
              )}
              {ollamaRunning === false && (
                <button
                  onClick={handleRestartOllama}
                  disabled={ollamaAction !== null}
                  title="Start Ollama server"
                  className="rounded px-2 py-1 text-xs text-emerald-400 hover:bg-emerald-900/30 hover:text-emerald-300 disabled:opacity-40 transition-colors"
                >
                  {ollamaAction === 'restarting' ? 'Starting…' : 'Start'}
                </button>
              )}
            </div>
          </div>
        )}
      </Card>

      {/* ── Download / setup dialog ── */}
      {setup && (
        <Modal
          open
          size="sm"
          // While working, only the Cancel button stops it (it aborts the download)
          onClose={inProgress ? () => {} : setup.phase === 'done' ? handleCloseDone : handleCancelDownload}
          closeOnBackdrop={!inProgress}
          closeOnEscape={!inProgress}
          ariaLabel="AI model setup"
          footer={
            inProgress ? (
              <button type="button" onClick={handleCancelDownload} className={modalButton.secondary}>Cancel</button>
            ) : setup.phase === 'done' ? (
              <button type="button" onClick={handleCloseDone} className={modalButton.primary}>Done</button>
            ) : (
              <>
                <button type="button" onClick={handleCancelDownload} className={modalButton.secondary}>Cancel</button>
                <button type="button" onClick={handleRetrySetup} className={modalButton.primary}>Retry</button>
              </>
            )
          }
        >
          <div className="px-5 py-4">
            {inProgress && (
              <>
                <h3 className="mb-1 text-sm font-semibold text-slate-100">
                  Setting up {setupModel?.name ?? 'model'}…
                </h3>
                <p className="mb-4 text-xs text-slate-400">{setup.label}</p>
                {isIndeterminate ? (
                  <div className="h-2 overflow-hidden rounded-full bg-slate-700">
                    <div className="h-full animate-pulse rounded-full bg-indigo-400/60" />
                  </div>
                ) : (
                  <>
                    <div className="mb-1 h-2 overflow-hidden rounded-full bg-slate-700">
                      <div
                        className="h-full rounded-full bg-indigo-500 transition-all duration-300"
                        style={{ width: `${setup.pct}%` }}
                      />
                    </div>
                    <p className="text-right text-xs text-slate-500">{setup.pct}%</p>
                  </>
                )}
              </>
            )}

            {setup.phase === 'done' && (
              <div className="flex items-start gap-3">
                <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-emerald-500/20">
                  <Check size={16} className="text-emerald-400" />
                </div>
                <div>
                  <h3 className="text-sm font-semibold text-slate-100">{setupModel?.name ?? 'Model'} is ready</h3>
                  <p className="mt-1 text-xs text-slate-400">Your local AI model is set up and active as the orchestrator.</p>
                </div>
              </div>
            )}

            {setup.phase === 'error' && (
              <div className="flex items-start gap-3">
                <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-red-500/20">
                  <X size={16} className="text-red-400" />
                </div>
                <div className="min-w-0">
                  <h3 className="text-sm font-semibold text-slate-100">Setup failed</h3>
                  <p role="alert" className="mt-2 rounded-lg bg-red-500/10 px-3 py-2 text-xs text-red-400">{setup.error}</p>
                </div>
              </div>
            )}
          </div>
        </Modal>
      )}
    </Section>
  )
}
