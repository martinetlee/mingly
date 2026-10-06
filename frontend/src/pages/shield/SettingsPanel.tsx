import { useEffect, useState } from 'react'
import { getScamDetectionSettings, setScamDetectionSettings, setShieldCloudSettings } from '../../api/client'
import { useLatestQuery } from '../../hooks/useLatestQuery'
import { useAsyncAction } from '../../hooks/useAsyncAction'
import { errorMessage } from '../../lib/toast'
import { ageDays, fmtRelative } from '../../utils/time'
import type { CloudSync } from './useCloudSync'
import type { CloudSettings } from './useCloudSettings'
import { SyncDismissalsToggle, SyncNowButton } from './SyncControls'

const input = 'rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-xs text-slate-200 placeholder-slate-500 focus:border-emerald-500 focus:outline-none'
const saveBtn = 'rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-500 disabled:opacity-40'

type SaveMsg = { ok: boolean; text: string } | null

function SaveStatus({ msg }: { msg: SaveMsg }) {
  if (!msg) return null
  return <span className={`text-xs ${msg.ok ? 'text-emerald-400' : 'text-red-400'}`}>{msg.text}</span>
}

function DetectionSettings() {
  const { data, refetch } = useLatestQuery('scam-detection-settings', () => getScamDetectionSettings())
  const [mode, setMode] = useState<'local_llm' | 'external'>('local_llm')
  const [apiUrl, setApiUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [maxTokens, setMaxTokens] = useState(200)
  const [msg, setMsg] = useState<SaveMsg>(null)

  useEffect(() => {
    if (!data) return
    setMode(data.mode === 'external' ? 'external' : 'local_llm')
    setApiUrl(data.api_url)
    setMaxTokens(data.max_tokens || 200)
  }, [data])

  const save = useAsyncAction(async () => {
    setMsg(null)
    try {
      await setScamDetectionSettings(mode, apiUrl, apiKey, maxTokens)
      setApiKey('')
      setMsg({ ok: true, text: 'Saved' })
      void refetch()
    } catch (e) {
      setMsg({ ok: false, text: errorMessage(e) })
    }
  })

  const dirty = () => setMsg(null)
  return (
    <>
      <h3 className="text-xs font-medium text-slate-400">Scam Detection Settings</h3>
      <div className="flex items-center gap-3">
        <select value={mode} onChange={(e) => { setMode(e.target.value as 'local_llm' | 'external'); dirty() }} className={input}>
          <option value="local_llm">Local LLM</option>
          <option value="external">External API</option>
        </select>
        {mode === 'local_llm' && (
          <div className="flex items-center gap-1.5">
            <label className="text-[10px] text-slate-500 whitespace-nowrap">Max tokens</label>
            <input type="number" min={50} max={2000} step={50} value={maxTokens}
              onChange={(e) => { setMaxTokens(Number(e.target.value)); dirty() }}
              className={`w-20 ${input} px-2`} />
          </div>
        )}
        {mode === 'external' && (
          <>
            <input type="url" placeholder="API endpoint URL" value={apiUrl}
              onChange={(e) => { setApiUrl(e.target.value); dirty() }} className={`flex-1 ${input}`} />
            <input type="password" placeholder={data?.api_key_set ? '••••••••' : 'API key'} value={apiKey}
              onChange={(e) => { setApiKey(e.target.value); dirty() }} className={`w-36 ${input}`} />
          </>
        )}
        <button disabled={save.pending || !data || (mode === 'external' && !apiUrl.trim())} onClick={() => { void save.run() }} className={saveBtn}>
          {save.pending ? 'Saving…' : 'Save'}
        </button>
        <SaveStatus msg={msg} />
      </div>
    </>
  )
}

function CloudSettingsForm({ settings, cloudSync }: { settings: CloudSettings; cloudSync: CloudSync }) {
  const [url, setUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [msg, setMsg] = useState<SaveMsg>(null)
  const { loaded, cloudUrl, refetch } = settings

  useEffect(() => { if (loaded) setUrl(cloudUrl) }, [loaded, cloudUrl])

  const save = useAsyncAction(async () => {
    setMsg(null)
    try {
      await setShieldCloudSettings(url, apiKey)
      setApiKey('')
      setMsg({ ok: true, text: 'Saved' })
      void refetch()
    } catch (e) {
      setMsg({ ok: false, text: errorMessage(e) })
    }
  })

  const last = cloudSync.lastSyncedAt
  const stale = (ageDays(last) ?? 0) >= 2
  return (
    <>
      <h3 className="text-xs font-medium text-slate-400 mt-3">Cloud Sync</h3>
      <div className="flex items-center gap-3 flex-wrap">
        <input type="url" placeholder="Cloud API URL" value={url}
          onChange={(e) => { setUrl(e.target.value); setMsg(null) }} className={`flex-1 ${input}`} />
        <input type="password" placeholder={settings.keySet ? '••••••••' : 'API key'} value={apiKey}
          onChange={(e) => { setApiKey(e.target.value); setMsg(null) }} className={`w-32 ${input}`} />
        <button disabled={save.pending || !loaded} onClick={() => { void save.run() }} className={saveBtn}>
          {save.pending ? 'Saving…' : 'Save'}
        </button>
        <SaveStatus msg={msg} />
        <SyncNowButton cloudSync={cloudSync} configured={!!settings.cloudUrl && settings.keySet} />
      </div>
      {last && (
        <p className={`text-[10px] ${stale ? 'text-amber-400' : 'text-slate-500'}`}>Last synced: {fmtRelative(last)}</p>
      )}
      <SyncDismissalsToggle settings={settings} compact />
    </>
  )
}

export default function SettingsPanel({ settings, cloudSync }: { settings: CloudSettings; cloudSync: CloudSync }) {
  return (
    <div className="border-b border-slate-700 bg-slate-800/50 px-6 py-4">
      <div className="max-w-xl space-y-3">
        <DetectionSettings />
        <CloudSettingsForm settings={settings} cloudSync={cloudSync} />
      </div>
    </div>
  )
}
