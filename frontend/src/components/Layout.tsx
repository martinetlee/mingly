import { useCallback, useMemo, useState } from 'react'
import { Outlet, useNavigate } from 'react-router-dom'
import Sidebar from './Sidebar'
import ShortcutHelp from './ShortcutHelp'
import GlobalSearch from './GlobalSearch'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import { useWebSocket } from '../hooks/useWebSocket'
import type { WSEvent } from '../api/types'

const NAV_ROUTES = ['/home', '/chats', '/contacts', '/organizations', '/tracked', '/shield', '/intelligence?tab=briefing', '/intelligence', '/settings']

export default function Layout() {
  const [helpOpen, setHelpOpen] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)
  const navigate = useNavigate()

  const shortcuts = useMemo(
    () => ({
      'escape': () => { setHelpOpen(false); setSearchOpen(false) },
      'shift+?': () => setHelpOpen((v) => !v),
      'mod+k': () => setSearchOpen((v) => !v),
      'mod+1': () => navigate(NAV_ROUTES[0]),
      'mod+2': () => navigate(NAV_ROUTES[1]),
      'mod+3': () => navigate(NAV_ROUTES[2]),
      'mod+4': () => navigate(NAV_ROUTES[3]),
      'mod+5': () => navigate(NAV_ROUTES[4]),
      'mod+6': () => navigate(NAV_ROUTES[5]),
      'mod+7': () => navigate(NAV_ROUTES[6]),
      'mod+8': () => navigate(NAV_ROUTES[7]),
      'mod+9': () => navigate(NAV_ROUTES[8]),
    }),
    [navigate],
  )

  useKeyboardShortcuts(shortcuts)

  // Handle global WebSocket events (e.g., notification clicks)
  useWebSocket(useCallback((event: WSEvent) => {
    if (event.type === 'navigate_to_chat') {
      navigate(`/chats?chatId=${event.chat_id}`)
    }
  }, [navigate]))

  return (
    <div className="flex h-screen overflow-hidden bg-slate-900">
      <Sidebar onSearchClick={() => setSearchOpen(true)} />
      <main className="flex flex-1 overflow-hidden">
        <Outlet />
      </main>
      <ShortcutHelp open={helpOpen} onClose={() => setHelpOpen(false)} />
      <GlobalSearch open={searchOpen} onClose={() => setSearchOpen(false)} />
    </div>
  )
}
