import { useMemo, useState } from 'react'
import { Outlet, useNavigate } from 'react-router-dom'
import Sidebar from './Sidebar'
import ShortcutHelp from './ShortcutHelp'
import GlobalSearch from './GlobalSearch'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import type { ShortcutMap } from '../hooks/useKeyboardShortcuts'
import { useWsEvent } from '../hooks/useWebSocket'
import { LOCK_ACTION } from '../lib/nav'
import { useNav } from '../context/FeaturesContext'
import { useLockVault } from '../hooks/useLockVault'

export default function Layout() {
  const [helpOpen, setHelpOpen] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)
  const navigate = useNavigate()
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

  // Native notification clicks are relayed by the backend as navigate_to_chat
  useWsEvent('navigate_to_chat', (event) => {
    navigate(`/chats?chatId=${event.chat_id}`)
  })

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
