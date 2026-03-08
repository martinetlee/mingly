import { NavLink } from 'react-router-dom'
import {
  MessageSquare,
  Inbox,
  CheckSquare,
  Users,
  Building2,
  Settings,
} from 'lucide-react'

const NAV = [
  { to: '/chats', icon: MessageSquare, label: 'Chats' },
  { to: '/inbox', icon: Inbox, label: 'Inbox' },
  { to: '/todos', icon: CheckSquare, label: 'Todos' },
  { to: '/contacts', icon: Users, label: 'Contacts' },
  { to: '/organizations', icon: Building2, label: 'Orgs' },
]

export default function Sidebar() {
  return (
    <aside className="flex h-screen w-16 flex-col items-center bg-gray-900 py-4">
      {/* Logo */}
      <div className="mb-6 flex h-9 w-9 items-center justify-center rounded-xl bg-blue-500 text-white font-bold text-sm select-none">
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
                  ? 'bg-gray-700 text-white'
                  : 'text-gray-400 hover:bg-gray-800 hover:text-white'
              }`
            }
          >
            <Icon size={20} />
          </NavLink>
        ))}
      </nav>

      {/* Settings at bottom */}
      <NavLink
        to="/settings"
        title="Settings"
        className={({ isActive }) =>
          `flex h-10 w-10 items-center justify-center rounded-lg transition-colors ${
            isActive
              ? 'bg-gray-700 text-white'
              : 'text-gray-400 hover:bg-gray-800 hover:text-white'
          }`
        }
      >
        <Settings size={20} />
      </NavLink>
    </aside>
  )
}
