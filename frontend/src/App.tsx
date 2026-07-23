import { useEffect, useState } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { getVaultStatus, getAuthStatus, openExternalUrl } from './api/client'
import { onSessionExpired } from './api/session'
import Layout from './components/Layout'
import Onboarding from './pages/Onboarding'
import Unlock from './pages/Unlock'
import Home from './pages/Home'
import Chats from './pages/Chats'
import Contacts from './pages/Contacts'
import Organizations from './pages/Organizations'
import Settings from './pages/Settings'
import Tracked from './pages/Tracked'
import Intelligence from './pages/Intelligence'
import Profile from './pages/Profile'
import Shield from './pages/Shield'
import LlmSetup from './pages/LlmSetup'
import Board from './pages/Board'
import { WorkspaceProvider } from './context/WorkspaceContext'

type AppState = 'loading' | 'vault-unset' | 'vault-locked' | 'onboarding' | 'llm-setup' | 'app'

export default function App() {
  const [state, setState] = useState<AppState>('loading')

  useEffect(() => {
    getVaultStatus()
      .then(({ status }) => {
        if (status === 'unset') { setState('vault-unset'); return }
        if (status === 'locked') { setState('vault-locked'); return }
        // Vault unlocked — check Telegram auth
        return getAuthStatus().then((s) =>
          setState(s.authenticated ? 'app' : 'onboarding')
        )
      })
      .catch(() => setState('vault-unset'))
  }, [])

  useEffect(() => {
    onSessionExpired(() => setState('vault-locked'))
  }, [])

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

  const handleVaultUnlocked = () => {
    getAuthStatus()
      .then((s) => setState(s.authenticated ? 'app' : 'onboarding'))
      .catch(() => setState('onboarding'))
  }

  if (state === 'loading') {
    return (
      <div className="flex h-screen items-center justify-center bg-gray-50">
        <div className="text-gray-400 text-sm">Loading…</div>
      </div>
    )
  }

  if (state === 'vault-unset') {
    return <Onboarding needsVaultSetup onComplete={() => setState('llm-setup')} />
  }

  if (state === 'vault-locked') {
    return <Unlock onUnlock={handleVaultUnlocked} />
  }

  if (state === 'onboarding') {
    return <Onboarding onComplete={() => setState('llm-setup')} />
  }

  if (state === 'llm-setup') {
    return <LlmSetup onComplete={() => setState('app')} />
  }

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<WorkspaceProvider><Layout /></WorkspaceProvider>}>
          <Route index element={<Navigate to="/home" replace />} />
          <Route path="home" element={<Home />} />
          <Route path="chats" element={<Chats />} />
          <Route path="board" element={<Board />} />
          <Route path="contacts" element={<Contacts />} />
          <Route path="organizations" element={<Organizations />} />
          <Route path="tracked" element={<Tracked />} />
          <Route path="shield" element={<Shield />} />
          <Route path="briefing" element={<Navigate to="/intelligence?tab=briefing" replace />} />
          <Route path="intelligence" element={<Intelligence />} />
          <Route path="profile/:userId" element={<Profile />} />
          <Route path="settings" element={<Settings />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
