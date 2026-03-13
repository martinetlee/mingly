import { useEffect, useState } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { getVaultStatus, getAuthStatus } from './api/client'
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

type AppState = 'loading' | 'vault-unset' | 'vault-locked' | 'onboarding' | 'app'

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
    return <Onboarding needsVaultSetup onComplete={() => setState('app')} />
  }

  if (state === 'vault-locked') {
    return <Unlock onUnlock={handleVaultUnlocked} />
  }

  if (state === 'onboarding') {
    return <Onboarding onComplete={() => setState('app')} />
  }

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Layout />}>
          <Route index element={<Navigate to="/home" replace />} />
          <Route path="home" element={<Home />} />
          <Route path="chats" element={<Chats />} />
          <Route path="contacts" element={<Contacts />} />
          <Route path="organizations" element={<Organizations />} />
          <Route path="tracked" element={<Tracked />} />
          <Route path="briefing" element={<Navigate to="/intelligence?tab=briefing" replace />} />
          <Route path="intelligence" element={<Intelligence />} />
          <Route path="profile/:userId" element={<Profile />} />
          <Route path="settings" element={<Settings />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
