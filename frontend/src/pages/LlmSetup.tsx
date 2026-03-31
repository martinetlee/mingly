import { useState, useEffect, useCallback, useRef } from 'react'
import logo from '../assets/logo.png'
import { getSessionToken, notifySessionExpired } from '../api/session'
import { Cpu, Shield, MessageSquare, Sparkles, Check, AlertCircle, Loader2 } from 'lucide-react'

// ---------------------------------------------------------------------------
// API helpers (self-contained for SSE streaming)
// ---------------------------------------------------------------------------

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const h: Record<string, string> = { ...extra }
  const token = getSessionToken()
  if (token) h['X-Session-Token'] = token
  return h
}

async function apiFetch<T>(
  method: string,
  path: string,
  body?: object,
  signal?: AbortSignal,
): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: authHeaders(body ? { 'Content-Type': 'application/json' } : undefined),
    body: body ? JSON.stringify(body) : undefined,
    signal,
  })
  if (res.status === 401) {
    notifySessionExpired()
    throw new Error('Session expired')
  }
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
  const res = await fetch(url, { headers: authHeaders(), signal })
  if (res.status === 401) {
    notifySessionExpired()
    throw new Error('Session expired')
  }
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
// Types
// ---------------------------------------------------------------------------

type SetupPhase =
  | 'checking'
  | 'install-binary'
  | 'start-server'
  | 'pull-model'
  | 'registering'
  | 'done'
  | 'error'

interface SetupProgress {
  phase: SetupPhase
  label: string
  pct: number
  error?: string
}

