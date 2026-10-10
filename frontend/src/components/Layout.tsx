import { useMemo, useState } from 'react'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import Sidebar from './Sidebar'
import ErrorBoundary from './ui/ErrorBoundary'
import ShortcutHelp from './ShortcutHelp'
import GlobalSearch from './GlobalSearch'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import type { ShortcutMap } from '../hooks/useKeyboardShortcuts'
import { useWsEvent } from '../hooks/useWebSocket'
import { LOCK_ACTION } from '../lib/nav'
import { openConversationUrl } from '../lib/routes'
import { useNav } from '../context/FeaturesContext'
import { useLockVault } from '../hooks/useLockVault'
import { useScamCheckEvents } from '../features/chat/scamChecks'

export default function Layout() {
  const [helpOpen, setHelpOpen] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)
  const navigate = useNavigate()
  const location = useLocation()
  const { lock } = useLockVault()
  const { shortcuts: navShortcuts } = useNav()

  const shortcuts = useMemo(() => {
    const map: ShortcutMap = {
      // Only consume Escape when it actually closed something
      'escape': () => {
        if (!searchOpen) return false
        setSearchOpen(false)
      },
      'shift+?': () => setHelpOpen((v) => !v),
      'mod+k': () => setSearchOpen((v) => !v),
    }
    for (const { combo, item } of navShortcuts) {
      map[combo] = () => navigate(item.to)
    }
    map[LOCK_ACTION.combo] = () => { void lock() }
    return map
  }, [navigate, searchOpen, lock, navShortcuts])

  useKeyboardShortcuts(shortcuts)
  // "Check for scam" can be started from any page showing messages (Chats, Mail, Contacts, Tracked)
  useScamCheckEvents()

  // Native notification clicks are relayed by the backend as navigate_to_conversation
  useWsEvent('navigate_to_conversation', (event) => {
    navigate(openConversationUrl(event.conversation_id))
  })

  return (
    <div className="flex h-screen overflow-hidden bg-slate-900">
      <Sidebar onSearchClick={() => setSearchOpen(true)} />
      <main className="flex flex-1 overflow-hidden">
        <ErrorBoundary key={location.pathname}>
          <Outlet />
        </ErrorBoundary>
      </main>
      <ShortcutHelp open={helpOpen} onClose={() => setHelpOpen(false)} />
      <GlobalSearch open={searchOpen} onClose={() => setSearchOpen(false)} />
    </div>
  )
}
