import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { getVaultStatus, openExternalUrl, validateSession } from './api/client'
import { clearSessionToken, getSessionToken, onSessionExpired } from './api/session'
import { isAbortError, isSessionExpiredError } from './lib/http'
import Layout from './components/layout/Layout'
import NotificationBridge from './components/layout/NotificationBridge'
import Onboarding from './pages/Onboarding'
import Unlock from './pages/Unlock'
import Home from './pages/Home'
import Chats from './pages/Chats'
import Mail from './pages/Mail'
import Calendar from './pages/Calendar'
import OpenConversation from './pages/OpenConversation'
import Contacts from './pages/Contacts'
import Organizations from './pages/Organizations'
import Settings from './pages/Settings'
import Tracked from './pages/Tracked'
import Intelligence from './pages/Intelligence'
import Person from './pages/Person'
import Mention from './pages/Mention'
import Shield from './pages/Shield'
import LlmSetup from './pages/LlmSetup'
import Board from './pages/Board'
import { WorkspaceProvider } from './context/WorkspaceContext'
import { WebSocketProvider } from './context/WebSocketContext'
import { FeaturesProvider, useFeatures, useFeaturesLoaded } from './context/FeaturesContext'
import { ThemeProvider } from './context/ThemeContext'
import { ToastProvider } from './components/ui/Toast'
import { ConfirmProvider } from './components/ui/ConfirmDialog'

type AppState = 'loading' | 'backend-unavailable' | 'vault-unset' | 'vault-locked' | 'llm-setup' | 'app'

const RETRY_BASE_MS = 1000
const RETRY_MAX_MS = 10000

/**
 * Decide the initial screen from GET /api/session/validate, which reports
 * both this tab's token validity and the vault status (a vault unlocked by
 * another window doesn't mean *this* tab has a session). Falls back to
 * vault status on backends without the endpoint. Throws when the backend
 * can't be reached — the caller shows "backend unavailable", never "create vault".
 *
 * A session goes straight to the app whatever accounts are connected: with
 * none, Home and Chats point to Settings → Connections.
 */
async function resolveInitialState(signal: AbortSignal): Promise<AppState> {
  const v = await validateSession(signal)
  if (v) {
    if (v.valid) return 'app'
    // Stale token from an earlier session: drop it so nothing sends it again
    if (getSessionToken()) clearSessionToken()
    return v.vault_status === 'unset' ? 'vault-unset' : 'vault-locked'
  }
  const { status } = await getVaultStatus()
  if (status === 'unset') return 'vault-unset'
  if (status === 'locked') return 'vault-locked'
  // Unlocked, but this tab holds no token → unlock to obtain one
  return getSessionToken() ? 'app' : 'vault-locked'
}

/** /shield exists only while the Shield feature is on */
export function ShieldRoute() {
  const { shield } = useFeatures()
  const loaded = useFeaturesLoaded()
  if (!loaded) return null
  return shield ? <Shield /> : <Navigate to="/home" replace />
}

function AppProviders({ children }: { children: ReactNode }) {
  return (
    <ThemeProvider>
      <ToastProvider>
        <ConfirmProvider>{children}</ConfirmProvider>
      </ToastProvider>
    </ThemeProvider>
  )
}

export default function App() {
  return (
    <AppProviders>
      <AppRoot />
    </AppProviders>
  )
}

function AppRoot() {
  const [state, setState] = useState<AppState>('loading')
  const [retryIn, setRetryIn] = useState<number | null>(null)
  const attemptRef = useRef(0)
  const [bootSeq, setBootSeq] = useState(0)

  useEffect(() => {
    const ctrl = new AbortController()
    let timer: ReturnType<typeof setTimeout> | null = null
    resolveInitialState(ctrl.signal)
      .then((next) => {
        if (ctrl.signal.aborted) return
        attemptRef.current = 0
        setRetryIn(null)
        setState(next)
      })
      .catch((e) => {
        if (ctrl.signal.aborted || isAbortError(e)) return
        // 401: this tab needs to unlock
        if (isSessionExpiredError(e)) { setState('vault-locked'); return }
        // Backend down or restarting — never mistake this for "no vault"
        const delay = Math.min(RETRY_MAX_MS, RETRY_BASE_MS * 2 ** attemptRef.current)
        attemptRef.current++
        setState('backend-unavailable')
        setRetryIn(delay)
        timer = setTimeout(() => setBootSeq((n) => n + 1), delay)
      })
    return () => {
      ctrl.abort()
      if (timer) clearTimeout(timer)
    }
  }, [bootSeq])

  const retryNow = useCallback(() => setBootSeq((n) => n + 1), [])

  useEffect(() => onSessionExpired(() => setState('vault-locked')), [])

  // Intercept clicks on external links and open them in the default browser
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      const anchor = (e.target as HTMLElement).closest('a')
      if (!anchor) return
      const href = anchor.getAttribute('href')
      if (!href) return
      // Only intercept absolute http/https URLs (external links)
      if (href.startsWith('http://') || href.startsWith('https://')) {
        e.preventDefault()
        e.stopPropagation()
        openExternalUrl(href)
      }
    }
    document.addEventListener('click', handler, true)
    return () => document.removeEventListener('click', handler, true)
  }, [])

  if (state === 'loading') {
    return (
      <div className="flex h-screen items-center justify-center bg-slate-900">
        <div className="text-sm text-slate-400">Loading…</div>
      </div>
    )
  }

  if (state === 'backend-unavailable') {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-3 bg-slate-900">
        <div className="text-sm text-slate-300">Can't reach the Mingly backend</div>
        <div className="text-xs text-slate-500">
          Retrying{retryIn != null ? ` in ${Math.round(retryIn / 1000)}s` : ''}…
        </div>
        <button
          type="button"
          onClick={retryNow}
          className="rounded-lg bg-slate-800 px-3 py-1.5 text-xs text-slate-300 transition-colors hover:bg-slate-700"
        >
          Retry now
        </button>
      </div>
    )
  }

  if (state === 'vault-unset') {
    return <Onboarding onComplete={() => setState('llm-setup')} />
  }

  if (state === 'vault-locked') {
    return <Unlock onUnlock={() => setState('app')} />
  }

  if (state === 'llm-setup') {
    return <FeaturesProvider><LlmSetup onComplete={() => setState('app')} /></FeaturesProvider>
  }

  return (
    <WebSocketProvider>
      <FeaturesProvider>
      <BrowserRouter>
        <NotificationBridge />
        <Routes>
          <Route path="/" element={<WorkspaceProvider><Layout /></WorkspaceProvider>}>
            <Route index element={<Navigate to="/home" replace />} />
            <Route path="home" element={<Home />} />
            <Route path="chats" element={<Chats />} />
            <Route path="mail" element={<Mail />} />
            <Route path="calendar" element={<Calendar />} />
            <Route path="open/:conversationId" element={<OpenConversation />} />
            <Route path="board" element={<Board />} />
            <Route path="contacts" element={<Contacts />} />
            <Route path="organizations" element={<Organizations />} />
            <Route path="tracked" element={<Tracked />} />
            <Route path="shield" element={<ShieldRoute />} />
            <Route path="briefing" element={<Navigate to="/intelligence?tab=briefing" replace />} />
            <Route path="intelligence" element={<Intelligence />} />
            <Route path="people/:personId" element={<Person />} />
            <Route path="mention/:channel/:externalId" element={<Mention />} />
            <Route path="settings" element={<Settings />} />
          </Route>
        </Routes>
      </BrowserRouter>
      </FeaturesProvider>
    </WebSocketProvider>
  )
}
