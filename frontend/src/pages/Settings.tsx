import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { getPrefs } from '../api/client'
import type { Prefs } from '../api/types'
import { useTheme } from '../hooks/useTheme'
import { SHORTCUTS_ENABLED_KEY, isShortcutsEnabled } from '../lib/keyboard'
import { reportError } from '../lib/toast'
import ModelSection from './settings/ModelSection'
import McpSection from './settings/McpSection'
import OrganizationsSection from './settings/OrganizationsSection'
import {
  BriefingSection, CatchUpSection, NotificationsSection, SecuritySection, TranslationSection,
} from './settings/PreferenceSections'
import ShieldTab from './settings/ShieldTab'
import Connections from './settings/Connections'
import { useFeatures } from '../context/FeaturesContext'
import TagsManager from './settings/TagsManager'
import { Card, Section, Switch } from './settings/shared'

type SettingsTab = 'general' | 'connections' | 'tags' | 'shield'

const TABS: SettingsTab[] = ['general', 'connections', 'tags', 'shield']

function readLocal(key: string): string | null {
  try { return localStorage.getItem(key) } catch { return null }
}

function writeLocal(key: string, value: string) {
  try { localStorage.setItem(key, value) } catch { /* storage unavailable: setting lasts this session */ }
}

export default function Settings() {
  const { theme, setTheme } = useTheme()
  // The tab lives in the URL (?tab=connections) so other screens can link to it
  const [searchParams, setSearchParams] = useSearchParams()
  const { shield } = useFeatures()
  const visibleTabs = TABS.filter((t) => t !== 'shield' || shield)
  const requested = searchParams.get('tab') as SettingsTab | null
  const settingsTab: SettingsTab = requested && visibleTabs.includes(requested) ? requested : 'general'
  const setSettingsTab = (tab: SettingsTab) => setSearchParams(tab === 'general' ? {} : { tab }, { replace: true })

  // One shared prefs read; each field adopts its server value from it
  const [prefs, setPrefs] = useState<Prefs | undefined>(undefined)
  useEffect(() => {
    getPrefs()
      .then(setPrefs)
      .catch((e) => reportError(e, 'Failed to load settings'))
  }, [])

  // Local-only (this device) preferences
  const [devMode, setDevMode] = useState(() => readLocal('developer_mode') === 'true')
  const [shortcutsEnabled, setShortcutsEnabled] = useState(isShortcutsEnabled)

  const handleDevMode = (checked: boolean) => {
    setDevMode(checked)
    writeLocal('developer_mode', String(checked))
    window.dispatchEvent(new Event('developer_mode_changed'))
  }

  const handleShortcutsToggle = (enabled: boolean) => {
    setShortcutsEnabled(enabled)
    writeLocal(SHORTCUTS_ENABLED_KEY, String(enabled))
  }

  return (
    <div className="flex flex-1 flex-col overflow-y-auto">
      {/* Header */}
      <div className="border-b border-slate-700/50 px-8 pt-8 pb-0">
        <h1 className="mb-4 text-lg font-semibold text-slate-100">Settings</h1>
        {/* Tab bar */}
        <div className="flex gap-1" role="tablist">
          {visibleTabs.map((tab) => (
            <button
              key={tab}
              role="tab"
              aria-selected={settingsTab === tab}
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
            <ModelSection />
            <NotificationsSection prefs={prefs} />
            <TranslationSection prefs={prefs} />
            <BriefingSection prefs={prefs} />
            <CatchUpSection prefs={prefs} />
            <OrganizationsSection />
            <SecuritySection />

            {/* ── Appearance ── */}
            <Section title="Appearance" description="Choose between dark and light mode.">
              <div className="flex items-center gap-3">
                {(['dark', 'light'] as const).map((t) => (
                  <button
                    key={t}
                    type="button"
                    aria-pressed={theme === t}
                    onClick={() => setTheme(t)}
                    className={`rounded-lg px-4 py-2 text-xs font-medium capitalize transition-colors ${
                      theme === t
                        ? 'bg-indigo-600 text-white'
                        : 'border border-slate-600 bg-slate-700 text-slate-300 hover:bg-slate-600'
                    }`}
                  >
                    {t}
                  </button>
                ))}
              </div>
            </Section>

            {/* ── Developer Mode (Shield-only debugging) ── */}
            {shield && (
            <Section
              title="Developer Mode"
              description="Enable advanced features for debugging and testing the scam detection pipeline."
            >
              <label className="flex items-center gap-3 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={devMode}
                  onChange={(e) => handleDevMode(e.target.checked)}
                  className="rounded border-slate-600 bg-slate-700 text-indigo-500 focus:ring-0 h-4 w-4"
                />
                <span className="text-xs text-slate-300">Enable developer mode</span>
              </label>
            </Section>
            )}

            {/* ── Keyboard Shortcuts ── */}
            <Section
              title="Keyboard Shortcuts"
              description={<>Press <kbd className="rounded border border-slate-600 bg-slate-700 px-1 py-0.5 text-[10px] font-mono">?</kbd> anywhere to see all available shortcuts.</>}
            >
              <Card>
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <p className="text-sm text-slate-200">Enable keyboard shortcuts</p>
                    <p className="text-xs text-slate-400 mt-0.5">j/k navigation, /, i, Cmd+1-7, and more</p>
                  </div>
                  <Switch checked={shortcutsEnabled} onChange={handleShortcutsToggle} label="Enable keyboard shortcuts" />
                </div>
              </Card>
            </Section>

            <McpSection />
          </>
        )}

        {settingsTab === 'connections' && <Connections />}

        {settingsTab === 'tags' && (
          <>
            <p className="mb-6 text-xs text-slate-400">
              Manage tags for organizing chats. Tags can be assigned to chats from the chat info panel.
            </p>
            <TagsManager />
          </>
        )}

        {shield && settingsTab === 'shield' && <ShieldTab />}
      </div>
    </div>
  )
}
