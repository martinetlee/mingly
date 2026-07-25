import React, { useState } from 'react'
import { NavLink, useLocation, useNavigate } from 'react-router-dom'
import logo from '../assets/logo.png'
import {
  Home,
  MessageSquare,
  Users,
  Building2,
  Radio,
  Shield,
  Brain,
  RefreshCw,
  Settings,
  Search,
  Plus,
} from 'lucide-react'
import { bulkSync } from '../api/client'
import { useWorkspaces } from '../context/WorkspaceContext'
import WorkspaceDialog from './WorkspaceDialog'

const WS_RING: Record<string, string> = {
  indigo: 'ring-indigo-400', emerald: 'ring-emerald-400', rose: 'ring-rose-400',
  amber: 'ring-amber-400', violet: 'ring-violet-400', sky: 'ring-sky-400', slate: 'ring-slate-400',
}

const NAV = [
  { to: '/home', icon: Home, label: 'Home' },
  { to: '/chats', icon: MessageSquare, label: 'Chats' },
{ to: '/contacts', icon: Users, label: 'Contacts' },
  { to: '/organizations', icon: Building2, label: 'Orgs' },
  { to: '/tracked', icon: Radio, label: 'Tracked' },
  { to: '/shield', icon: Shield, label: 'Shield' },
  { to: '/intelligence', icon: Brain, label: 'Intelligence' },
]

export default function Sidebar({ onSearchClick }: { onSearchClick?: () => void }) {
  const location = useLocation()
  const [syncing, setSyncing] = useState(false)
  const navigate = useNavigate()
  const { workspaces, activeWorkspace, setActiveWorkspaceId, refreshWorkspaces } = useWorkspaces()
  const [showWorkspaceDialog, setShowWorkspaceDialog] = useState(false)
  const [editingWorkspace, setEditingWorkspace] = useState<import('../api/types').Workspace | null>(null)

  // Kanban workspaces land on their board; list workspaces (and "All") on the chat list.
  // viewModeHint covers freshly created workspaces not yet in the workspaces array.
  const selectWorkspace = (id: number | null, viewModeHint?: string) => {
    setActiveWorkspaceId(id)
    const mode = viewModeHint ?? (id === null ? null : workspaces.find((w) => w.id === id)?.view_mode)
    navigate(mode === 'kanban' ? '/board' : '/chats')
  }

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
      <div className="mb-4 flex h-9 w-9 items-center justify-center select-none">
        <img src={logo} alt="Common Defense" className="h-9 w-9 object-contain" />
      </div>

      {/* Workspace switcher */}
      <div className="mb-3 flex max-h-56 flex-col items-center gap-1.5 overflow-y-auto">
        <button
          onClick={() => selectWorkspace(null)}
          title="All chats"
          className={`flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full text-[10px] font-semibold transition-all ${
            activeWorkspace === null
              ? 'bg-indigo-500/30 text-indigo-300 ring-2 ring-indigo-400'
              : 'bg-slate-800 text-slate-400 hover:bg-slate-700'
          }`}
        >
          All
        </button>
        {workspaces.map((ws) => (
          <button
            key={ws.id}
            onClick={() => selectWorkspace(ws.id)}
            onContextMenu={(e) => {
              e.preventDefault()
              setEditingWorkspace(ws)
              setShowWorkspaceDialog(true)
            }}
            title={`${ws.name} — ${ws.member_count} chats${ws.unallocated_count ? `, ${ws.unallocated_count} to triage` : ''} (right-click to edit)`}
            className={`relative flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full text-sm transition-all ${
              activeWorkspace?.id === ws.id
                ? `bg-slate-700 ring-2 ${WS_RING[ws.color] ?? 'ring-indigo-400'}`
                : 'bg-slate-800 hover:bg-slate-700'
            }`}
          >
            {ws.emoji || ws.name[0].toUpperCase()}
            {ws.unallocated_count > 0 && (
              <span className="absolute -right-0.5 -top-0.5 flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-rose-500 px-0.5 text-[8px] font-bold text-white">
                {ws.unallocated_count > 9 ? '9+' : ws.unallocated_count}
              </span>
            )}
          </button>
        ))}
        <button
          onClick={() => { setEditingWorkspace(null); setShowWorkspaceDialog(true) }}
          title="New workspace"
          className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full border border-dashed border-slate-700 text-slate-500 transition-colors hover:border-slate-500 hover:text-slate-300"
        >
          <Plus size={14} />
        </button>
      </div>
      <div className="mb-3 h-px w-8 bg-slate-800" />

      <WorkspaceDialog
        open={showWorkspaceDialog}
        workspace={editingWorkspace}
        onClose={() => setShowWorkspaceDialog(false)}
        onSaved={async (id, viewMode) => {
          setShowWorkspaceDialog(false)
          const wasActive = editingWorkspace != null && activeWorkspace?.id === editingWorkspace.id
          setEditingWorkspace(null)
          await refreshWorkspaces()
          if (id != null) selectWorkspace(id, viewMode)
          else if (wasActive) selectWorkspace(null) // active workspace was deleted
        }}
      />

      {/* Nav */}
      <nav className="flex flex-1 flex-col gap-1">
        {NAV.map(({ to, icon: Icon, label }) => {
          // Custom active detection for intelligence tabs
          const url = new URL(to, 'http://x')
          const isActive = url.searchParams.has('tab')
            ? location.pathname === url.pathname && new URLSearchParams(location.search).get('tab') === url.searchParams.get('tab')
            : location.pathname === url.pathname && !new URLSearchParams(location.search).has('tab')
          return (
            <React.Fragment key={to}>
              <NavLink
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
              {/* Search button — positioned after Chats */}
              {to === '/chats' && (
                <button
                  onClick={onSearchClick}
                  title="Search (⌘K)"
                  className="flex flex-col items-center justify-center gap-0.5 rounded-lg px-1 py-1.5 text-slate-500 hover:bg-slate-800 hover:text-slate-300 transition-colors"
                >
                  <Search size={18} />
                  <span className="text-[9px] leading-tight">Search</span>
                </button>
              )}
            </React.Fragment>
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
