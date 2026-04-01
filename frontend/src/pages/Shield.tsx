import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { Shield as ShieldIcon, ShieldAlert, ShieldCheck, X, ExternalLink, Settings, ChevronDown, Flag, FileText, GitBranch, RefreshCw, Cloud, CloudOff, History, MessageSquare, Send } from 'lucide-react'
import type { ScamFlag, ScannedMessage, ScamReport, SyncHistoryStats, WSEvent } from '../api/types'
import { getScamFlags, dismissScamFlag, getScannedMessages, clearMessageScam, checkMessagesForScam, getScamDetectionSettings, setScamDetectionSettings, getScamReports, getScamPipeline, getShieldCloudSettings, setShieldCloudSettings, triggerSync, triggerMassScan, getSyncHistory, submitFeedback } from '../api/client'
import { useWebSocket } from '../hooks/useWebSocket'

function stripFormatting(text: string): string {
  return text
    .replace(/\[(\*{0,2})(.*?)\1\]\([^)]*\)/g, '$2')
    .replace(/\*\*(.*?)\*\*/g, '$1')
    .replace(/__(.*?)__/g, '$1')
    .replace(/`([^`]*)`/g, '$1')
    .replace(/[*_~`]/g, '')
}

function timeAgo(iso: string): string {
  // Backend stores dates as UTC without Z suffix — ensure correct parsing
  const dateStr = iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z'
  const diff = Date.now() - new Date(dateStr).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  if (days < 30) return `${days}d ago`
  return new Date(iso).toLocaleDateString([], { month: 'short', day: 'numeric' })
}

