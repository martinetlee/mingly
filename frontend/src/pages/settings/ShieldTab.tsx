import { useCallback, useEffect, useRef, useState } from 'react'
import {
  getScamDetectionSettings, getShieldCloudSettings, setScamDetectionSettings, setShieldCloudSettings, triggerSync,
} from '../../api/client'
import { useAsyncAction } from '../../hooks/useAsyncAction'
import { useSettingField } from '../../hooks/useSettingField'
import { reportError, toast } from '../../lib/toast'
import { fmtDateTime } from '../../utils/time'
import { FieldStatus, Section } from './shared'

const fieldClass =
  'w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-sm text-slate-200 placeholder-slate-500 focus:border-emerald-500 focus:outline-none'

export function validateShieldMaxTokens(v: string): string | null {
  if (!/^\d+$/.test(v.trim())) return 'Enter a whole number'
  const n = Number(v)
  return n < 50 || n > 2000 ? 'Between 50 and 2000' : null
}

export default function ShieldTab() {
  // Cloud sync
  const [cloudUrl, setCloudUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [keySet, setKeySet] = useState(false)
  const [lastSynced, setLastSynced] = useState<string | null>(null)

  // Detection: mode/URL are edited on the Shield page; saving max tokens must send them back unchanged
  const detectionRef = useRef<{ mode: string; api_url: string } | null>(null)
  const [detectionMaxTokens, setDetectionMaxTokens] = useState<string | undefined>(undefined)

  const loadCloud = useCallback(() =>
    getShieldCloudSettings()
      .then((s) => {
        setCloudUrl(s.cloud_url)
        setKeySet(s.api_key_set)
        setLastSynced(s.last_synced_at)
      })
      .catch((e) => reportError(e, 'Failed to load Shield cloud settings')), [])

  useEffect(() => {
    void loadCloud()
    getScamDetectionSettings()
      .then((s) => {
        detectionRef.current = { mode: s.mode, api_url: s.api_url }
        setDetectionMaxTokens(String(s.max_tokens || 200))
      })
      .catch((e) => reportError(e, 'Failed to load detection settings'))
  }, [loadCloud])

  const save = useAsyncAction(async () => {
    await setShieldCloudSettings(cloudUrl.trim(), apiKey)
    if (apiKey) setKeySet(true)
    setApiKey('')
    toast.success('Cloud sync settings saved')
  }, { errorContext: "Couldn't save cloud sync settings" })

  const sync = useAsyncAction(async () => {
    const r = await triggerSync()
    if (r.status === 'success' || r.status === 'reloaded') {
      toast.success('Synced successfully')
    } else {
      toast.error(`Sync ${r.status}${r.error ? `: ${r.error}` : ''}`)
    }
    await loadCloud()
  }, { errorContext: 'Sync failed' })

  const maxTokens = useSettingField<string>({
    initial: '200',
    serverValue: detectionMaxTokens,
    set: (v) => {
      const cfg = detectionRef.current
      if (!cfg) return Promise.reject(new Error('Detection settings not loaded yet'))
      // Empty api_key keeps the stored key (external mode)
      return setScamDetectionSettings(cfg.mode, cfg.api_url, '', Number(v))
    },
    validate: validateShieldMaxTokens,
    debounceMs: 800,
    label: 'detection max tokens',
  })

  return (
    <>
      <Section
        title="Cloud Sync"
        className="mb-8"
        description="Connect to the Common Defense cloud to receive pipeline updates, scam signatures, and scenario libraries. Your scan results and reports will be synced to improve detection for all users."
      >
        <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); void save.run() }}>
          <div>
            <label className="block text-xs font-medium text-slate-400 mb-1" htmlFor="shield-cloud-url">Cloud API endpoint</label>
            <input
              id="shield-cloud-url"
              type="url"
              placeholder="https://api.shield.commondefense.app/v1"
              value={cloudUrl}
              onChange={(e) => setCloudUrl(e.target.value)}
              className={fieldClass}
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-400 mb-1" htmlFor="shield-api-key">
              API key {keySet && <span className="text-emerald-400">(set)</span>}
            </label>
            <input
              id="shield-api-key"
              type="password"
              autoComplete="off"
              placeholder={keySet ? '•••••••• (leave blank to keep the current key)' : 'Enter your API key'}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              className={fieldClass}
            />
            <p className="mt-1 text-[11px] text-slate-500">Stored encrypted in the vault; it is never shown again.</p>
          </div>
          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={save.pending}
              className="rounded-lg bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-40"
            >
              {save.pending ? 'Saving…' : 'Save'}
            </button>
            <button
              type="button"
              disabled={sync.pending || !cloudUrl}
              onClick={() => void sync.run()}
              className="rounded-lg bg-indigo-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-40"
            >
              {sync.pending ? 'Syncing…' : 'Sync Now'}
            </button>
          </div>
          {lastSynced && (
            <p className="text-xs text-slate-500">Last synced: {fmtDateTime(lastSynced)}</p>
          )}
        </form>
      </Section>

      <Section
        title="Detection Settings"
        className="mb-8"
        description="Configure how the local scam detection pipeline runs."
      >
        <div className="flex items-center gap-3">
          <label className="text-xs text-slate-400" htmlFor="shield-max-tokens">Max tokens per LLM call</label>
          <input
            id="shield-max-tokens"
            type="number"
            inputMode="numeric"
            min={50}
            max={2000}
            step={50}
            value={maxTokens.value}
            disabled={detectionMaxTokens === undefined}
            onChange={(e) => maxTokens.setValue(e.target.value)}
            onBlur={maxTokens.flush}
            onKeyDown={(e) => { if (e.key === 'Enter') maxTokens.flush() }}
            className="w-24 rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-sm text-slate-200 focus:border-emerald-500 focus:outline-none disabled:opacity-60"
          />
        </div>
        <FieldStatus saving={maxTokens.saving} invalid={maxTokens.invalid} />
      </Section>
    </>
  )
}
