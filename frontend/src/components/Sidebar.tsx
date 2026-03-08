import { useState } from 'react'
import { NavLink } from 'react-router-dom'
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
  { to: '/briefing', icon: FileText, label: 'Briefing' },
  { to: '/intelligence', icon: Brain, label: 'Intelligence' },
]

export default function Sidebar() {
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
    <aside className="flex h-screen w-16 flex-col items-center bg-slate-950 py-4">
      {/* Logo */}
      <div className="mb-6 flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-indigo-500 to-purple-500 text-white font-bold text-sm select-none">
        M
      </div>

      {/* Nav */}
      <nav className="flex flex-1 flex-col gap-1">
        {NAV.map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            title={label}
            className={({ isActive }) =>
              `flex h-10 w-10 items-center justify-center rounded-lg transition-colors ${
                isActive
                  ? 'bg-indigo-500/20 text-indigo-400'
                  : 'text-slate-500 hover:bg-slate-800 hover:text-slate-300'
              }`
            }
          >
            <Icon size={20} />
          </NavLink>
        ))}
      </nav>

      {/* Bulk sync */}
      <button
        onClick={handleBulkSync}
        disabled={syncing}
        title="Sync latest 50 chats"
        className="mb-1 flex h-10 w-10 items-center justify-center rounded-lg text-slate-500 hover:bg-slate-800 hover:text-slate-300 transition-colors disabled:opacity-40"
      >
        <RefreshCw size={20} className={syncing ? 'animate-spin' : ''} />
      </button>

      {/* Settings at bottom */}
      <NavLink
        to="/settings"
        title="Settings"
        className={({ isActive }) =>
          `flex h-10 w-10 items-center justify-center rounded-lg transition-colors ${
            isActive
              ? 'bg-indigo-500/20 text-indigo-400'
              : 'text-slate-500 hover:bg-slate-800 hover:text-slate-300'
          }`
        }
      >
        <Settings size={20} />
      </NavLink>
    </aside>
  )
}
