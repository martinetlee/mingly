import { useState } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import logo from '../assets/logo.png'
import {
  MessageSquare,
  Inbox,
  CheckSquare,
  Users,
  Building2,
  Radio,
  FileText,
  Brain,
  RefreshCw,
  Settings,
} from 'lucide-react'
import { bulkSync } from '../api/client'

const NAV = [
  { to: '/chats', icon: MessageSquare, label: 'Chats' },
  { to: '/inbox', icon: Inbox, label: 'Inbox' },
  { to: '/todos', icon: CheckSquare, label: 'Todos' },
  { to: '/contacts', icon: Users, label: 'Contacts' },
  { to: '/organizations', icon: Building2, label: 'Orgs' },
  { to: '/tracked', icon: Radio, label: 'Tracked' },
  { to: '/intelligence?tab=briefing', icon: FileText, label: 'Briefing' },
  { to: '/intelligence', icon: Brain, label: 'Intelligence' },
]

export default function Sidebar() {
  const location = useLocation()
  const [syncing, setSyncing] = useState(false)

  const handleBulkSync = async () => {
    if (syncing) return
    setSyncing(true)
    try {
      await bulkSync()
    } finally {
      setSyncing(false)
    }
  }

  return (
    <aside className="flex h-screen w-[4.5rem] flex-col items-center bg-slate-950 py-4">
      {/* Logo */}
      <div className="mb-6 flex h-9 w-9 items-center justify-center select-none">
        <img src={logo} alt="Mingly" className="h-9 w-9 object-contain" />
      </div>

      {/* Nav */}
      <nav className="flex flex-1 flex-col gap-1">
        {NAV.map(({ to, icon: Icon, label }) => {
          // Custom active detection for intelligence tabs
          const url = new URL(to, 'http://x')
          const isActive = url.searchParams.has('tab')
            ? location.pathname === url.pathname && new URLSearchParams(location.search).get('tab') === url.searchParams.get('tab')
            : location.pathname === url.pathname && !new URLSearchParams(location.search).has('tab')
          return (
            <NavLink
              key={to}
              to={to}
              title={label}
              className={
                `flex flex-col items-center justify-center gap-0.5 rounded-lg px-1 py-1.5 transition-colors ${
                  isActive
                    ? 'bg-indigo-500/20 text-indigo-400'
                    : 'text-slate-500 hover:bg-slate-800 hover:text-slate-300'
                }`
              }
            >
              <Icon size={18} />
              <span className="text-[9px] leading-tight">{label}</span>
            </NavLink>
          )
        })}
      </nav>

      {/* Bulk sync */}
      <button
        onClick={handleBulkSync}
        disabled={syncing}
        title="Sync latest 50 chats"
        className="mb-1 flex flex-col items-center justify-center gap-0.5 rounded-lg px-1 py-1.5 text-slate-500 hover:bg-slate-800 hover:text-slate-300 transition-colors disabled:opacity-40"
      >
        <RefreshCw size={18} className={syncing ? 'animate-spin' : ''} />
        <span className="text-[9px] leading-tight">Sync</span>
      </button>

      {/* Settings at bottom */}
      <NavLink
        to="/settings"
        title="Settings"
        className={({ isActive }) =>
          `flex flex-col items-center justify-center gap-0.5 rounded-lg px-1 py-1.5 transition-colors ${
            isActive
              ? 'bg-indigo-500/20 text-indigo-400'
              : 'text-slate-500 hover:bg-slate-800 hover:text-slate-300'
          }`
        }
      >
        <Settings size={18} />
        <span className="text-[9px] leading-tight">Settings</span>
      </NavLink>
    </aside>
  )
}
