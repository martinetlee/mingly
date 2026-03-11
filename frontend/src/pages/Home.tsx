import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { MessageSquare, Mail, Users, ArrowRight, FileText } from 'lucide-react'
import type { DashboardData } from '../api/types'
import { getDashboard } from '../api/client'

function getGreeting(): string {
  const hour = new Date().getHours()
  if (hour < 12) return 'Good morning'
  if (hour < 17) return 'Good afternoon'
  return 'Good evening'
}

function fmtRelative(iso: string): string {
  const d = new Date(iso)
  const now = new Date()
  const diffMs = now.getTime() - d.getTime()
  const diffMins = Math.floor(diffMs / 60000)
  if (diffMins < 1) return 'Just now'
  if (diffMins < 60) return `${diffMins}m ago`
  const diffHours = Math.floor(diffMins / 60)
  if (diffHours < 24) return `${diffHours}h ago`
  const diffDays = Math.floor(diffHours / 24)
  if (diffDays < 7) return `${diffDays}d ago`
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

export default function Home() {
  const [data, setData] = useState<DashboardData | null>(null)
  const [loading, setLoading] = useState(true)
  const navigate = useNavigate()

  useEffect(() => {
    getDashboard()
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  // Auto-refresh on WebSocket new_message events
  useEffect(() => {
    const handler = (e: MessageEvent) => {
      try {
        const ev = JSON.parse(e.data)
        if (ev.type === 'new_message') {
          getDashboard().then(setData).catch(() => {})
        }
      } catch {}
    }
    // Try to listen on existing WebSocket — the WS is managed by Layout/App
    // For now, poll every 60s as a fallback
    const interval = setInterval(() => {
      getDashboard().then(setData).catch(() => {})
    }, 60000)
    return () => clearInterval(interval)
  }, [])

  if (loading) {
    return (
      <div className="flex flex-1 items-center justify-center text-slate-500 text-sm">
        Loading dashboard...
      </div>
    )
  }

  if (!data) {
    return (
      <div className="flex flex-1 items-center justify-center text-slate-500 text-sm">
        Failed to load dashboard
      </div>
    )
  }

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-3xl px-6 py-8">
        {/* Greeting */}
        <h1 className="text-2xl font-bold text-gray-100 mb-6">{getGreeting()}</h1>

        {/* Stats */}
        <div className="grid grid-cols-3 gap-4 mb-8">
          <div className="rounded-xl bg-slate-800 border border-slate-700/50 p-4">
            <div className="flex items-center gap-2 mb-1">
              <Mail size={14} className="text-indigo-400" />
              <span className="text-xs text-slate-400">Unread</span>
            </div>
            <p className="text-2xl font-bold text-gray-100">{data.stats.total_unread}</p>
          </div>
          <div className="rounded-xl bg-slate-800 border border-slate-700/50 p-4">
            <div className="flex items-center gap-2 mb-1">
              <MessageSquare size={14} className="text-emerald-400" />
              <span className="text-xs text-slate-400">Messages Today</span>
            </div>
            <p className="text-2xl font-bold text-gray-100">{data.stats.messages_today}</p>
          </div>
          <div className="rounded-xl bg-slate-800 border border-slate-700/50 p-4">
            <div className="flex items-center gap-2 mb-1">
              <Users size={14} className="text-amber-400" />
              <span className="text-xs text-slate-400">Active Contacts</span>
            </div>
            <p className="text-2xl font-bold text-gray-100">{data.stats.active_contacts}</p>
          </div>
        </div>

        {/* Needs Reply */}
        <div className="mb-8">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-xs font-medium uppercase tracking-wider text-slate-400">Needs Reply</h2>
            <Link to="/chats" className="text-[10px] text-indigo-400 hover:text-indigo-300 transition-colors flex items-center gap-0.5">
              View all <ArrowRight size={10} />
            </Link>
          </div>
          {data.needs_reply.length === 0 ? (
            <div className="rounded-xl bg-slate-800 border border-slate-700/50 px-4 py-6 text-center">
              <p className="text-sm text-slate-500">You're all caught up! No conversations waiting for your reply.</p>
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              {data.needs_reply.map((item) => (
                <button
                  key={item.chat.telegram_id}
                  onClick={() => navigate(`/chats?select=${item.chat.telegram_id}`)}
                  className="flex items-center gap-3 rounded-xl bg-slate-800 border border-slate-700/50 px-4 py-3 text-left hover:bg-slate-700/50 transition-colors w-full"
                >
                  <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full bg-slate-700 text-sm font-medium text-slate-300">
                    {item.chat.name.charAt(0).toUpperCase()}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-2">
                      <p className="truncate text-sm font-medium text-gray-200">{item.chat.name}</p>
                      <span className="flex-shrink-0 text-[10px] text-slate-500">
                        {fmtRelative(item.last_message.date)}
                      </span>
                    </div>
                    <p className="truncate text-xs text-slate-400 mt-0.5">
                      {item.last_message.sender_name && (
                        <span className="text-slate-300">{item.last_message.sender_name}: </span>
                      )}
                      {item.last_message.text || <span className="italic">[media]</span>}
                    </p>
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Contacts Needing Attention */}
        <div className="mb-8">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-xs font-medium uppercase tracking-wider text-slate-400">Important Contacts</h2>
            <Link to="/contacts" className="text-[10px] text-indigo-400 hover:text-indigo-300 transition-colors flex items-center gap-0.5">
              View all <ArrowRight size={10} />
            </Link>
          </div>
          {data.attention_contacts.length === 0 ? (
            <div className="rounded-xl bg-slate-800 border border-slate-700/50 px-4 py-6 text-center">
              <p className="text-sm text-slate-500">All contacts are in good standing.</p>
            </div>
          ) : (
            <div className="flex flex-col gap-1">
              {data.attention_contacts.map((c) => (
                <Link
                  key={c.id}
                  to={`/profile/${c.telegram_id}`}
                  className="flex items-center gap-3 rounded-lg px-3 py-2 hover:bg-slate-800 transition-colors"
                >
                  <div className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full bg-slate-700 text-[10px] font-medium text-slate-300">
                    {c.name.charAt(0).toUpperCase()}
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm text-gray-200">{c.name}</p>
                    {c.primary_position && (
                      <p className="truncate text-[10px] text-slate-500">
                        {c.primary_position.title
                          ? `${c.primary_position.title} @ ${c.primary_position.organization}`
                          : c.primary_position.organization}
                      </p>
                    )}
                  </div>
                  {c.relationship_status && (
                    <span className="flex-shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium bg-slate-700 text-slate-400">
                      {c.relationship_status}
                    </span>
                  )}
                </Link>
              ))}
            </div>
          )}
        </div>

        {/* Latest Briefing */}
        <div className="mb-8">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-xs font-medium uppercase tracking-wider text-slate-400">Latest Briefing</h2>
            <Link
              to="/intelligence?tab=briefing"
              className="text-[10px] text-indigo-400 hover:text-indigo-300 transition-colors flex items-center gap-0.5"
            >
              View full <ArrowRight size={10} />
            </Link>
          </div>
          {!data.latest_briefing ? (
            <div className="rounded-xl bg-slate-800 border border-slate-700/50 px-4 py-6 text-center">
              <p className="text-sm text-slate-500">No briefing yet.</p>
              <Link
                to="/intelligence?tab=briefing"
                className="mt-1 text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
              >
                Generate one from Intelligence
              </Link>
            </div>
          ) : (
            <div className="rounded-xl bg-slate-800 border border-slate-700/50 p-4">
              <div className="flex items-center gap-2 mb-3">
                <FileText size={12} className="text-slate-400" />
                <span className="text-[10px] text-slate-500">
                  {fmtRelative(data.latest_briefing.generated_at)}
                </span>
              </div>
              <div className="flex flex-col gap-2">
                {data.latest_briefing.entries.map((e) => (
                  <div key={e.id} className="text-xs">
                    <span className="font-medium text-slate-300">{e.chat_name}: </span>
                    <span className="text-slate-400 line-clamp-2">{e.summary}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
