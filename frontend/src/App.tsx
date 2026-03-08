import { useEffect, useState } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { getAuthStatus } from './api/client'
import Layout from './components/Layout'
import Onboarding from './pages/Onboarding'
import Chats from './pages/Chats'
import Inbox from './pages/Inbox'
import Todos from './pages/Todos'
import Contacts from './pages/Contacts'
import Organizations from './pages/Organizations'
import Settings from './pages/Settings'

export default function App() {
  const [isAuthenticated, setIsAuthenticated] = useState<boolean | null>(null)

  useEffect(() => {
    getAuthStatus()
      .then((s) => setIsAuthenticated(s.authenticated))
      .catch(() => setIsAuthenticated(false))
  }, [])

  if (isAuthenticated === null) {
    return (
      <div className="flex h-screen items-center justify-center bg-gray-50">
        <div className="text-gray-400 text-sm">Loading…</div>
      </div>
    )
  }

  if (!isAuthenticated) {
    return <Onboarding onComplete={() => setIsAuthenticated(true)} />
  }

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Layout />}>
          <Route index element={<Navigate to="/chats" replace />} />
          <Route path="chats" element={<Chats />} />
          <Route path="inbox" element={<Inbox />} />
          <Route path="todos" element={<Todos />} />
          <Route path="contacts" element={<Contacts />} />
          <Route path="organizations" element={<Organizations />} />
          <Route path="settings" element={<Settings />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