function ConfidencePill({ value }: { value: number | null }) {
  if (value == null) return <span className="text-slate-500">—</span>
  const pct = Math.round(value * 100)
  const cls = value > 0.7 ? 'text-red-400' : value > 0.4 ? 'text-amber-400' : 'text-yellow-400'
  return (
    <div className="flex items-center gap-1.5">
      <div className="h-1 w-12 rounded-full bg-slate-700 overflow-hidden">
        <div
          className={`h-full rounded-full ${value > 0.7 ? 'bg-red-500' : value > 0.4 ? 'bg-amber-500' : 'bg-yellow-500'}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className={`text-[10px] font-medium ${cls}`}>{pct}%</span>
    </div>
  )
}

function SyncBadge({ synced }: { synced: boolean }) {
  return synced ? (
    <span className="inline-flex items-center gap-0.5 text-[10px] text-emerald-400" title="Synced to cloud">
      <Cloud size={10} />
    </span>
  ) : (
    <span className="inline-flex items-center gap-0.5 text-[10px] text-slate-600" title="Not synced">
      <CloudOff size={10} />
    </span>
  )
}

type Tab = 'flagged' | 'scanned' | 'reports' | 'sync' | 'pipeline'

export default function Shield() {
  const navigate = useNavigate()
  const [tab, setTab] = useState<Tab>('flagged')
  const [devMode, setDevMode] = useState(() => localStorage.getItem('developer_mode') === 'true')

  // Listen for developer mode changes from Settings
  useEffect(() => {
    const handler = () => setDevMode(localStorage.getItem('developer_mode') === 'true')
    window.addEventListener('developer_mode_changed', handler)
    return () => window.removeEventListener('developer_mode_changed', handler)
  }, [])

  // Flagged scammers
  const [flags, setFlags] = useState<ScamFlag[]>([])
  const [loadingFlags, setLoadingFlags] = useState(true)

  // Scanned messages
  const [scanned, setScanned] = useState<ScannedMessage[]>([])
  const [loadingScanned, setLoadingScanned] = useState(true)
  const [scanFilter, setScanFilter] = useState<'all' | 'scam' | 'failed'>('all')
  const [expandedTrace, setExpandedTrace] = useState<string | null>(null) // "chatId-msgId"
  const [scannedPage, setScannedPage] = useState(1)
  const [scannedTotal, setScannedTotal] = useState(0)
  const PAGE_SIZE = 200

  // Reports
  const [reports, setReports] = useState<ScamReport[]>([])
  const [loadingReports, setLoadingReports] = useState(true)

  // Pipeline visualization
  const [pipelineData, setPipelineData] = useState<{
    version: string
    updated_at: string
    nodes: { id: string; type: string; config: Record<string, any>; short_circuit?: any; output_forward?: string[]; output_trace_only?: string[] }[]
    edges: { from: string; to: string }[]
    output: string
    tools?: { name: string; script: string; description?: string; script_params?: Record<string, any> }[]
  } | null>(null)
  const [loadingPipeline, setLoadingPipeline] = useState(true)
  const [selectedNode, setSelectedNode] = useState<string | null>(null)

  // Detection settings
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [scamMode, setScamMode] = useState<'local_llm' | 'external'>('local_llm')
  const [scamApiUrl, setScamApiUrl] = useState('')
  const [scamApiKeySet, setScamApiKeySet] = useState(false)
  const [scamApiKey, setScamApiKey] = useState('')
  const [scamMaxTokens, setScamMaxTokens] = useState(200)
  const [scamSaving, setScamSaving] = useState(false)
  const [scamSaveMsg, setScamSaveMsg] = useState<string | null>(null)

  // Sync history
  const [syncStats, setSyncStats] = useState<SyncHistoryStats | null>(null)
  const [loadingSyncStats, setLoadingSyncStats] = useState(true)

  // Feedback
  const [feedbackOpen, setFeedbackOpen] = useState(false)
  const [feedbackType, setFeedbackType] = useState('general')
  const [feedbackMsg, setFeedbackMsg] = useState('')
  const [feedbackSending, setFeedbackSending] = useState(false)
  const [feedbackResult, setFeedbackResult] = useState<string | null>(null)

  // Cloud sync
  const [cloudUrl, setCloudUrl] = useState('')
  const [cloudApiKey, setCloudApiKey] = useState('')
  const [cloudKeySet, setCloudKeySet] = useState(false)
  const [cloudLastSynced, setCloudLastSynced] = useState<string | null>(null)
  const [syncDismissals, setSyncDismissals] = useState(false)
  const [cloudSaving, setCloudSaving] = useState(false)
  const [cloudSaveMsg, setCloudSaveMsg] = useState<string | null>(null)
  const [syncing, setSyncing] = useState(false)
  const [syncResult, setSyncResult] = useState<string | null>(null)

  const loadFlags = useCallback(() => {
    getScamFlags()
      .then((data) => setFlags(data.flags))
      .catch(() => {})
      .finally(() => setLoadingFlags(false))
  }, [])

  const loadScanned = useCallback((page: number = 1) => {
    setLoadingScanned(true)
    getScannedMessages(page, PAGE_SIZE)
      .then((data) => {
        setScanned(data.messages)
        setScannedTotal(data.total)
        setScannedPage(data.page)
      })
      .catch(() => {})
      .finally(() => setLoadingScanned(false))
  }, [])

  const loadReports = useCallback(() => {
    getScamReports()
      .then((data) => setReports(data.reports))
      .catch(() => {})
      .finally(() => setLoadingReports(false))
  }, [])

  useEffect(() => {
    loadFlags()
    loadScanned()
    loadReports()
    getScamPipeline()
      .then((data) => setPipelineData(data))
      .catch(() => {})
      .finally(() => setLoadingPipeline(false))
    getScamDetectionSettings()
      .then((s) => {
        setScamMode(s.mode as 'local_llm' | 'external')
        setScamApiUrl(s.api_url)
        setScamApiKeySet(s.api_key_set)
        setScamMaxTokens(s.max_tokens || 200)
      })
      .catch(() => {})
    getShieldCloudSettings()
      .then((s) => {
        setCloudUrl(s.cloud_url)
        setCloudKeySet(s.api_key_set)
        setCloudLastSynced(s.last_synced_at)
        setSyncDismissals(s.sync_dismissals ?? false)
      })
      .catch(() => {})
    getSyncHistory()
      .then(setSyncStats)
      .catch(() => {})
      .finally(() => setLoadingSyncStats(false))
  }, [loadFlags, loadScanned, loadReports])

  useWebSocket(useCallback((event: WSEvent) => {
    if (event.type === 'scam_users_flagged' || event.type === 'scam_detected') {
      loadFlags()
      loadScanned()
    }
  }, [loadFlags, loadScanned]))

  const handleDismissFlag = async (telegramId: number) => {
    setFlags((prev) => prev.filter((f) => f.telegram_id !== telegramId))
    try { await dismissScamFlag(telegramId) } catch { loadFlags() }
  }

  const handleClearMessage = async (chatId: number, tgMessageId: number) => {
    setScanned((prev) => prev.filter((m) => !(m.chat_id === chatId && m.tg_message_id === tgMessageId)))
    try { await clearMessageScam(chatId, tgMessageId) } catch { loadScanned() }
  }

  const [rescanning, setRescanning] = useState<Set<string>>(new Set())
  const [massScanning, setMassScanning] = useState(false)
  const [massScanResult, setMassScanResult] = useState<string | null>(null)

  const handleRescan = async (chatId: number, tgMessageId: number) => {
    const key = `${chatId}-${tgMessageId}`
    setRescanning(prev => new Set(prev).add(key))
    try {
      await checkMessagesForScam(chatId, [tgMessageId])
      // Result arrives via WebSocket — reload after a short delay
      setTimeout(() => loadScanned(scannedPage), 3000)
    } catch { /* ignore */ }
    finally {
      setRescanning(prev => { const next = new Set(prev); next.delete(key); return next })
    }
  }

  const goToMessage = (chatId: number, messageId: number) => {
    navigate(`/chats?chatId=${chatId}&msgId=${messageId}`)
  }

  const filteredScanned = scanFilter === 'scam'
    ? scanned.filter((m) => m.scam_score > 0.5)
    : scanFilter === 'failed'
    ? scanned.filter((m) => { try { return JSON.parse(m.trace_json || '{}').status === 'failed' } catch { return false } })
    : scanned
  const scamCount = scanned.filter((m) => m.scam_score > 0.5).length
  const failedCount = scanned.filter((m) => { try { return JSON.parse(m.trace_json || '{}').status === 'failed' } catch { return false } }).length

  return (
    <div className="flex h-full flex-1 flex-col overflow-hidden">
      {/* Header */}
      <header className="flex items-center gap-3 border-b border-slate-700 bg-slate-900 px-6 py-3">
        <ShieldAlert size={18} className="text-red-400" />
        <h1 className="text-sm font-semibold text-slate-200">Shield</h1>
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={() => { setFeedbackOpen((v) => !v); setFeedbackResult(null) }}
            className={`rounded-lg p-1.5 transition-colors ${feedbackOpen ? 'bg-slate-700 text-slate-200' : 'text-slate-500 hover:bg-slate-800 hover:text-slate-300'}`}
            title="Send feedback"
          >
            <MessageSquare size={15} />
          </button>
          <button
            onClick={() => setSettingsOpen((v) => !v)}
            className={`rounded-lg p-1.5 transition-colors ${settingsOpen ? 'bg-slate-700 text-slate-200' : 'text-slate-500 hover:bg-slate-800 hover:text-slate-300'}`}
            title="Detection settings"
          >
            <Settings size={15} />
          </button>
        </div>
      </header>

      {/* Settings panel (collapsible) */}
      {settingsOpen && (
        <div className="border-b border-slate-700 bg-slate-800/50 px-6 py-4">
          <div className="max-w-xl space-y-3">
            <h3 className="text-xs font-medium text-slate-400">Scam Detection Settings</h3>
            <div className="flex items-center gap-3">
              <select
                value={scamMode}
                onChange={(e) => { setScamMode(e.target.value as 'local_llm' | 'external'); setScamSaveMsg(null) }}
                className="rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-xs text-slate-200 focus:border-emerald-500 focus:outline-none"
              >
                <option value="local_llm">Local LLM</option>
                <option value="external">External API</option>
              </select>
              {scamMode === 'local_llm' && (
                <div className="flex items-center gap-1.5">
                  <label className="text-[10px] text-slate-500 whitespace-nowrap">Max tokens</label>
                  <input
                    type="number"
                    min={50}
                    max={2000}
                    step={50}
                    value={scamMaxTokens}
                    onChange={(e) => { setScamMaxTokens(Number(e.target.value)); setScamSaveMsg(null) }}
                    className="w-20 rounded-lg border border-slate-600 bg-slate-700 px-2 py-1.5 text-xs text-slate-200 focus:border-emerald-500 focus:outline-none"
                  />
                </div>
              )}
              {scamMode === 'external' && (
                <>
                  <input
                    type="url"
                    placeholder="API endpoint URL"
                    value={scamApiUrl}
                    onChange={(e) => { setScamApiUrl(e.target.value); setScamSaveMsg(null) }}
                    className="flex-1 rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-xs text-slate-200 placeholder-slate-500 focus:border-emerald-500 focus:outline-none"
                  />
                  <input
                    type="password"
                    placeholder={scamApiKeySet ? '••••••••' : 'API key'}
                    value={scamApiKey}
                    onChange={(e) => { setScamApiKey(e.target.value); setScamSaveMsg(null) }}
                    className="w-36 rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-xs text-slate-200 placeholder-slate-500 focus:border-emerald-500 focus:outline-none"
                  />
                </>
              )}
              <button
                disabled={scamSaving || (scamMode === 'external' && !scamApiUrl.trim())}
                onClick={async () => {
                  setScamSaving(true); setScamSaveMsg(null)
                  try {
                    await setScamDetectionSettings(scamMode, scamApiUrl, scamApiKey, scamMaxTokens)
                    setScamSaveMsg('Saved')
                    if (scamApiKey) setScamApiKeySet(true)
                    setScamApiKey('')
                  } catch (err) {
                    setScamSaveMsg(err instanceof Error ? err.message : 'Failed')
                  } finally { setScamSaving(false) }
                }}
                className="rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-500 disabled:opacity-40"
              >
                {scamSaving ? 'Saving…' : 'Save'}
              </button>
              {scamSaveMsg && (
                <span className={`text-xs ${scamSaveMsg === 'Saved' ? 'text-emerald-400' : 'text-red-400'}`}>{scamSaveMsg}</span>
              )}
            </div>

            {/* Cloud Sync */}
            <h3 className="text-xs font-medium text-slate-400 mt-3">Cloud Sync</h3>
            <div className="flex items-center gap-3">
              <input
                type="url"
                placeholder="Cloud API URL"
                value={cloudUrl}
                onChange={(e) => { setCloudUrl(e.target.value); setCloudSaveMsg(null) }}
                className="flex-1 rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-xs text-slate-200 placeholder-slate-500 focus:border-emerald-500 focus:outline-none"
              />
              <input
                type="password"
                placeholder={cloudKeySet ? '••••••••' : 'API key'}
                value={cloudApiKey}
                onChange={(e) => { setCloudApiKey(e.target.value); setCloudSaveMsg(null) }}
                className="w-32 rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-xs text-slate-200 placeholder-slate-500 focus:border-emerald-500 focus:outline-none"
              />
              <button
                disabled={cloudSaving}
                onClick={async () => {
                  setCloudSaving(true); setCloudSaveMsg(null)
                  try {
                    await setShieldCloudSettings(cloudUrl, cloudApiKey)
                    setCloudSaveMsg('Saved')
                    if (cloudApiKey) setCloudKeySet(true)
                    setCloudApiKey('')
                  } catch (err) {
                    setCloudSaveMsg(err instanceof Error ? err.message : 'Failed')
                  } finally { setCloudSaving(false) }
                }}
                className="rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-500 disabled:opacity-40"
              >
                {cloudSaving ? 'Saving…' : 'Save'}
              </button>
              <button
                disabled={syncing || !cloudUrl}
                onClick={async () => {
                  setSyncing(true); setSyncResult(null)
                  try {
                    const r = await triggerSync()
                    setSyncResult(r.status === 'success' ? 'Synced' : r.error || r.status)
                    if (r.status === 'success' || r.status === 'reloaded') {
                      setCloudLastSynced(new Date().toISOString())
                    }
                  } catch (err) {
                    setSyncResult(err instanceof Error ? err.message : 'Failed')
                  } finally { setSyncing(false) }
                }}
                className="rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-500 disabled:opacity-40"
              >
                {syncing ? 'Syncing…' : 'Sync Now'}
              </button>
              {cloudSaveMsg && <span className={`text-xs ${cloudSaveMsg === 'Saved' ? 'text-emerald-400' : 'text-red-400'}`}>{cloudSaveMsg}</span>}
              {syncResult && <span className={`text-xs ${syncResult === 'Synced' ? 'text-emerald-400' : 'text-red-400'}`}>{syncResult}</span>}
            </div>
            {cloudLastSynced && (
              <p className={`text-[10px] ${
                (() => {
                  const hrs = (Date.now() - new Date(cloudLastSynced.endsWith('Z') ? cloudLastSynced : cloudLastSynced + 'Z').getTime()) / 3600000
                  return hrs > 48 ? 'text-amber-400' : 'text-slate-500'
                })()
              }`}>
                Last synced: {timeAgo(cloudLastSynced)}
              </p>
            )}
            <label className="flex items-center gap-2 mt-2 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={syncDismissals}
                onChange={async (e) => {
                  const val = e.target.checked
                  setSyncDismissals(val)
                  try {
                    await setShieldCloudSettings(cloudUrl, '', val)
                  } catch {
                    setSyncDismissals(!val) // revert on failure
                  }
                }}
                className="rounded border-slate-600 bg-slate-700 text-emerald-500 focus:ring-0 focus:ring-offset-0 h-3 w-3"
              />
              <span className="text-[11px] text-slate-400">Sync dismissed scam flags to cloud</span>
              <span className="text-[10px] text-slate-600" title="When enabled, dismissing a scam flag or clearing a scanned message will notify the cloud so the detection model can learn from false positives.">?</span>
            </label>
          </div>
        </div>
      )}

      {/* Feedback panel (collapsible) */}
      {feedbackOpen && (
        <div className="border-b border-slate-700 bg-slate-800/50 px-6 py-4">
          <div className="max-w-xl space-y-3">
            <h3 className="text-xs font-medium text-slate-400">Send Feedback</h3>
            <div className="flex items-center gap-3">
              <select
                value={feedbackType}
                onChange={(e) => setFeedbackType(e.target.value)}
                className="rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-xs text-slate-200 focus:border-emerald-500 focus:outline-none"
              >
                <option value="general">General</option>
                <option value="false_positive">False Positive</option>
                <option value="missed_scam">Missed Scam</option>
                <option value="feature_request">Feature Request</option>
                <option value="bug">Bug Report</option>
              </select>
            </div>
            <textarea
              value={feedbackMsg}
              onChange={(e) => { setFeedbackMsg(e.target.value); setFeedbackResult(null) }}
              placeholder="Describe your feedback — e.g. a scam that was missed, a false positive, or how we can improve detection…"
              rows={3}
              className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-xs text-slate-200 placeholder-slate-500 focus:border-emerald-500 focus:outline-none resize-none"
            />
            <div className="flex items-center gap-3">
              <button
                disabled={feedbackSending || !feedbackMsg.trim()}
                onClick={async () => {
                  setFeedbackSending(true); setFeedbackResult(null)
                  try {
                    await submitFeedback({ feedback_type: feedbackType, message: feedbackMsg })
                    setFeedbackResult('Sent — thank you!')
                    setFeedbackMsg('')
                    setFeedbackType('general')
                  } catch (err) {
                    setFeedbackResult(err instanceof Error ? err.message : 'Failed to send')
                  } finally { setFeedbackSending(false) }
                }}
                className="flex items-center gap-1.5 rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-500 disabled:opacity-40"
              >
                <Send size={12} />
                {feedbackSending ? 'Sending…' : 'Send Feedback'}
              </button>
              {feedbackResult && (
                <span className={`text-xs ${feedbackResult.startsWith('Sent') ? 'text-emerald-400' : 'text-red-400'}`}>{feedbackResult}</span>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Tabs */}
      <div className="flex items-center gap-1 border-b border-slate-700 bg-slate-900 px-6">
        <button
          onClick={() => setTab('flagged')}
          className={`px-3 py-2 text-xs font-medium border-b-2 transition-colors ${
            tab === 'flagged'
              ? 'border-red-400 text-red-400'
              : 'border-transparent text-slate-500 hover:text-slate-300'
          }`}
        >
          Flagged Scammers
          {flags.length > 0 && (
            <span className="ml-1.5 rounded-full bg-red-500/20 px-1.5 py-0.5 text-[10px]">{flags.length}</span>
          )}
        </button>
        <button
          onClick={() => setTab('scanned')}
          className={`px-3 py-2 text-xs font-medium border-b-2 transition-colors ${
            tab === 'scanned'
              ? 'border-indigo-400 text-indigo-400'
              : 'border-transparent text-slate-500 hover:text-slate-300'
          }`}
        >
          Scanned Messages
          {scannedTotal > 0 && (
            <span className="ml-1.5 rounded-full bg-slate-700 px-1.5 py-0.5 text-[10px]">{scannedTotal}</span>
          )}
        </button>
        <button
          onClick={() => setTab('reports')}
          className={`px-3 py-2 text-xs font-medium border-b-2 transition-colors ${
            tab === 'reports'
              ? 'border-amber-400 text-amber-400'
              : 'border-transparent text-slate-500 hover:text-slate-300'
          }`}
        >
          Reports
          {reports.length > 0 && (
            <span className="ml-1.5 rounded-full bg-slate-700 px-1.5 py-0.5 text-[10px]">{reports.length}</span>
          )}
        </button>
        <button
          onClick={() => setTab('sync')}
          className={`px-3 py-2 text-xs font-medium border-b-2 transition-colors ${
            tab === 'sync'
              ? 'border-emerald-400 text-emerald-400'
              : 'border-transparent text-slate-500 hover:text-slate-300'
          }`}
        >
          Sync History
        </button>
        {devMode && (
          <button
            onClick={() => setTab('pipeline')}
            className={`px-3 py-2 text-xs font-medium border-b-2 transition-colors ${
              tab === 'pipeline'
                ? 'border-cyan-400 text-cyan-400'
                : 'border-transparent text-slate-500 hover:text-slate-300'
            }`}
          >
            Pipeline
          </button>
        )}

        {/* Scam-only filter + mass scan for scanned tab */}
        {tab === 'scanned' && (
          <div className="ml-auto flex items-center gap-3">
            {devMode && (
              <>
                <button
                  disabled={massScanning}
                  onClick={async () => {
                    setMassScanning(true); setMassScanResult(null)
                    try {
                      const r = await triggerMassScan()
                      setMassScanResult(`${r.messages_queued} messages queued`)
                      setTimeout(() => loadScanned(scannedPage), 5000)
                    } catch { setMassScanResult('Failed') }
                    finally { setMassScanning(false) }
                  }}
                  className="rounded-lg bg-indigo-600 px-2.5 py-1 text-[10px] font-medium text-white hover:bg-indigo-500 disabled:opacity-40"
                >
                  {massScanning ? 'Scanning…' : 'Mass Scan'}
                </button>
                {massScanResult && <span className="text-[10px] text-slate-400">{massScanResult}</span>}
              </>
            )}
            <div className="flex items-center gap-1 rounded-lg bg-slate-800 p-0.5">
              {([
                { value: 'all' as const, label: 'All' },
                { value: 'scam' as const, label: `Scam (${scamCount})` },
                { value: 'failed' as const, label: `Failed (${failedCount})` },
              ]).map(({ value, label }) => (
                <button
                  key={value}
                  onClick={() => setScanFilter(value)}
                  className={`rounded-md px-2 py-0.5 text-[10px] font-medium transition-colors ${
                    scanFilter === value
                      ? 'bg-slate-700 text-slate-200'
                      : 'text-slate-500 hover:text-slate-400'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Table content */}
      <div className="flex-1 overflow-y-auto">
        {tab === 'flagged' && (
          loadingFlags ? (
            <div className="p-6 text-xs text-slate-500">Loading…</div>
          ) : flags.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-20">
              <ShieldCheck size={32} className="mb-3 text-slate-600" />
              <p className="text-sm font-medium text-slate-400">No flagged scammers</p>
              <p className="mt-1 text-xs text-slate-500">Messages are being monitored for suspicious activity.</p>
            </div>
          ) : (
            <table className="w-full text-left">
              <thead className="sticky top-0 z-10 bg-slate-900 border-b border-slate-700">
                <tr className="text-[10px] uppercase tracking-wider text-slate-500">
                  <th className="px-6 py-2 font-medium">User</th>
                  <th className="px-3 py-2 font-medium">Source</th>
                  <th className="px-3 py-2 font-medium">Flagged Message</th>
                  <th className="px-3 py-2 font-medium">Reason</th>
                  <th className="px-3 py-2 font-medium">Confidence</th>
                  <th className="px-2 py-2 font-medium w-8" title="Cloud sync status"><Cloud size={10} /></th>
                  <th className="px-3 py-2 font-medium w-8"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {flags.map((flag) => (
                  <tr key={flag.telegram_id} className="hover:bg-slate-800/50 transition-colors">
                    <td className="px-6 py-2.5">
                      <div className="flex items-center gap-2">
                        <ShieldAlert size={12} className="flex-shrink-0 text-red-400" />
                        <span className="text-xs font-medium text-slate-200 truncate max-w-[140px]">
                          {flag.sender_name || `ID: ${flag.telegram_id}`}
                        </span>
                      </div>
                    </td>
                    <td className="px-3 py-2.5">
                      <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium ${
                        flag.source === 'cloud' ? 'bg-purple-500/20 text-purple-300' :
                        flag.source === 'manual' ? 'bg-amber-500/20 text-amber-300' :
                        'bg-blue-500/20 text-blue-300'
                      }`}>
                        {flag.source === 'cloud' ? 'Cloud' : flag.source === 'manual' ? 'Manual' : 'Local'}
                      </span>
                    </td>
                    <td className="px-3 py-2.5">
                      {flag.flagged_message_text ? (
                        <button
                          onClick={() => flag.flagged_message_chat_id && flag.flagged_message_id && goToMessage(flag.flagged_message_chat_id, flag.flagged_message_id)}
                          className="text-xs text-slate-400 hover:text-slate-200 truncate max-w-[220px] block text-left transition-colors"
                          title={flag.flagged_message_text}
                        >
                          {flag.flagged_message_text}
                        </button>
                      ) : (
                        <span className="text-xs text-slate-500">—</span>
                      )}
                    </td>
                    <td className="px-3 py-2.5">
                      <span className="text-xs text-slate-400 truncate max-w-[200px] block" title={flag.reason || undefined}>
                        {flag.reason || '—'}
                      </span>
                    </td>
                    <td className="px-3 py-2.5">
                      <ConfidencePill value={flag.confidence} />
                    </td>
                    <td className="px-2 py-2.5 text-center">
                      <SyncBadge synced={flag.synced_to_cloud || false} />
                    </td>
                    <td className="px-3 py-2.5">
                      <button
                        onClick={() => handleDismissFlag(flag.telegram_id)}
                        title="Unflag user"
                        className="rounded p-1 text-slate-500 hover:bg-slate-700 hover:text-slate-300 transition-colors"
                      >
                        <X size={13} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        )}

        {tab === 'scanned' && (
          loadingScanned ? (
            <div className="p-6 text-xs text-slate-500">Loading…</div>
          ) : filteredScanned.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-20">
              <ShieldIcon size={32} className="mb-3 text-slate-600" />
              <p className="text-sm font-medium text-slate-400">
                {scanFilter === 'scam' ? 'No scam messages found' : scanFilter === 'failed' ? 'No failed scans' : 'No scanned messages yet'}
              </p>
              <p className="mt-1 text-xs text-slate-500">
                {scanFilter !== 'all' ? 'Try switching the filter to "All" to see all scanned messages.' : 'Use "Check for scam" on messages in Chats to start scanning.'}
              </p>
            </div>
          ) : (
            <table className="w-full text-left">
              <thead className="sticky top-0 z-10 bg-slate-900 border-b border-slate-700">
                <tr className="text-[10px] uppercase tracking-wider text-slate-500">
                  <th className="px-6 py-2 font-medium">Sent By</th>
                  <th className="px-3 py-2 font-medium">Chat</th>
                  <th className="px-3 py-2 font-medium">Verdict</th>
                  <th className="px-3 py-2 font-medium">Message</th>
                  <th className="px-3 py-2 font-medium">Confidence</th>
                  <th className="px-2 py-2 font-medium w-8" title="Cloud sync status"><Cloud size={10} /></th>
                  <th className="px-3 py-2 font-medium w-8"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {filteredScanned.map((msg) => {
                  const isScam = msg.scam_score > 0.5
                  const traceKey = `${msg.chat_id}-${msg.tg_message_id}`
                  const isTraceOpen = expandedTrace === traceKey
                  const hasTrace = msg.trace_json || msg.llm_prompt || msg.llm_response
                  const parsedTrace = msg.trace_json ? (() => { try { return JSON.parse(msg.trace_json) } catch { return null } })() : null
                  const isFailed = parsedTrace?.status === 'failed'
                  const failedStep = isFailed ? parsedTrace?.steps?.find((s: any) => s.status === 'failed') : null
                  const failedError = failedStep?.error || parsedTrace?.error || null
                  const isShortCircuited = parsedTrace?.status === 'short_circuited'
                  const shortCircuitNode = parsedTrace?.short_circuit?.node_id
                  return (
                    <>
                      <tr key={traceKey} className="hover:bg-slate-800/50 transition-colors">
                        <td className="px-6 py-2.5">
                          <span className="text-xs text-slate-200 truncate max-w-[140px] block">
                            {msg.sender_name || (msg.sender_id ? `ID: ${msg.sender_id}` : '—')}
                          </span>
                          {msg.date && (
                            <span className="text-[10px] text-slate-500">{timeAgo(msg.date)}</span>
                          )}
                        </td>
                        <td className="px-3 py-2.5">
                          <button
                            onClick={() => goToMessage(msg.chat_id, msg.tg_message_id)}
                            className="text-xs text-indigo-400 hover:text-indigo-300 truncate max-w-[160px] block text-left"
                            title={msg.chat_name}
                          >
                            {msg.chat_name}
                          </button>
                        </td>
                        <td className="px-3 py-2.5">
                          {isFailed ? (
                            <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium bg-orange-500/20 text-orange-300" title={failedError || undefined}>
                              Scan Failed
                            </span>
                          ) : isShortCircuited ? (
                            <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium bg-amber-500/20 text-amber-300">
                              ⚡ {shortCircuitNode || 'short-circuit'}
                            </span>
                          ) : (
                            <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium ${
                              isScam ? 'bg-red-500/20 text-red-300' : 'bg-emerald-500/20 text-emerald-300'
                            }`}>
                              {isScam ? (
                                <><ShieldAlert size={10} /> Scam</>
                              ) : (
                                <><ShieldCheck size={10} /> Safe</>
                              )}
                            </span>
                          )}
                        </td>
                        <td className="px-3 py-2.5">
                          <span className="text-xs text-slate-400 truncate max-w-[280px] block" title={msg.text ? stripFormatting(msg.text) : undefined}>
                            {msg.text ? (() => { const t = stripFormatting(msg.text); return t.length > 80 ? t.slice(0, 80) + '…' : t })() : '—'}
                          </span>
                        </td>
                        <td className="px-3 py-2.5">
                          {isFailed ? (
                            <span className="text-[10px] text-orange-400">—</span>
                          ) : isShortCircuited ? (
                            <span className="text-[10px] text-amber-400">⚡</span>
                          ) : (
                            <ConfidencePill value={msg.scam_score} />
                          )}
                        </td>
                        <td className="px-2 py-2.5 text-center">
                          <SyncBadge synced={msg.synced_to_cloud || false} />
                        </td>
                        <td className="px-3 py-2.5">
                          <div className="flex items-center gap-0.5">
                            <button
                              onClick={() => setExpandedTrace(isTraceOpen ? null : traceKey)}
                              title="View LLM trace"
                              className={`rounded p-1 transition-colors ${isTraceOpen ? 'text-indigo-400 bg-slate-700' : 'text-slate-500 hover:bg-slate-700 hover:text-slate-300'}`}
                            >
                              <ChevronDown size={13} className={`transition-transform ${isTraceOpen ? 'rotate-180' : ''}`} />
                            </button>
                            <button
                              onClick={() => handleRescan(msg.chat_id, msg.tg_message_id)}
                              title="Rescan message"
                              disabled={rescanning.has(`${msg.chat_id}-${msg.tg_message_id}`)}
                              className="rounded p-1 text-slate-500 hover:bg-slate-700 hover:text-indigo-400 transition-colors disabled:opacity-40"
                            >
                              <RefreshCw size={13} className={rescanning.has(`${msg.chat_id}-${msg.tg_message_id}`) ? 'animate-spin' : ''} />
                            </button>
                            <button
                              onClick={() => handleClearMessage(msg.chat_id, msg.tg_message_id)}
                              title="Clear scam flag"
                              className="rounded p-1 text-slate-500 hover:bg-slate-700 hover:text-slate-300 transition-colors"
                            >
                              <X size={13} />
                            </button>
                          </div>
                        </td>
                      </tr>
                      {isTraceOpen && (
                        <tr key={`${traceKey}-trace`} className="bg-slate-800/30">
                          <td colSpan={7} className="px-6 py-3">
                            {parsedTrace ? (
                              <div className="space-y-3 max-w-4xl">
                                {/* Pipeline header */}
                                <div className="flex items-center gap-3 text-[10px] text-slate-500">
                                  <span>Pipeline v{parsedTrace.pipeline_version}</span>
                                  <span>{parsedTrace.status}</span>
                                  {parsedTrace.duration_ms != null && <span>{parsedTrace.duration_ms}ms</span>}
                                </div>

                                {/* Full message text */}
                                {msg.text && (
                                  <div>
                                    <h4 className="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Message</h4>
                                    <pre className="whitespace-pre-wrap text-[11px] text-slate-300 bg-slate-900 rounded-lg p-3 border border-slate-700 max-h-32 overflow-y-auto">{msg.text}</pre>
                                  </div>
                                )}

                                {/* Reason */}
                                {msg.scam_reason && (
                                  <div>
                                    <h4 className="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Reason</h4>
                                    <p className="text-xs text-slate-400">{msg.scam_reason}</p>
                                  </div>
                                )}

                                {/* Short-circuit info */}
                                {parsedTrace.short_circuit && (
                                  <div className="rounded-lg bg-amber-500/10 border border-amber-500/30 p-2">
                                    <p className="text-xs text-amber-300">
                                      Short-circuited: {parsedTrace.short_circuit.match_type || parsedTrace.short_circuit.node_id} — {parsedTrace.short_circuit.matched_value || parsedTrace.short_circuit.condition}
                                    </p>
                                  </div>
                                )}

                                {/* Steps grouped by layer */}
                                {parsedTrace.steps?.map((step: any, i: number) => (
                                  <div key={i} className="rounded-lg border border-slate-700 bg-slate-900 p-3 space-y-2">
                                    <div className="flex items-center gap-2">
                                      <span className={`text-[10px] font-medium ${
                                        step.status === 'completed' ? 'text-emerald-400' :
                                        step.status === 'failed' ? 'text-red-400' :
                                        'text-slate-500'
                                      }`}>
                                        {step.status === 'completed' ? '\u2713' : step.status === 'failed' ? '\u2717' : '\u2014'}
                                      </span>
                                      <span className="text-xs font-medium text-slate-300">{step.node_id}</span>
                                      <span className="text-[10px] text-slate-500">{step.type}</span>
                                      {step.model_used && <span className="text-[10px] text-slate-500">{step.model_used}</span>}
                                      {step.duration_ms != null && <span className="text-[10px] text-slate-500 ml-auto">{step.duration_ms}ms</span>}
                                    </div>
                                    {step.model_warning && (
                                      <p className="text-[10px] text-amber-400">{step.model_warning}</p>
                                    )}
                                    {step.error && (
                                      <p className="text-[10px] text-red-400">{step.error}</p>
                                    )}
                                    {devMode && step.prompt_sent && (
                                      <details className="text-[10px]">
                                        <summary className="text-slate-500 cursor-pointer hover:text-slate-400">Show prompt</summary>
                                        <pre className="mt-1 whitespace-pre-wrap text-[11px] text-slate-400 bg-slate-950 rounded p-2 max-h-48 overflow-y-auto">{step.prompt_sent}</pre>
                                      </details>
                                    )}
                                    {devMode && step.input && (
                                      <details className="text-[10px]">
                                        <summary className="text-slate-500 cursor-pointer hover:text-slate-400">Show input</summary>
                                        <pre className="mt-1 whitespace-pre-wrap text-[11px] text-slate-400 bg-slate-950 rounded p-2 max-h-48 overflow-y-auto">{typeof step.input === 'string' ? step.input : JSON.stringify(step.input, null, 2)}</pre>
                                      </details>
                                    )}
                                    {step.tool_calls?.length > 0 && (
                                      <div className="space-y-1">
                                        {step.tool_calls.map((tc: any, tci: number) => (
                                          <details key={tci} className="text-[10px]">
                                            <summary className="text-indigo-400 cursor-pointer hover:text-indigo-300">
                                              Tool: {tc.tool}({typeof tc.args === 'object' ? JSON.stringify(tc.args) : tc.args}) <span className="text-slate-500">{tc.duration_ms}ms</span>
                                            </summary>
                                            <pre className="mt-1 whitespace-pre-wrap text-[10px] text-slate-400 bg-slate-950 rounded p-2 max-h-32 overflow-y-auto border border-indigo-500/20">{JSON.stringify(tc.result, null, 2)}</pre>
                                          </details>
                                        ))}
                                      </div>
                                    )}
                                    {devMode && step.raw_response && (
                                      <details className="text-[10px]">
                                        <summary className="text-slate-500 cursor-pointer hover:text-slate-400">Show response</summary>
                                        <pre className="mt-1 whitespace-pre-wrap text-[11px] text-slate-400 bg-slate-950 rounded p-2 max-h-32 overflow-y-auto">{step.raw_response}</pre>
                                      </details>
                                    )}
                                    {devMode && step.output && (
                                      <details className="text-[10px]">
                                        <summary className="text-slate-500 cursor-pointer hover:text-slate-400">Show output</summary>
                                        <pre className="mt-1 whitespace-pre-wrap text-[11px] text-emerald-400/70 bg-slate-950 rounded p-2 max-h-24 overflow-y-auto">{JSON.stringify(step.output, null, 2)}</pre>
                                      </details>
                                    )}
                                  </div>
                                ))}
                              </div>
                            ) : hasTrace ? (
                              <div className="space-y-3 max-w-4xl">
                                {msg.text && (
                                  <div>
                                    <h4 className="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Message</h4>
                                    <pre className="whitespace-pre-wrap text-[11px] text-slate-300 bg-slate-900 rounded-lg p-3 border border-slate-700 max-h-32 overflow-y-auto">{msg.text}</pre>
                                  </div>
                                )}
                                {msg.llm_prompt && (
                                  <div>
                                    <h4 className="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Prompt sent to LLM</h4>
                                    <pre className="whitespace-pre-wrap text-[11px] text-slate-400 bg-slate-900 rounded-lg p-3 border border-slate-700 max-h-48 overflow-y-auto">
                                      {msg.llm_prompt}
                                    </pre>
                                  </div>
                                )}
                                {msg.llm_response && (
                                  <div>
                                    <h4 className="text-[10px] uppercase tracking-wider text-slate-500 mb-1">LLM Response</h4>
                                    <pre className="whitespace-pre-wrap text-[11px] text-slate-400 bg-slate-900 rounded-lg p-3 border border-slate-700 max-h-32 overflow-y-auto">
                                      {msg.llm_response}
                                    </pre>
                                  </div>
                                )}
                              </div>
                            ) : (
                              <p className="text-xs text-slate-500">No trace recorded — this scan was run before trace logging was enabled.</p>
                            )}
                          </td>
                        </tr>
                      )}
                    </>
                  )
                })}
              </tbody>
            </table>
          )
        )}

        {/* Pagination for scanned messages */}
        {tab === 'scanned' && scannedTotal > PAGE_SIZE && (
          <div className="flex items-center justify-center gap-3 border-t border-slate-700 py-3">
            <button
              disabled={scannedPage <= 1}
              onClick={() => loadScanned(scannedPage - 1)}
              className="rounded px-3 py-1 text-xs text-slate-400 hover:bg-slate-800 disabled:opacity-30 disabled:hover:bg-transparent"
            >
              Previous
            </button>
            <span className="text-[10px] text-slate-500">
              Page {scannedPage} of {Math.ceil(scannedTotal / PAGE_SIZE)} ({scannedTotal} total)
            </span>
            <button
              disabled={scannedPage >= Math.ceil(scannedTotal / PAGE_SIZE)}
              onClick={() => loadScanned(scannedPage + 1)}
              className="rounded px-3 py-1 text-xs text-slate-400 hover:bg-slate-800 disabled:opacity-30 disabled:hover:bg-transparent"
            >
              Next
            </button>
          </div>
        )}

        {tab === 'reports' && (
          loadingReports ? (
            <div className="p-6 text-xs text-slate-500">Loading...</div>
          ) : reports.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-20">
              <FileText size={32} className="mb-3 text-slate-600" />
              <p className="text-sm font-medium text-slate-400">No reports submitted</p>
              <p className="mt-1 text-xs text-slate-500">Use "Report scam" on messages in Chats to submit reports.</p>
            </div>
          ) : (
            <table className="w-full text-left">
              <thead className="sticky top-0 z-10 bg-slate-900 border-b border-slate-700">
                <tr className="text-[10px] uppercase tracking-wider text-slate-500">
                  <th className="px-6 py-2 font-medium">Reported User</th>
                  <th className="px-3 py-2 font-medium">Chat</th>
                  <th className="px-3 py-2 font-medium">Category</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                  <th className="px-2 py-2 font-medium w-8" title="Cloud sync status"><Cloud size={10} /></th>
                  <th className="px-3 py-2 font-medium">Date</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {reports.map((r) => (
                  <tr key={r.report_id} className="hover:bg-slate-800/50 transition-colors">
                    <td className="px-6 py-2.5">
                      <div className="flex items-center gap-2">
                        <Flag size={12} className="flex-shrink-0 text-red-400" />
                        <span className="text-xs font-medium text-slate-200 truncate max-w-[140px]">
                          {r.reported_sender_name || `ID: ${r.reported_telegram_id}`}
                        </span>
                      </div>
                    </td>
                    <td className="px-3 py-2.5">
                      <span className="text-xs text-slate-400 truncate max-w-[160px] block">{r.chat_name}</span>
                    </td>
                    <td className="px-3 py-2.5">
                      <span className="text-xs text-slate-400">
                        {r.custom_category || r.scam_category || '—'}
                      </span>
                    </td>
                    <td className="px-3 py-2.5">
                      <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium ${
                        r.status === 'confirmed' ? 'bg-red-500/20 text-red-300' :
                        r.status === 'false_positive' ? 'bg-emerald-500/20 text-emerald-300' :
                        'bg-amber-500/20 text-amber-300'
                      }`}>
                        {r.status}
                      </span>
                    </td>
                    <td className="px-2 py-2.5 text-center">
                      <SyncBadge synced={!!r.synced_at} />
                    </td>
                    <td className="px-3 py-2.5 text-xs text-slate-400 whitespace-nowrap">
                      {r.created_at ? timeAgo(r.created_at) : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        )}

        {tab === 'sync' && (
          loadingSyncStats ? (
            <div className="p-6 text-xs text-slate-500">Loading…</div>
          ) : !syncStats ? (
            <div className="flex flex-col items-center justify-center py-20">
              <Cloud size={32} className="mb-3 text-slate-600" />
              <p className="text-sm font-medium text-slate-400">Sync data unavailable</p>
            </div>
          ) : (
            <div className="p-6 max-w-2xl space-y-6">
              {/* Last sync time + status */}
              <div className="space-y-2">
                <div className="flex items-center gap-3">
                  <History size={14} className={syncStats.last_sync_status === 'failed' ? 'text-red-400' : 'text-slate-500'} />
                  <span className="text-xs text-slate-400">
                    Last synced: {syncStats.last_synced_at ? timeAgo(syncStats.last_synced_at) : 'Never'}
                  </span>
                  {syncStats.last_sync_status === 'success' && (
                    <span className="inline-flex items-center rounded-full bg-emerald-500/20 px-2 py-0.5 text-[10px] font-medium text-emerald-300">Success</span>
                  )}
                  {syncStats.last_sync_status === 'failed' && (
                    <span className="inline-flex items-center rounded-full bg-red-500/20 px-2 py-0.5 text-[10px] font-medium text-red-300">Failed</span>
                  )}
                  <button
                    disabled={syncing || !cloudUrl}
                    onClick={async () => {
                      setSyncing(true); setSyncResult(null)
                      try {
                        const r = await triggerSync()
                        setSyncResult(r.status === 'success' ? 'Synced' : r.error || r.status)
                        if (r.status === 'success' || r.status === 'reloaded') {
                          setCloudLastSynced(new Date().toISOString())
                        }
                        getSyncHistory().then(setSyncStats).catch(() => {})
                      } catch (err) {
                        setSyncResult(err instanceof Error ? err.message : 'Failed')
                        getSyncHistory().then(setSyncStats).catch(() => {})
                      } finally { setSyncing(false) }
                    }}
                    className="rounded-lg bg-indigo-600 px-3 py-1 text-[10px] font-medium text-white hover:bg-indigo-500 disabled:opacity-40"
                  >
                    {syncing ? 'Syncing…' : 'Sync Now'}
                  </button>
                  {syncResult && <span className={`text-[10px] ${syncResult === 'Synced' ? 'text-emerald-400' : 'text-red-400'}`}>{syncResult}</span>}
                </div>

                {/* Failed sync error */}
                {syncStats.last_sync_status === 'failed' && syncStats.last_sync_error && (
                  <div className="rounded-lg bg-red-500/10 border border-red-500/30 px-3 py-2">
                    <p className="text-xs text-red-300">Sync failed: {syncStats.last_sync_error}</p>
                  </div>
                )}

                {/* Last successful sync details */}
                {syncStats.last_sync_status === 'success' && syncStats.last_sync_details && (
                  <div className="text-[10px] text-slate-500 flex items-center gap-3 flex-wrap">
                    {syncStats.last_sync_details.flags_submitted > 0 && <span>{syncStats.last_sync_details.flags_submitted} flags uploaded</span>}
                    {syncStats.last_sync_details.reports_submitted > 0 && <span>{syncStats.last_sync_details.reports_submitted} reports uploaded</span>}
                    {syncStats.last_sync_details.scan_results_submitted > 0 && <span>{syncStats.last_sync_details.scan_results_submitted} scans uploaded</span>}
                    {(syncStats.last_sync_details.dismissals_submitted ?? 0) > 0 && <span>{syncStats.last_sync_details.dismissals_submitted} dismissals uploaded</span>}
                    {syncStats.last_sync_details.files_updated.length > 0 && <span>{syncStats.last_sync_details.files_updated.length} files downloaded</span>}
                    {syncStats.last_sync_details.flags_submitted === 0 && syncStats.last_sync_details.reports_submitted === 0 && syncStats.last_sync_details.scan_results_submitted === 0 && syncStats.last_sync_details.files_updated.length === 0 && (
                      <span>Nothing new to sync</span>
                    )}
                  </div>
                )}
              </div>

              {/* Stats cards */}
              <div className="grid grid-cols-2 gap-4">
                {([
                  { label: 'Flagged Scammers', data: syncStats.flags, color: 'red' },
                  { label: 'Scam Reports', data: syncStats.reports, color: 'amber' },
                ] as const).map(({ label, data, color }) => {
                  const pct = data.total > 0 ? Math.round((data.synced / data.total) * 100) : 0
                  return (
                    <div key={label} className="rounded-lg border border-slate-700 bg-slate-800/50 p-4 space-y-3">
                      <h3 className="text-xs font-medium text-slate-300">{label}</h3>
                      <div className="flex items-end gap-2">
                        <span className="text-2xl font-bold text-slate-200">{data.synced}</span>
                        <span className="text-xs text-slate-500 mb-0.5">/ {data.total}</span>
                      </div>
                      <div className="space-y-1">
                        <div className="h-1.5 w-full rounded-full bg-slate-700 overflow-hidden">
                          <div
                            className={`h-full rounded-full transition-all ${
                              color === 'red' ? 'bg-red-500' : 'bg-amber-500'
                            }`}
                            style={{ width: `${pct}%` }}
                          />
                        </div>
                        <div className="flex justify-between text-[10px]">
                          <span className="text-emerald-400">{pct}% synced</span>
                          {data.pending > 0 && (
                            <span className="text-slate-500">{data.pending} pending</span>
                          )}
                        </div>
                      </div>
                    </div>
                  )
                })}

                {/* Scan Results card — scam + failed are synced */}
                {(() => {
                  const { total, synced, pending, total_scanned, scam_detected, failed } = syncStats.scans
                  const pct = total > 0 ? Math.round((synced / total) * 100) : 0
                  return (
                    <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-4 space-y-3">
                      <h3 className="text-xs font-medium text-slate-300">Scan Results</h3>
                      <div className="flex items-end gap-2">
                        <span className="text-2xl font-bold text-slate-200">{synced}</span>
                        <span className="text-xs text-slate-500 mb-0.5">/ {total} to sync</span>
                      </div>
                      <div className="flex flex-col gap-0.5 text-[10px]">
                        <span className="text-red-400">{scam_detected} scam detected</span>
                        {failed > 0 && <span className="text-orange-400">{failed} scan failed</span>}
                        {syncStats.dismissals.total > 0 && (
                          <span className="flex items-center gap-1 text-slate-400">
                            {syncStats.dismissals.synced}/{syncStats.dismissals.total} dismissed
                            {syncDismissals ? (
                              <span className="text-emerald-400" title="Dismissals are being synced">✓</span>
                            ) : (
                              <span className="text-slate-600" title="Enable 'Sync dismissed scam flags' to upload these">✗</span>
                            )}
                            {syncStats.dismissals.pending > 0 && syncDismissals && (
                              <span className="text-slate-500">({syncStats.dismissals.pending} pending)</span>
                            )}
                          </span>
                        )}
                        <span className="text-slate-600">{total_scanned} scanned total</span>
                      </div>
                      <div className="space-y-1">
                        <div className="h-1.5 w-full rounded-full bg-slate-700 overflow-hidden">
                          <div
                            className="h-full rounded-full bg-indigo-500 transition-all"
                            style={{ width: `${pct}%` }}
                          />
                        </div>
                        <div className="flex justify-between text-[10px]">
                          <span className="text-emerald-400">{pct}% synced</span>
                          {pending > 0 && (
                            <span className="text-slate-500">{pending} pending</span>
                          )}
                        </div>
                      </div>
                    </div>
                  )
                })()}

                {/* Analytics card */}
                {(() => {
                  const { total, synced, pending, latest } = syncStats.analytics
                  const pct = total > 0 ? Math.round((synced / total) * 100) : 0
                  return (
                    <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-4 space-y-3">
                      <h3 className="text-xs font-medium text-slate-300">Analytics</h3>
                      <div className="flex items-end gap-2">
                        <span className="text-2xl font-bold text-slate-200">{synced}</span>
                        <span className="text-xs text-slate-500 mb-0.5">/ {total} period{total !== 1 ? 's' : ''}</span>
                      </div>
                      {latest && (
                        <div className="flex flex-col gap-0.5 text-[10px]">
                          <span className="text-slate-500">Latest: {new Date(latest.period_start + 'Z').toLocaleDateString([], { month: 'short', day: 'numeric' })}</span>
                          <span className="text-slate-400">{latest.total_scanned} scanned · {latest.scam_detected} scam · {latest.pipeline_errors} errors</span>
                          <span className="text-slate-500">avg {latest.avg_pipeline_ms}ms/scan</span>
                        </div>
                      )}
                      <div className="space-y-1">
                        <div className="h-1.5 w-full rounded-full bg-slate-700 overflow-hidden">
                          <div
                            className="h-full rounded-full bg-cyan-500 transition-all"
                            style={{ width: `${pct}%` }}
                          />
                        </div>
                        <div className="flex justify-between text-[10px]">
                          <span className="text-emerald-400">{pct}% synced</span>
                          {pending > 0 && (
                            <span className="text-slate-500">{pending} pending</span>
                          )}
                        </div>
                      </div>
                    </div>
                  )
                })()}
              </div>

              {/* Dismissal sync setting */}
              <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-4 space-y-2">
                <label className="flex items-center gap-3 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={syncDismissals}
                    onChange={async (e) => {
                      const val = e.target.checked
                      setSyncDismissals(val)
                      try {
                        await setShieldCloudSettings(cloudUrl, '', val)
                      } catch {
                        setSyncDismissals(!val)
                      }
                    }}
                    className="rounded border-slate-600 bg-slate-700 text-emerald-500 focus:ring-0 focus:ring-offset-0 h-3.5 w-3.5"
                  />
                  <div>
                    <p className="text-xs font-medium text-slate-300">Sync dismissed scam flags</p>
                    <p className="text-[10px] text-slate-500">When you dismiss a scam flag or clear a scanned message, send that to the cloud so the detection model can learn from false positives.</p>
                  </div>
                </label>
              </div>

              {/* Summary insights */}
              <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-4 space-y-2">
                <h3 className="text-xs font-medium text-slate-300">Insights</h3>
                <ul className="space-y-1.5 text-xs text-slate-400">
                  {syncStats.flags.pending > 0 && (
                    <li className="flex items-center gap-2">
                      <span className="h-1.5 w-1.5 rounded-full bg-red-400 flex-shrink-0" />
                      {syncStats.flags.pending} flagged scammer{syncStats.flags.pending !== 1 ? 's' : ''} not yet synced to cloud
                    </li>
                  )}
                  {syncStats.reports.pending > 0 && (
                    <li className="flex items-center gap-2">
                      <span className="h-1.5 w-1.5 rounded-full bg-amber-400 flex-shrink-0" />
                      {syncStats.reports.pending} report{syncStats.reports.pending !== 1 ? 's' : ''} awaiting upload
                    </li>
                  )}
                  {syncStats.scans.pending > 0 && (
                    <li className="flex items-center gap-2">
                      <span className="h-1.5 w-1.5 rounded-full bg-indigo-400 flex-shrink-0" />
                      {syncStats.scans.pending} scan result{syncStats.scans.pending !== 1 ? 's' : ''} pending sync
                    </li>
                  )}
                  {syncStats.flags.pending === 0 && syncStats.reports.pending === 0 && syncStats.scans.pending === 0 && (
                    <li className="flex items-center gap-2">
                      <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 flex-shrink-0" />
                      All data synced to cloud — nothing pending
                    </li>
                  )}
                  {syncStats.last_sync_status === 'failed' && (
                    <li className="flex items-center gap-2">
                      <span className="h-1.5 w-1.5 rounded-full bg-red-400 flex-shrink-0" />
                      Last sync failed — pending items will be retried on next sync attempt
                    </li>
                  )}
                  {!syncStats.last_synced_at && (
                    <li className="flex items-center gap-2">
                      <span className="h-1.5 w-1.5 rounded-full bg-slate-500 flex-shrink-0" />
                      Cloud sync has never run. Configure cloud settings and sync to start sharing data.
                    </li>
                  )}
                  {syncStats.last_synced_at && (() => {
                    const hrs = (Date.now() - new Date(syncStats.last_synced_at.endsWith('Z') ? syncStats.last_synced_at : syncStats.last_synced_at + 'Z').getTime()) / 3600000
                    return hrs > 48 ? (
                      <li className="flex items-center gap-2">
                        <span className="h-1.5 w-1.5 rounded-full bg-amber-400 flex-shrink-0" />
                        Last sync was {Math.floor(hrs / 24)} days ago — consider syncing to keep cloud data up to date
                      </li>
                    ) : null
                  })()}
                </ul>
              </div>
            </div>
          )
        )}

        {tab === 'pipeline' && (
          loadingPipeline ? (
            <div className="p-6 text-xs text-slate-500">Loading...</div>
          ) : !pipelineData ? (
            <div className="flex flex-col items-center justify-center py-20">
              <GitBranch size={32} className="mb-3 text-slate-600" />
              <p className="text-sm font-medium text-slate-400">Pipeline not loaded</p>
            </div>
          ) : (
            <div className="p-4 space-y-4">
              {/* Pipeline header + DAG flow on one line */}
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-xs font-medium text-slate-300">Pipeline</span>
                <span className="rounded-full bg-cyan-500/20 px-1.5 py-0.5 text-[9px] font-medium text-cyan-300">v{pipelineData.version}</span>

              </div>

              {/* DAG visualization with SVG edges */}
              {(() => {
                const nodeMap = new Map(pipelineData.nodes.map(n => [n.id, n]))
                const predecessors: Record<string, string[]> = {}
                pipelineData.nodes.forEach(n => { predecessors[n.id] = [] })
                pipelineData.edges.forEach(e => {
                  if (e.from !== '$input' && predecessors[e.to]) predecessors[e.to].push(e.from)
                })
                const nodeLayer: Record<string, number> = {}
                pipelineData.nodes.forEach(n => { if (predecessors[n.id].length === 0) nodeLayer[n.id] = 0 })
                let changed = true
                while (changed) {
                  changed = false
                  pipelineData.nodes.forEach(n => {
                    if (nodeLayer[n.id] !== undefined) return
                    const pl = predecessors[n.id].map(p => nodeLayer[p]).filter(l => l !== undefined)
                    if (pl.length === predecessors[n.id].length) { nodeLayer[n.id] = Math.max(...pl) + 1; changed = true }
                  })
                }
                const maxLayer = Math.max(...Object.values(nodeLayer), 0)
                const layers: string[][] = Array.from({length: maxLayer + 1}, () => [])
                Object.entries(nodeLayer).forEach(([nid, l]) => layers[l].push(nid))

                // Layout constants
                const nodeW = 120, nodeH = 40, layerGap = 80, nodeGap = 16, padX = 20, padY = 20
                const maxNodesInLayer = Math.max(...layers.map(l => l.length), 1)

                // Compute positions: allIds includes $input and $result
                const allIds = ['$input', ...pipelineData.nodes.map(n => n.id), '$result']
                const pos: Record<string, {x: number, y: number}> = {}
                const totalLayers = maxLayer + 3 // $input + node layers + $result
                const hasShortCircuit = pipelineData.nodes.some(n => n.short_circuit)
                const totalHeight = maxNodesInLayer * (nodeH + nodeGap) - nodeGap + padY * 2 + (hasShortCircuit ? 50 : 0)
                const totalWidth = totalLayers * (nodeW + layerGap) - layerGap + padX * 2

                // $input position
                const inputY = totalHeight / 2
                pos['$input'] = { x: padX + nodeW / 2, y: inputY }

                // Node positions
                layers.forEach((layerNodes, li) => {
                  const x = padX + (li + 1) * (nodeW + layerGap) + nodeW / 2
                  const layerH = layerNodes.length * (nodeH + nodeGap) - nodeGap
                  const startY = (totalHeight - layerH) / 2 + nodeH / 2
                  layerNodes.forEach((nid, ni) => {
                    pos[nid] = { x, y: startY + ni * (nodeH + nodeGap) }
                  })
                })

                // $result position
                pos['$result'] = { x: padX + (maxLayer + 2) * (nodeW + layerGap) + nodeW / 2, y: inputY }

                // Build edge list including $input→nodes and output→$result
                const svgEdges: {from: string, to: string}[] = []
                pipelineData.edges.forEach(e => {
                  svgEdges.push({ from: e.from === '$input' ? '$input' : e.from, to: e.to })
                })
                svgEdges.push({ from: pipelineData.output, to: '$result' })

                return (
                  <div className="overflow-x-auto">
                    <svg width={totalWidth} height={totalHeight} className="block">
                      <defs>
                        <marker id="arrow" viewBox="0 0 10 6" refX="10" refY="3" markerWidth="8" markerHeight="6" orient="auto">
                          <path d="M0,0 L10,3 L0,6" fill="#475569" />
                        </marker>
                        <marker id="arrow-sc" viewBox="0 0 10 6" refX="10" refY="3" markerWidth="8" markerHeight="6" orient="auto">
                          <path d="M0,0 L10,3 L0,6" fill="#fbbf24" />
                        </marker>
                      </defs>

                      {/* Edges */}
                      {svgEdges.map((e, i) => {
                        const from = pos[e.from]
                        const to = pos[e.to]
                        if (!from || !to) return null
                        const x1 = from.x + nodeW / 2
                        const x2 = to.x - nodeW / 2
                        const midX = (x1 + x2) / 2
                        return (
                          <path
                            key={i}
                            d={`M${x1},${from.y} C${midX},${from.y} ${midX},${to.y} ${x2},${to.y}`}
                            fill="none"
                            stroke="#475569"
                            strokeWidth={1.5}
                            markerEnd="url(#arrow)"
                          />
                        )
                      })}

                      {/* $input node */}
                      <g transform={`translate(${pos['$input'].x - nodeW/2}, ${pos['$input'].y - nodeH/2})`}>
                        <rect width={nodeW} height={nodeH} rx={nodeH/2} fill="#1e293b" stroke="#475569" strokeWidth={1} />
                        <text x={nodeW/2} y={nodeH/2 - 2} textAnchor="middle" dominantBaseline="middle" fill="#94a3b8" fontSize={10} fontWeight={500}>$input</text>
                        <text x={nodeW/2} y={nodeH/2 + 10} textAnchor="middle" dominantBaseline="middle" fill="#64748b" fontSize={8}>Message</text>
                      </g>

                      {/* Short-circuit bypass arrows (dashed, routed below the graph) */}
                      {pipelineData.nodes.map(node => {
                        if (!node.short_circuit) return null
                        const from = pos[node.id]
                        const to = pos['$result']
                        if (!from || !to) return null
                        const x1 = from.x
                        const y1 = from.y + nodeH / 2
                        const x2 = to.x
                        const y2 = to.y + nodeH / 2
                        const bypassY = totalHeight - 12
                        return (
                          <g key={`sc-${node.id}`}>
                            <path
                              d={`M${x1},${y1} L${x1},${bypassY - 8} Q${x1},${bypassY} ${x1 + 8},${bypassY} L${x2 - 8},${bypassY} Q${x2},${bypassY} ${x2},${bypassY - 8} L${x2},${y2}`}
                              fill="none" stroke="#fbbf24" strokeWidth={1.2} strokeDasharray="4,3"
                              markerEnd="url(#arrow-sc)"
                            />
                            <text x={(x1 + x2) / 2} y={bypassY - 4} textAnchor="middle" fill="#fbbf24" fontSize={7}>short-circuit</text>
                          </g>
                        )
                      })}

                      {/* Pipeline nodes */}
                      {pipelineData.nodes.map(node => {
                        const p = pos[node.id]
                        if (!p) return null
                        const isSelected = selectedNode === node.id
                        const isOutput = node.id === pipelineData.output
                        const hasShortCircuit = !!node.short_circuit
                        const strokeColor = isSelected ? '#22d3ee' : isOutput ? 'rgba(34,211,238,0.5)' : hasShortCircuit ? 'rgba(251,191,36,0.5)' : '#475569'
                        const fillColor = isSelected ? 'rgba(34,211,238,0.1)' : isOutput ? 'rgba(34,211,238,0.07)' : '#1e293b'
                        const typeColor = node.type === 'llm' ? '#818cf8' : node.type === 'script' ? '#fbbf24' : '#34d399'
                        return (
                          <g
                            key={node.id}
                            transform={`translate(${p.x - nodeW/2}, ${p.y - nodeH/2})`}
                            onClick={() => setSelectedNode(isSelected ? null : node.id)}
                            className="cursor-pointer"
                          >
                            <rect width={nodeW} height={nodeH} rx={8} fill={fillColor} stroke={strokeColor} strokeWidth={isSelected ? 2 : 1.5} />
                            <text x={nodeW/2} y={hasShortCircuit ? nodeH/2 - 8 : nodeH/2 - 4} textAnchor="middle" dominantBaseline="middle" fill="#e2e8f0" fontSize={11} fontWeight={500}>{node.id}</text>
                            <text x={nodeW/2} y={hasShortCircuit ? nodeH/2 + 4 : nodeH/2 + 10} textAnchor="middle" dominantBaseline="middle" fill={typeColor} fontSize={9}>{node.type}</text>
                            {hasShortCircuit && (
                              <text x={nodeW/2} y={nodeH/2 + 16} textAnchor="middle" dominantBaseline="middle" fill="#fbbf24" fontSize={7}>⚡ short-circuit</text>
                            )}
                          </g>
                        )
                      })}

                      {/* $result node */}
                      <g transform={`translate(${pos['$result'].x - nodeW/2}, ${pos['$result'].y - nodeH/2})`}>
                        <rect width={nodeW} height={nodeH} rx={nodeH/2} fill="rgba(16,185,129,0.07)" stroke="rgba(16,185,129,0.5)" strokeWidth={1} />
                        <text x={nodeW/2} y={nodeH/2 + 1} textAnchor="middle" dominantBaseline="middle" fill="#34d399" fontSize={10} fontWeight={500}>result</text>
                      </g>
                    </svg>
                  </div>
                )
              })()}

              {/* Selected node detail panel */}
              {selectedNode && (() => {
                const node = pipelineData.nodes.find(n => n.id === selectedNode)
                if (!node) return null
                return (
                  <div className="rounded-lg border border-slate-700 bg-slate-800 p-4 space-y-3">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-slate-200">{node.id}</span>
                      <span className={`rounded-full px-1.5 py-0.5 text-[9px] font-medium ${
                        node.type === 'llm' ? 'bg-indigo-500/20 text-indigo-300' :
                        node.type === 'script' ? 'bg-amber-500/20 text-amber-300' :
                        'bg-emerald-500/20 text-emerald-300'
                      }`}>{node.type}</span>
                      {node.id === pipelineData.output && <span className="text-[10px] text-cyan-400">output node</span>}
                      <button onClick={() => setSelectedNode(null)} className="ml-auto rounded p-1 text-slate-500 hover:bg-slate-700 hover:text-slate-300">
                        <X size={13} />
                      </button>
                    </div>

                    {/* Config fields */}
                    <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[11px]">
                      {node.config.model && <>
                        <span className="text-slate-500">Model</span>
                        <span className="text-slate-300">{node.config.model}</span>
                      </>}
                      {node.config.max_tokens && <>
                        <span className="text-slate-500">Max tokens</span>
                        <span className="text-slate-300">{node.config.max_tokens}</span>
                      </>}
                      {node.config.temperature != null && <>
                        <span className="text-slate-500">Temperature</span>
                        <span className="text-slate-300">{node.config.temperature}</span>
                      </>}
                      {node.config.script && <>
                        <span className="text-slate-500">Script</span>
                        <span className="text-slate-300 font-mono">{node.config.script}</span>
                      </>}
                      {node.config.few_shot_examples?.length > 0 && <>
                        <span className="text-slate-500">Few-shot examples</span>
                        <span className="text-slate-300">{node.config.few_shot_examples.length}</span>
                      </>}
                    </div>

                    {/* Params — from node config.params OR tool definition's script_params */}
                    {(() => {
                      const nodeParams = node.config.params && Object.keys(node.config.params).length > 0 ? node.config.params : null
                      const toolDef = pipelineData.tools?.find(t => t.name === node.config.script)
                      const params = nodeParams || toolDef?.script_params
                      if (!params || Object.keys(params).length === 0) return null
                      return (
                        <div>
                          <h4 className="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Parameters</h4>
                          <div className="rounded-lg bg-slate-900 border border-slate-700 p-3 space-y-1.5">
                            {Object.entries(params).map(([key, val]) => (
                              <div key={key} className="flex items-start gap-2">
                                <span className="text-[11px] text-slate-500 font-mono whitespace-nowrap">{key}:</span>
                                <span className="text-[11px] text-slate-300">
                                  {Array.isArray(val) ? (
                                    <span className="flex flex-wrap gap-1">
                                      {(val as string[]).map((v, i) => (
                                        <span key={i} className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] font-mono">{String(v)}</span>
                                      ))}
                                    </span>
                                  ) : typeof val === 'number' ? (
                                    <span className="font-mono">{val}</span>
                                  ) : (
                                    String(val)
                                  )}
                                </span>
                              </div>
                            ))}
                          </div>
                        </div>
                      )
                    })()}

                    {/* Full prompt */}
                    {node.config.prompt && (
                      <div>
                        <h4 className="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Prompt</h4>
                        <pre className="whitespace-pre-wrap text-[11px] text-slate-400 bg-slate-900 rounded-lg p-3 border border-slate-700 max-h-64 overflow-y-auto">{node.config.prompt}</pre>
                      </div>
                    )}

                    {/* Few-shot examples */}
                    {node.config.few_shot_examples?.length > 0 && (
                      <div>
                        <h4 className="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Few-shot Examples</h4>
                        <div className="space-y-2">
                          {node.config.few_shot_examples.map((ex: any, i: number) => (
                            <div key={i} className="rounded-lg bg-slate-900 border border-slate-700 p-2 space-y-1">
                              <div>
                                <span className="text-[9px] uppercase tracking-wider text-slate-500">Input</span>
                                <pre className="whitespace-pre-wrap text-[10px] text-slate-400 mt-0.5">{ex.input}</pre>
                              </div>
                              <div>
                                <span className="text-[9px] uppercase tracking-wider text-slate-500">Expected output</span>
                                <pre className="whitespace-pre-wrap text-[10px] text-emerald-400/70 mt-0.5">{ex.output}</pre>
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Short-circuit */}
                    {node.short_circuit && (
                      <div>
                        <h4 className="text-[10px] uppercase tracking-wider text-amber-400 mb-1">Short-circuit</h4>
                        <div className="rounded-lg bg-slate-900 border border-amber-500/30 p-2 space-y-1">
                          <div>
                            <span className="text-[9px] text-slate-500">Condition</span>
                            <p className="text-[11px] text-amber-300 font-mono">{node.short_circuit.condition}</p>
                          </div>
                          <div>
                            <span className="text-[9px] text-slate-500">Result</span>
                            <pre className="text-[10px] text-slate-400 mt-0.5">{typeof node.short_circuit.result === 'string' ? node.short_circuit.result : JSON.stringify(node.short_circuit.result, null, 2)}</pre>
                          </div>
                        </div>
                      </div>
                    )}

                  </div>
                )
              })()}
            </div>
          )
        )}
      </div>
    </div>
  )
}