interface Props {
  onComplete: () => void
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function LlmSetup({ onComplete }: Props) {
  const [status, setStatus] = useState<'intro' | 'setting-up' | 'ready' | 'skipped'>('intro')
  const [progress, setProgress] = useState<SetupProgress | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  // Check on mount if already set up
  useEffect(() => {
    (async () => {
      try {
        const { instances } = await apiFetch<{ instances: { name: string; model: string; healthy: boolean }[] }>('GET', '/api/llm/instances')
        const local = instances.find((i) => i.name === 'local')
        if (local?.healthy) {
          // Already configured and healthy — skip this page
          onComplete()
        }
      } catch {
        // ignore — let user set up manually
      }
    })()
  }, [onComplete])

  const runSetup = useCallback(async () => {
    setStatus('setting-up')
    const ctrl = new AbortController()
    abortRef.current = ctrl
    const signal = ctrl.signal

    const update = (phase: SetupPhase, label: string, pct: number, error?: string) =>
      setProgress({ phase, label, pct, error })

    try {
      // 1. Check Ollama binary & server
      update('checking', 'Checking Ollama installation…', 0)
      const installStatus = await apiFetch<{
        binary_found: boolean
        server_running: boolean
      }>('GET', '/api/llm/ollama/install-status', undefined, signal)

      // 2. Install binary if needed
      if (!installStatus.binary_found) {
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

      // 3. Start server if needed
      if (!installStatus.server_running) {
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

      // 4. Check if model already downloaded
      let needsPull = true
      try {
        const { models } = await apiFetch<{ models: { name: string }[] }>('GET', '/api/llm/ollama/models', undefined, signal)
        needsPull = !models.some((m) => m.name === 'qwen2.5:7b' || m.name.startsWith('qwen2.5:7b'))
      } catch {
        // assume needs pull
      }

      // 5. Pull model if needed
      if (needsPull) {
        update('pull-model', 'Downloading Qwen 2.5 7B model…', 0)
        const { job_id: pullId } = await apiFetch<{ job_id: number }>(
          'POST', '/api/llm/ollama/pull', { model: 'qwen2.5:7b' }, signal,
        )
        for await (const ev of streamSSE(`/api/llm/jobs/${pullId}`, signal)) {
          if (ev.status === 'error')
            throw new Error((ev.error as string) || 'Model download failed')
          if (typeof ev.total === 'number' && ev.total > 0 && typeof ev.completed === 'number') {
            update('pull-model', 'Downloading Qwen 2.5 7B model…',
              Math.round((ev.completed / ev.total) * 100))
          }
          if (ev.status === 'done') break
        }
      }

      // 6. Register instance
      update('registering', 'Configuring AI model…', 0)
      try {
        await apiFetch('POST', '/api/llm/instances', {
          name: 'local',
          provider: 'ollama',
          model: 'qwen2.5:7b',
          base_url: 'http://localhost:11434/v1',
        }, signal)
      } catch (e) {
        if (!(e instanceof Error && e.message.toLowerCase().includes('already'))) throw e
      }
      await apiFetch('PUT', '/api/llm/orchestrator', {
        orchestrator_name: 'local',
        orchestrator_prompt: '',
      }, signal)

      update('done', 'AI is ready', 100)
      setStatus('ready')
    } catch (e) {
      if (e instanceof DOMException && e.name === 'AbortError') {
        setStatus('intro')
        setProgress(null)
        return
      }
      update('error', '', 0, e instanceof Error ? e.message : 'Setup failed')
    }
  }, [])

  const handleSkip = () => {
    if (abortRef.current) abortRef.current.abort()
    onComplete()
  }

  const phaseLabel = (phase: SetupPhase): string => {
    switch (phase) {
      case 'checking': return 'Checking'
      case 'install-binary': return 'Installing Ollama'
      case 'start-server': return 'Starting server'
      case 'pull-model': return 'Downloading model'
      case 'registering': return 'Configuring'
      case 'done': return 'Complete'
      case 'error': return 'Error'
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-slate-950 via-slate-900 to-indigo-950 p-6">
      <div className="w-full max-w-lg">
        {/* Logo */}
        <div className="mb-6 flex justify-center">
          <img src={logo} alt="Mingly" className="h-10 w-10 rounded-xl" />
        </div>

        <div className="rounded-2xl border border-slate-700/50 bg-slate-800/50 p-6 shadow-xl backdrop-blur-sm">
          {status === 'intro' && (
            <div className="space-y-6">
              <div className="text-center">
                <h2 className="text-lg font-semibold text-slate-200">Set Up Local AI</h2>
                <p className="mt-1 text-xs text-slate-500">
                  Your messages are syncing in the background. Let's set up AI while you wait.
                </p>
              </div>

              {/* Why this matters */}
              <div className="space-y-3">
                <p className="text-xs font-medium text-slate-400 uppercase tracking-wider">Why local AI?</p>
                <div className="space-y-2.5">
                  <div className="flex items-start gap-3 rounded-lg bg-slate-900/50 px-3 py-2.5 border border-slate-700/50">
                    <Shield size={16} className="mt-0.5 flex-shrink-0 text-red-400" />
                    <div>
                      <p className="text-xs font-medium text-slate-300">Scam Detection</p>
                      <p className="text-[11px] text-slate-500">Messages are analyzed locally to detect scam attempts, phishing, and suspicious behavior — without sending your conversations to any server.</p>
                    </div>
                  </div>
                  <div className="flex items-start gap-3 rounded-lg bg-slate-900/50 px-3 py-2.5 border border-slate-700/50">
                    <MessageSquare size={16} className="mt-0.5 flex-shrink-0 text-indigo-400" />
                    <div>
                      <p className="text-xs font-medium text-slate-300">Smart Summaries & Briefings</p>
                      <p className="text-[11px] text-slate-500">Get daily briefings, catch-up summaries, and message importance scoring — all processed on your machine.</p>
                    </div>
                  </div>
                  <div className="flex items-start gap-3 rounded-lg bg-slate-900/50 px-3 py-2.5 border border-slate-700/50">
                    <Sparkles size={16} className="mt-0.5 flex-shrink-0 text-amber-400" />
                    <div>
                      <p className="text-xs font-medium text-slate-300">100% Private</p>
                      <p className="text-[11px] text-slate-500">Qwen 2.5 7B runs entirely on your computer. Your messages never leave your device for AI processing.</p>
                    </div>
                  </div>
                </div>
              </div>

              {/* Model info */}
              <div className="rounded-lg bg-indigo-500/10 border border-indigo-500/20 px-3 py-2.5">
                <div className="flex items-center gap-2">
                  <Cpu size={14} className="text-indigo-400" />
                  <span className="text-xs font-medium text-indigo-300">Qwen 2.5 · 7B</span>
                </div>
                <p className="mt-1 text-[11px] text-slate-500">
                  ~4 GB download · Runs locally via Ollama · Q4 quantized for efficiency
                </p>
              </div>

              {/* Actions */}
              <div className="flex flex-col gap-2">
                <button
                  onClick={runSetup}
                  className="w-full rounded-lg bg-gradient-to-r from-indigo-500 to-purple-500 py-2.5 text-sm font-medium text-white shadow-lg shadow-indigo-500/25 transition hover:from-indigo-400 hover:to-purple-400"
                >
                  Set Up AI Model
                </button>
                <button
                  onClick={handleSkip}
                  className="text-xs text-slate-500 hover:text-slate-400 transition-colors py-1"
                >
                  Skip for now — I'll set this up later in Settings
                </button>
              </div>
            </div>
          )}

          {status === 'setting-up' && progress && (
            <div className="space-y-5">
              <div className="text-center">
                <h2 className="text-lg font-semibold text-slate-200">Setting Up AI</h2>
                <p className="mt-1 text-xs text-slate-500">This may take a few minutes depending on your connection.</p>
              </div>

              {/* Progress steps */}
              <div className="space-y-2">
                {(['install-binary', 'start-server', 'pull-model', 'registering'] as const).map((phase) => {
                  const phases: SetupPhase[] = ['checking', 'install-binary', 'start-server', 'pull-model', 'registering', 'done']
                  const currentIdx = phases.indexOf(progress.phase)
                  const phaseIdx = phases.indexOf(phase)
                  const isActive = progress.phase === phase
                  const isDone = currentIdx > phaseIdx || progress.phase === 'done'
                  const isFailed = progress.phase === 'error' && isActive

                  return (
                    <div key={phase} className={`flex items-center gap-3 rounded-lg px-3 py-2 transition-colors ${
                      isActive ? 'bg-slate-900/80 border border-indigo-500/30' :
                      isDone ? 'bg-slate-900/30' : 'opacity-40'
                    }`}>
                      <div className="flex-shrink-0 w-5 h-5 flex items-center justify-center">
                        {isDone ? (
                          <Check size={14} className="text-emerald-400" />
                        ) : isActive ? (
                          <Loader2 size={14} className="text-indigo-400 animate-spin" />
                        ) : isFailed ? (
                          <AlertCircle size={14} className="text-red-400" />
                        ) : (
                          <div className="h-1.5 w-1.5 rounded-full bg-slate-600" />
                        )}
                      </div>
                      <span className={`text-xs ${isDone ? 'text-slate-400' : isActive ? 'text-slate-200' : 'text-slate-500'}`}>
                        {phaseLabel(phase)}
                      </span>
                      {isActive && progress.pct > 0 && progress.pct < 100 && (
                        <span className="ml-auto text-[10px] text-indigo-400">{progress.pct}%</span>
                      )}
                    </div>
                  )
                })}
              </div>

              {/* Progress bar */}
              {progress.phase !== 'error' && (
                <div className="space-y-1">
                  <div className="h-1.5 w-full rounded-full bg-slate-700 overflow-hidden">
                    <div
                      className="h-full rounded-full bg-gradient-to-r from-indigo-500 to-purple-500 transition-all duration-300"
                      style={{ width: `${progress.pct}%` }}
                    />
                  </div>
                  <p className="text-[10px] text-slate-500 text-center">{progress.label}</p>
                </div>
              )}

              {/* Error state */}
              {progress.phase === 'error' && (
                <div className="space-y-3">
                  <div className="rounded-lg bg-red-500/10 border border-red-500/30 px-3 py-2.5">
                    <p className="text-xs text-red-300">{progress.error}</p>
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={() => { setProgress(null); runSetup() }}
                      className="flex-1 rounded-lg bg-indigo-600 py-2 text-xs font-medium text-white hover:bg-indigo-500"
                    >
                      Retry
                    </button>
                    <button
                      onClick={handleSkip}
                      className="flex-1 rounded-lg border border-slate-600 py-2 text-xs font-medium text-slate-400 hover:bg-slate-700"
                    >
                      Skip for now
                    </button>
                  </div>
                </div>
              )}

              {/* Cancel */}
              {progress.phase !== 'error' && (
                <button
                  onClick={() => { if (abortRef.current) abortRef.current.abort() }}
                  className="w-full text-xs text-slate-500 hover:text-slate-400 transition-colors py-1"
                >
                  Cancel
                </button>
              )}
            </div>
          )}

          {status === 'ready' && (
            <div className="space-y-5">
              <div className="text-center">
                <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-emerald-500/20 text-emerald-400 ring-1 ring-emerald-500/30">
                  <Check size={24} />
                </div>
                <h2 className="text-lg font-semibold text-slate-200">AI is Ready</h2>
                <p className="mt-1 text-xs text-slate-500">
                  Qwen 2.5 7B is running locally. Scam detection, summaries, and briefings are now enabled.
                </p>
              </div>
              <button
                onClick={onComplete}
                className="w-full rounded-lg bg-gradient-to-r from-indigo-500 to-purple-500 py-2.5 text-sm font-medium text-white shadow-lg shadow-indigo-500/25 transition hover:from-indigo-400 hover:to-purple-400"
              >
                Get Started
              </button>
            </div>
          )}
        </div>

        <p className="mt-4 text-center text-xs text-slate-600">
          You can change the AI model anytime in Settings
        </p>
      </div>
    </div>
  )
}
