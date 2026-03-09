import { useState, useEffect, useCallback, useRef, type ChangeEvent } from 'react'
import { getPrefs, setNotifPref as apiSetNotifPref, setBriefingSchedule as apiSetBriefingSchedule, setBriefingTime as apiSetBriefingTime, getTags, createTag, updateTag, deleteTag } from '../api/client'
import type { Tag } from '../api/types'

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
    id: 'qwen35-9b',
    name: 'Qwen 3.5 · 9B',
    detail: '9B · Runs locally · ~6.6 GB download',
    type: 'local',
    provider: 'ollama',
    ollamaModel: 'qwen3.5:9b',
    available: true,
  },
  {
    id: 'qwen35-4b',
    name: 'Qwen 3.5 · 4B',
    detail: '4B · Runs locally · ~3.4 GB download',
    type: 'local',
    provider: 'ollama',
    ollamaModel: 'qwen3.5:4b',
    available: true,
  },
  {
    id: 'qwen35-2b',
    name: 'Qwen 3.5 · 2B',
    detail: '2B · Runs locally · ~2.7 GB download',
    type: 'local',
    provider: 'ollama',
    ollamaModel: 'qwen3.5:2b',
    available: true,
  },
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
// Tag colors
// ---------------------------------------------------------------------------

const TAG_COLORS: Record<string, { chip: string; dot: string }> = {
  indigo:  { chip: 'bg-indigo-500/20 text-indigo-300 border-indigo-500/30',  dot: 'bg-indigo-400' },
  emerald: { chip: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30', dot: 'bg-emerald-400' },
  rose:    { chip: 'bg-rose-500/20 text-rose-300 border-rose-500/30',    dot: 'bg-rose-400' },
  amber:   { chip: 'bg-amber-500/20 text-amber-300 border-amber-500/30',   dot: 'bg-amber-400' },
  violet:  { chip: 'bg-violet-500/20 text-violet-300 border-violet-500/30',  dot: 'bg-violet-400' },
  sky:     { chip: 'bg-sky-500/20 text-sky-300 border-sky-500/30',      dot: 'bg-sky-400' },
  slate:   { chip: 'bg-slate-500/20 text-slate-300 border-slate-500/30',   dot: 'bg-slate-400' },
}
const COLOR_OPTIONS = Object.keys(TAG_COLORS)

// ---------------------------------------------------------------------------
// TagsManager component
// ---------------------------------------------------------------------------

function TagsManager() {
  const [tags, setTags] = useState<Tag[]>([])
  const [loading, setLoading] = useState(true)

  // Create form
  const [newName, setNewName] = useState('')
  const [newColor, setNewColor] = useState('slate')
  const [newCategory, setNewCategory] = useState('')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState('')

  // Inline edit
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editName, setEditName] = useState('')
  const [editColor, setEditColor] = useState('slate')
  const [editCategory, setEditCategory] = useState('')
  const [saving, setSaving] = useState(false)

  // Delete confirm
  const [deletingId, setDeletingId] = useState<number | null>(null)

  useEffect(() => {
    getTags()
      .then(setTags)
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  // Group tags by category
  const grouped = tags.reduce<Record<string, Tag[]>>((acc, tag) => {
    const cat = tag.category?.trim() || 'Uncategorized'
    ;(acc[cat] = acc[cat] || []).push(tag)
    return acc
  }, {})
  // Sort so Uncategorized goes last
  const categories = Object.keys(grouped).sort((a, b) => {
    if (a === 'Uncategorized') return 1
    if (b === 'Uncategorized') return -1
    return a.localeCompare(b)
  })

  const startEdit = (tag: Tag) => {
    setEditingId(tag.id)
    setEditName(tag.name)
    setEditColor(tag.color)
    setEditCategory(tag.category || '')
  }

  const cancelEdit = () => setEditingId(null)

  const saveEdit = async (tagId: number) => {
    if (!editName.trim()) return
    setSaving(true)
    try {
      const updated = await updateTag(tagId, {
        name: editName.trim(),
        color: editColor,
        category: editCategory.trim() || null,
      })
      setTags((prev) => prev.map((t) => (t.id === updated.id ? updated : t)))
      setEditingId(null)
    } catch {
      // ignore
    } finally {
      setSaving(false)
    }
  }

  const confirmDelete = async (tagId: number) => {
    await deleteTag(tagId)
    setTags((prev) => prev.filter((t) => t.id !== tagId))
    setDeletingId(null)
  }

  const handleCreate = async () => {
    if (!newName.trim()) return
    setCreateError('')
    setCreating(true)
    try {
      const tag = await createTag(newName.trim(), newColor, newCategory.trim() || undefined)
      setTags((prev) => [...prev, tag])
      setNewName('')
      setNewColor('slate')
      setNewCategory('')
    } catch (e) {
      setCreateError(e instanceof Error ? e.message : 'Failed to create tag')
    } finally {
      setCreating(false)
    }
  }

  if (loading) {
    return <p className="mt-6 text-sm text-slate-400">Loading tags…</p>
  }

  return (
    <div className="max-w-2xl space-y-6">

      {/* ── Create new tag ── */}
      <div className="rounded-xl border border-slate-700 bg-slate-800 p-4">
        <h3 className="mb-3 text-sm font-medium text-slate-300">New Tag</h3>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <div className="flex-1">
            <label className="mb-1 block text-xs text-slate-400">Name</label>
            <input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
              placeholder="Tag name…"
              className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-sm text-slate-100 placeholder-slate-500 outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
            />
          </div>
          <div className="flex-1">
            <label className="mb-1 block text-xs text-slate-400">Category (optional)</label>
            <input
              value={newCategory}
              onChange={(e) => setNewCategory(e.target.value)}
              placeholder="e.g. Work, Personal…"
              className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-sm text-slate-100 placeholder-slate-500 outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs text-slate-400">Color</label>
            <div className="flex gap-1.5">
              {COLOR_OPTIONS.map((c) => (
                <button
                  key={c}
                  onClick={() => setNewColor(c)}
                  className={`h-6 w-6 rounded-full ${TAG_COLORS[c].dot} ring-offset-slate-800 ring-offset-2 transition ${newColor === c ? 'ring-2 ring-blue-400' : ''}`}
                  title={c}
                />
              ))}
            </div>
          </div>
          <button
            onClick={handleCreate}
            disabled={creating || !newName.trim()}
            className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
          >
            {creating ? 'Creating…' : 'Create'}
          </button>
        </div>
        {createError && <p className="mt-2 text-xs text-red-400">{createError}</p>}
      </div>

      {/* ── Tag list grouped by category ── */}
      {tags.length === 0 ? (
        <p className="text-sm text-slate-400">No tags yet.</p>
      ) : (
        categories.map((cat) => (
          <div key={cat} className="rounded-xl border border-slate-700 bg-slate-800">
            <div className="border-b border-slate-700 px-4 py-2.5">
              <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">{cat}</span>
            </div>
            <ul className="divide-y divide-slate-700/50">
              {grouped[cat].map((tag) => (
                <li key={tag.id} className="px-4 py-3">
                  {editingId === tag.id ? (
                    /* ── Inline edit row ── */
                    <div className="flex flex-col gap-2">
                      <div className="flex gap-2">
                        <input
                          value={editName}
                          onChange={(e) => setEditName(e.target.value)}
                          className="flex-1 rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-sm text-slate-100 outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
                        />
                        <input
                          value={editCategory}
                          onChange={(e) => setEditCategory(e.target.value)}
                          placeholder="Category"
                          className="flex-1 rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-sm text-slate-100 placeholder-slate-500 outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
                        />
                      </div>
                      <div className="flex items-center gap-3">
                        <div className="flex gap-1.5">
                          {COLOR_OPTIONS.map((c) => (
                            <button
                              key={c}
                              onClick={() => setEditColor(c)}
                              className={`h-5 w-5 rounded-full ${TAG_COLORS[c].dot} ring-offset-slate-800 ring-offset-2 transition ${editColor === c ? 'ring-2 ring-blue-400' : ''}`}
                              title={c}
                            />
                          ))}
                        </div>
                        <div className="ml-auto flex gap-2">
                          <button
                            onClick={cancelEdit}
                            className="rounded-lg px-3 py-1 text-xs text-slate-400 hover:bg-slate-700 hover:text-slate-200"
                          >
                            Cancel
                          </button>
                          <button
                            onClick={() => saveEdit(tag.id)}
                            disabled={saving || !editName.trim()}
                            className="rounded-lg bg-blue-600 px-3 py-1 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
                          >
                            {saving ? 'Saving…' : 'Save'}
                          </button>
                        </div>
                      </div>
                    </div>
                  ) : deletingId === tag.id ? (
                    /* ── Delete confirm ── */
                    <div className="flex items-center justify-between">
                      <p className="text-sm text-slate-300">
                        Delete <span className="font-medium text-slate-100">"{tag.name}"</span>? It will be removed from all chats.
                      </p>
                      <div className="flex gap-2 ml-4 shrink-0">
                        <button
                          onClick={() => setDeletingId(null)}
                          className="rounded-lg px-3 py-1 text-xs text-slate-400 hover:bg-slate-700 hover:text-slate-200"
                        >
                          Cancel
                        </button>
                        <button
                          onClick={() => confirmDelete(tag.id)}
                          className="rounded-lg bg-red-600 px-3 py-1 text-xs font-medium text-white hover:bg-red-500"
                        >
                          Delete
                        </button>
                      </div>
                    </div>
                  ) : (
                    /* ── Normal row ── */
                    <div className="flex items-center gap-3">
                      <span className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium ${TAG_COLORS[tag.color]?.chip ?? TAG_COLORS.slate.chip}`}>
                        {tag.name}
                      </span>
                      {tag.is_default && (
                        <span className="text-xs text-slate-500">default</span>
                      )}
                      <div className="ml-auto flex gap-1.5">
                        <button
                          onClick={() => startEdit(tag)}
                          className="rounded-lg px-2.5 py-1 text-xs text-slate-400 hover:bg-slate-700 hover:text-slate-200"
                        >
                          Edit
                        </button>
                        <button
                          onClick={() => setDeletingId(tag.id)}
                          className="rounded-lg px-2.5 py-1 text-xs text-red-400 hover:bg-red-900/30 hover:text-red-300"
                        >
                          Delete
                        </button>
                      </div>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ))
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main Settings component
// ---------------------------------------------------------------------------

export default function Settings() {
  // Tab state
  const [settingsTab, setSettingsTab] = useState<'general' | 'tags'>('general')

  // Orchestrator state
  const [orchestratorId, setOrchestratorId] = useState<string | null>(null)
  const [pendingModelId, setPendingModelId] = useState<string | null>(null)
  const [loadingCfg, setLoadingCfg] = useState(true)

  // Download modal state
  const [setup, setSetup] = useState<SetupState | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  // Ollama server status
  const [ollamaRunning, setOllamaRunning] = useState<boolean | null>(null)
  const [ollamaAction, setOllamaAction] = useState<'stopping' | 'restarting' | null>(null)

  // Notification preference
  const [notifPref, setNotifPref] = useState<NotifPref>('tracked')

  // Briefing schedule preference
  const [briefingSchedule, setBriefingSchedule] = useState<'daily' | 'weekly'>('daily')
  const [briefingTime, setBriefingTime] = useState<string>('09:00')

  const refreshOllamaStatus = useCallback(async () => {
    try {
      const { running } = await apiFetch<{ running: boolean }>('GET', '/api/llm/ollama/status')
      setOllamaRunning(running)
    } catch {
      setOllamaRunning(false)
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
      .catch(() => {})
      .finally(() => setLoadingCfg(false))

    // Load saved preferences from backend
    getPrefs()
      .then((prefs: Record<string, string>) => {
        if (prefs.notif_pref) setNotifPref(prefs.notif_pref as NotifPref)
        if (prefs.briefing_schedule) setBriefingSchedule(prefs.briefing_schedule as 'daily' | 'weekly')
        if (prefs.briefing_time) setBriefingTime(prefs.briefing_time)
      })
      .catch(() => {})

    // Check Ollama status on mount
    refreshOllamaStatus()
  }, [refreshOllamaStatus])

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

  const handleStopOllama = async () => {
    setOllamaAction('stopping')
    try {
      await apiFetch('POST', '/api/llm/ollama/stop')
      await new Promise((r) => setTimeout(r, 800))
      await refreshOllamaStatus()
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Failed to stop Ollama')
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
        await new Promise((r) => setTimeout(r, 500))
        const { running } = await apiFetch<{ running: boolean }>('GET', '/api/llm/ollama/status').catch(() => ({ running: false }))
        if (running) { setOllamaRunning(true); break }
      }
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Failed to restart Ollama')
    } finally {
      setOllamaAction(null)
      refreshOllamaStatus()
    }
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

  const handleBriefingScheduleChange = async (e: ChangeEvent<HTMLSelectElement>) => {
    const schedule = e.target.value as 'daily' | 'weekly'
    setBriefingSchedule(schedule)
    apiSetBriefingSchedule(schedule).catch(() => {})
  }

  const handleBriefingTimeChange = async (e: ChangeEvent<HTMLInputElement>) => {
    const time = e.target.value  // HH:MM
    setBriefingTime(time)
    apiSetBriefingTime(time).catch(() => {})
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
    <div className="flex flex-1 flex-col overflow-y-auto">
      {/* Header */}
      <div className="border-b border-slate-700/50 px-8 pt-8 pb-0">
        <h1 className="mb-4 text-lg font-semibold text-slate-100">Settings</h1>
        {/* Tab bar */}
        <div className="flex gap-1">
          {(['general', 'tags'] as const).map((tab) => (
            <button
              key={tab}
              onClick={() => setSettingsTab(tab)}
              className={`rounded-t-lg px-4 py-2 text-sm font-medium capitalize transition ${
                settingsTab === tab
                  ? 'bg-white text-gray-900'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              {tab}
            </button>
          ))}
        </div>
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-y-auto p-8">
        {settingsTab === 'general' && (
          <>
            {/* ── AI Models ── */}
            <section className="mb-8 max-w-xl">
              <h2 className="mb-1 text-sm font-medium text-slate-300">AI Models</h2>
              <p className="mb-4 text-xs text-slate-400">
                Select the orchestrator model that powers AI features in Mingly.
              </p>

              <div className="overflow-hidden rounded-xl border border-slate-700 bg-slate-800 p-4">
                <label className="mb-1.5 block text-xs font-medium text-slate-400">
                  Orchestrator LLM
                </label>
                <select
                  value={selectValue}
                  onChange={handleModelSelectChange}
                  className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2.5 text-sm text-slate-100 outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 cursor-pointer"
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
                  <div className="mt-3 flex items-center justify-between rounded-lg bg-blue-900/40 border border-blue-700/50 px-3 py-2.5">
                    <span className="text-sm text-blue-300">
                      {pendingModel?.type === 'local'
                        ? `Set up ${pendingModel?.name} locally?`
                        : `Switch to ${pendingModel?.name}?`}
                    </span>
                    <div className="flex gap-2">
                      <button
                        onClick={handleCancelPending}
                        className="rounded-lg px-3 py-1.5 text-xs font-medium text-slate-400 hover:bg-slate-700 hover:text-slate-200"
                      >
                        Cancel
                      </button>
                      <button
                        onClick={handleConfirmSelection}
                        disabled={!pendingModel?.available}
                        className="rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
                      >
                        {pendingModel?.type === 'local' ? 'Download & Set up' : 'Apply'}
                      </button>
                    </div>
                  </div>
                )}

                {/* Current status */}
                {orchestratorId && !pendingModelId && (
                  <div className="mt-3 flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className={`inline-block h-1.5 w-1.5 rounded-full ${ollamaRunning ? 'bg-emerald-400' : 'bg-slate-500'}`} />
                      <p className={`text-xs ${ollamaRunning ? 'text-emerald-400' : 'text-slate-500'}`}>
                        {ollamaRunning
                          ? `${MODELS.find((m) => m.id === orchestratorId)?.name} is running`
                          : 'Ollama server stopped'}
                      </p>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <button
                        onClick={handleRestartOllama}
                        disabled={ollamaAction !== null}
                        title="Restart Ollama server"
                        className="rounded px-2 py-1 text-xs text-slate-400 hover:bg-slate-700 hover:text-slate-200 disabled:opacity-40 transition-colors"
                      >
                        {ollamaAction === 'restarting' ? 'Restarting…' : 'Restart'}
                      </button>
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
                      {!ollamaRunning && ollamaRunning !== null && (
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
              </div>
            </section>

            {/* ── Notifications ── */}
            <section className="max-w-xl">
              <h2 className="mb-1 text-sm font-medium text-slate-300">Notifications</h2>
              <p className="mb-4 text-xs text-slate-400">
                Get notified when new messages arrive.
              </p>

              <div className="overflow-hidden rounded-xl border border-slate-700 bg-slate-800 p-4">
                <label className="mb-1.5 block text-xs font-medium text-slate-400">
                  Notify me about
                </label>
                <select
                  value={notifPref}
                  onChange={handleNotifPrefChange}
                  className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2.5 text-sm text-slate-100 outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 cursor-pointer"
                >
                  <option value="all">All new messages</option>
                  <option value="tracked">Tracked chats only</option>
                  <option value="off">Off</option>
                </select>

                {notifPref !== 'off' && (
                  <p className="mt-2 text-xs text-emerald-400">✓ Native notifications enabled</p>
                )}
              </div>
            </section>

            {/* ── Briefing Schedule ── */}
            <section className="mt-8 max-w-xl">
              <h2 className="mb-1 text-sm font-medium text-slate-300">Briefing Schedule</h2>
              <p className="mb-4 text-xs text-slate-400">
                How often the Intelligence → Briefing tab auto-updates chat summaries.
                The update runs at the configured time, or on first app open after that time.
              </p>
              <div className="overflow-hidden rounded-xl border border-slate-700 bg-slate-800 p-4 flex flex-col gap-4">
                <div>
                  <label className="mb-1.5 block text-xs font-medium text-slate-400">
                    Update frequency
                  </label>
                  <select
                    value={briefingSchedule}
                    onChange={handleBriefingScheduleChange}
                    className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2.5 text-sm text-slate-100 outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 cursor-pointer"
                  >
                    <option value="daily">Daily (last 24 hours)</option>
                    <option value="weekly">Weekly (last 7 days)</option>
                  </select>
                  <p className="mt-2 text-xs text-slate-500">
                    {briefingSchedule === 'daily'
                      ? 'Summarises the past day of messages each morning.'
                      : 'Summarises the past week of messages each Monday morning.'}
                  </p>
                </div>
                <div>
                  <label className="mb-1.5 block text-xs font-medium text-slate-400">
                    Run time (local time)
                  </label>
                  <input
                    type="time"
                    value={briefingTime}
                    onChange={handleBriefingTimeChange}
                    className="rounded-lg border border-slate-600 bg-slate-700 px-3 py-2.5 text-sm text-slate-100 outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 cursor-pointer"
                  />
                  <p className="mt-2 text-xs text-slate-500">
                    The briefing will auto-run at this time each {briefingSchedule === 'weekly' ? 'Monday' : 'day'}, or on first app open after this time.
                  </p>
                </div>
              </div>
            </section>
          </>
        )}

        {settingsTab === 'tags' && (
          <>
            <p className="mb-6 text-xs text-slate-400">
              Manage tags for organizing chats. Tags can be assigned to chats from the chat info panel.
            </p>
            <TagsManager />
          </>
        )}
      </div>

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
