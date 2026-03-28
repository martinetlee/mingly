import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { Shield as ShieldIcon, ShieldAlert, ShieldCheck, X, ExternalLink, Settings, ChevronDown } from 'lucide-react'
import type { ScamFlag, ScannedMessage, WSEvent } from '../api/types'
import { getScamFlags, dismissScamFlag, getScannedMessages, clearMessageScam, getScamDetectionSettings, setScamDetectionSettings } from '../api/client'
import { useWebSocket } from '../hooks/useWebSocket'

function timeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
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

type Tab = 'flagged' | 'scanned'

export default function Shield() {
  const navigate = useNavigate()
  const [tab, setTab] = useState<Tab>('flagged')

  // Flagged scammers
  const [flags, setFlags] = useState<ScamFlag[]>([])
  const [loadingFlags, setLoadingFlags] = useState(true)

  // Scanned messages
  const [scanned, setScanned] = useState<ScannedMessage[]>([])
  const [loadingScanned, setLoadingScanned] = useState(true)
  const [scamOnly, setScamOnly] = useState(false)
  const [expandedTrace, setExpandedTrace] = useState<string | null>(null) // "chatId-msgId"

  // Detection settings
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [scamMode, setScamMode] = useState<'local_llm' | 'external'>('local_llm')
  const [scamApiUrl, setScamApiUrl] = useState('')
  const [scamApiKeySet, setScamApiKeySet] = useState(false)
  const [scamApiKey, setScamApiKey] = useState('')
  const [scamSaving, setScamSaving] = useState(false)
  const [scamSaveMsg, setScamSaveMsg] = useState<string | null>(null)

  const loadFlags = useCallback(() => {
    getScamFlags()
      .then((data) => setFlags(data.flags))
      .catch(() => {})
      .finally(() => setLoadingFlags(false))
  }, [])

  const loadScanned = useCallback(() => {
    getScannedMessages()
      .then((data) => setScanned(data.messages))
      .catch(() => {})
      .finally(() => setLoadingScanned(false))
  }, [])

  useEffect(() => {
    loadFlags()
    loadScanned()
    getScamDetectionSettings()
      .then((s) => {
        setScamMode(s.mode as 'local_llm' | 'external')
        setScamApiUrl(s.api_url)
        setScamApiKeySet(s.api_key_set)
      })
      .catch(() => {})
  }, [loadFlags, loadScanned])

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

  const goToMessage = (chatId: number, messageId: number) => {
    navigate(`/chats?chatId=${chatId}&msgId=${messageId}`)
  }

  const filteredScanned = scamOnly ? scanned.filter((m) => m.scam_score > 0.5) : scanned
  const scamCount = scanned.filter((m) => m.scam_score > 0.5).length

  return (
    <div className="flex h-full flex-1 flex-col overflow-hidden">
      {/* Header */}
      <header className="flex items-center gap-3 border-b border-slate-700 bg-slate-900 px-6 py-3">
        <ShieldAlert size={18} className="text-red-400" />
        <h1 className="text-sm font-semibold text-slate-200">Shield</h1>
        <div className="ml-auto flex items-center gap-2">
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
                    await setScamDetectionSettings(scamMode, scamApiUrl, scamApiKey)
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
          {scanned.length > 0 && (
            <span className="ml-1.5 rounded-full bg-slate-700 px-1.5 py-0.5 text-[10px]">{scanned.length}</span>
          )}
        </button>

        {/* Scam-only filter for scanned tab */}
        {tab === 'scanned' && (
          <label className="ml-auto flex items-center gap-1.5 text-[10px] text-slate-400 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={scamOnly}
              onChange={(e) => setScamOnly(e.target.checked)}
              className="rounded border-slate-600 bg-slate-700 text-red-500 focus:ring-0 focus:ring-offset-0 h-3 w-3"
            />
            Scam only ({scamCount})
          </label>
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
                  <th className="px-3 py-2 font-medium">Last Active Chat</th>
                  <th className="px-3 py-2 font-medium">Last Active</th>
                  <th className="px-3 py-2 font-medium">Last Message</th>
                  <th className="px-3 py-2 font-medium">Reason</th>
                  <th className="px-3 py-2 font-medium">Confidence</th>
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
                      {flag.last_chat_name ? (
                        <button
                          onClick={() => goToMessage(flag.last_chat_id!, flag.last_message_id!)}
                          className="text-xs text-indigo-400 hover:text-indigo-300 truncate max-w-[160px] block text-left"
                          title={flag.last_chat_name}
                        >
                          {flag.last_chat_name}
                        </button>
                      ) : (
                        <span className="text-xs text-slate-500">—</span>
                      )}
                    </td>
                    <td className="px-3 py-2.5 text-xs text-slate-400 whitespace-nowrap">
                      {flag.last_active ? timeAgo(flag.last_active) : '—'}
                    </td>
                    <td className="px-3 py-2.5">
                      <span className="text-xs text-slate-400 truncate max-w-[200px] block" title={flag.last_message || undefined}>
                        {flag.last_message || '—'}
                      </span>
                    </td>
                    <td className="px-3 py-2.5">
                      <span className="text-xs text-slate-400 truncate max-w-[200px] block" title={flag.reason || undefined}>
                        {flag.reason || '—'}
                      </span>
                    </td>
                    <td className="px-3 py-2.5">
                      <ConfidencePill value={flag.confidence} />
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
                {scamOnly ? 'No scam messages found' : 'No scanned messages yet'}
              </p>
              <p className="mt-1 text-xs text-slate-500">
                {scamOnly ? 'Try disabling the filter to see all scanned messages.' : 'Use "Check for scam" on messages in Chats to start scanning.'}
              </p>
            </div>
          ) : (
            <table className="w-full text-left">
              <thead className="sticky top-0 z-10 bg-slate-900 border-b border-slate-700">
                <tr className="text-[10px] uppercase tracking-wider text-slate-500">
                  <th className="px-6 py-2 font-medium">Sent By</th>
                  <th className="px-3 py-2 font-medium">Chat</th>
                  <th className="px-3 py-2 font-medium">Verdict</th>
                  <th className="px-3 py-2 font-medium">Reason</th>
                  <th className="px-3 py-2 font-medium">Confidence</th>
                  <th className="px-3 py-2 font-medium w-8"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {filteredScanned.map((msg) => {
                  const isScam = msg.scam_score > 0.5
                  const traceKey = `${msg.chat_id}-${msg.tg_message_id}`
                  const isTraceOpen = expandedTrace === traceKey
                  const hasTrace = msg.llm_prompt || msg.llm_response
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
                          <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium ${
                            isScam ? 'bg-red-500/20 text-red-300' : 'bg-emerald-500/20 text-emerald-300'
                          }`}>
                            {isScam ? (
                              <><ShieldAlert size={10} /> Scam</>
                            ) : (
                              <><ShieldCheck size={10} /> Safe</>
                            )}
                          </span>
                        </td>
                        <td className="px-3 py-2.5">
                          <span className="text-xs text-slate-400 truncate max-w-[220px] block" title={msg.scam_reason || undefined}>
                            {msg.scam_reason || '—'}
                          </span>
                        </td>
                        <td className="px-3 py-2.5">
                          <ConfidencePill value={msg.scam_score} />
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
                          <td colSpan={6} className="px-6 py-3">
                            {hasTrace ? (
                              <div className="space-y-3 max-w-4xl">
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
      </div>
    </div>
  )
}
