import { useState, useRef, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  Brain,
  Send,
  Wrench,
  ChevronDown,
  ChevronRight,
  AlertCircle,
  Loader2,
  Plus,
  Trash2,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  Check,
  X,
  AtSign,
  RefreshCw,
  Search,
  CheckCheck,
  Bookmark,
  Tag,
  Newspaper,
  Radio,
  RotateCcw,
} from 'lucide-react'
import {
  listConversations,
  createConversation,
  getConversation,
  updateConversationTitle,
  deleteConversation,
  appendConversationMessage,
  suggestConversationTitle,
  getMentions,
  getTags,
  getPrefs,
  setBriefingLastRun,
} from '../api/client'
import type { ConversationSummary, MentionGroup, MentionMessage, BriefingSummary } from '../api/types'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface UserMessage {
  role: 'user'
  content: string
}

interface AssistantMessage {
  role: 'assistant'
  content: string
  toolCalls?: ToolCallEvent[]
  streaming?: boolean
}

interface ToolCallEvent {
  name: string
  args: Record<string, unknown>
  result?: string
}

type Message = UserMessage | AssistantMessage

// ---------------------------------------------------------------------------
// Tool call display labels
// ---------------------------------------------------------------------------

const TOOL_LABELS: Record<string, (args: Record<string, unknown>) => string> = {
  get_chat_messages: (a) => `Reading messages in "${a.chat_name}"`,
  find_chats_by_tag: (a) => `Finding chats tagged "${a.tag_name}"`,
  search_messages: (a) => `Searching messages for "${a.keyword}"`,
  search_chats_by_title: (a) => `Searching chat titles for "${a.keyword}"`,
  tag_chat: (a) => `Tagging "${a.chat_name}" with "${a.tag_name}"`,
  find_chats_by_category: (a) => `Finding ${a.category} chats`,
}

// ---------------------------------------------------------------------------
// SSE streaming helper
// ---------------------------------------------------------------------------

async function* streamChat(
  messages: { role: string; content: string }[],
  signal: AbortSignal,
): AsyncGenerator<{ event: string; data: Record<string, unknown> }> {
  const resp = await fetch('/api/intelligence/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages }),
    signal,
  })

  if (!resp.ok || !resp.body) {
    throw new Error('Failed to connect to intelligence API')
  }

  const reader = resp.body.getReader()
  const dec = new TextDecoder()
  let buf = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += dec.decode(value, { stream: true })
      const parts = buf.split('\n\n')
      buf = parts.pop() ?? ''
      for (const part of parts) {
        const lines = part.trim().split('\n')
        let event = 'message'
        let dataLine = ''
        for (const line of lines) {
          if (line.startsWith('event: ')) event = line.slice(7)
          if (line.startsWith('data: ')) dataLine = line.slice(6)
        }
        if (dataLine) {
          try {
            yield { event, data: JSON.parse(dataLine) }
          } catch {
            // skip malformed
          }
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}

// ---------------------------------------------------------------------------
// Format date for sidebar
// ---------------------------------------------------------------------------

function fmtDate(iso: string): string {
  const d = new Date(iso)
  const now = new Date()
  const diffMs = now.getTime() - d.getTime()
  const diffDays = Math.floor(diffMs / 86400000)
  if (diffDays === 0) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (diffDays === 1) return 'Yesterday'
  if (diffDays < 7) return d.toLocaleDateString([], { weekday: 'short' })
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function ToolCallBadge({ tc }: { tc: ToolCallEvent }) {
  const [open, setOpen] = useState(false)
  const label = TOOL_LABELS[tc.name]?.(tc.args) ?? tc.name

  return (
    <div className="rounded-lg border border-violet-500/20 bg-violet-500/5 text-xs overflow-hidden">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-violet-400 hover:bg-violet-500/10 transition-colors"
      >
        {tc.result ? (
          <Wrench size={11} className="shrink-0" />
        ) : (
          <Loader2 size={11} className="shrink-0 animate-spin" />
        )}
        <span className="flex-1">{label}</span>
        {tc.result && (open ? <ChevronDown size={11} /> : <ChevronRight size={11} />)}
      </button>
      {open && tc.result && (
        <div className="border-t border-violet-500/20 px-3 py-2 text-slate-400 font-mono whitespace-pre-wrap leading-relaxed">
          {tc.result}
        </div>
      )}
    </div>
  )
}

function ToolCallGroup({ toolCalls }: { toolCalls: ToolCallEvent[] }) {
  const [open, setOpen] = useState(false)
  const allDone = toolCalls.every((tc) => tc.result !== undefined)
  const count = toolCalls.length

  return (
    <div className="rounded-lg border border-violet-500/20 bg-violet-500/5 text-xs overflow-hidden">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-violet-400 hover:bg-violet-500/10 transition-colors"
      >
        {allDone ? (
          <Wrench size={11} className="shrink-0" />
        ) : (
          <Loader2 size={11} className="shrink-0 animate-spin" />
        )}
        <span className="flex-1">
          {allDone ? `Used ${count} tools` : `Running tools\u2026`}
        </span>
        {open ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
      </button>
      {open && (
        <div className="border-t border-violet-500/20 p-2 space-y-1.5">
          {toolCalls.map((tc, i) => (
            <ToolCallBadge key={i} tc={tc} />
          ))}
        </div>
      )}
    </div>
  )
}

function MdContent({ content }: { content: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        p: ({ children }) => <p className="mb-2 last:mb-0 leading-relaxed">{children}</p>,
        h1: ({ children }) => <h1 className="mb-2 mt-3 text-base font-semibold text-slate-100">{children}</h1>,
        h2: ({ children }) => <h2 className="mb-1.5 mt-3 text-sm font-semibold text-slate-200">{children}</h2>,
        h3: ({ children }) => <h3 className="mb-1 mt-2 text-sm font-medium text-slate-300">{children}</h3>,
        ul: ({ children }) => <ul className="mb-2 ml-4 list-disc space-y-0.5 last:mb-0">{children}</ul>,
        ol: ({ children }) => <ol className="mb-2 ml-4 list-decimal space-y-0.5 last:mb-0">{children}</ol>,
        li: ({ children }) => <li className="leading-relaxed">{children}</li>,
        code: ({ children, className }) => {
          const isBlock = Boolean(className?.startsWith('language-') || String(children).includes('\n'))
          return isBlock ? (
            <code className="block overflow-x-auto rounded-lg bg-slate-800 p-3 font-mono text-xs text-slate-300 leading-relaxed">
              {children}
            </code>
          ) : (
            <code className="rounded bg-slate-600/60 px-1 py-0.5 font-mono text-xs text-indigo-300">
              {children}
            </code>
          )
        },
        pre: ({ children }) => <pre className="mb-2 last:mb-0">{children}</pre>,
        blockquote: ({ children }) => (
          <blockquote className="mb-2 border-l-2 border-indigo-500/50 pl-3 text-slate-400 italic last:mb-0">
            {children}
          </blockquote>
        ),
        a: ({ children, href }) => (
          <a href={href} target="_blank" rel="noopener noreferrer" className="text-indigo-400 underline underline-offset-2 hover:text-indigo-300">
            {children}
          </a>
        ),
        strong: ({ children }) => <strong className="font-semibold text-slate-100">{children}</strong>,
        em: ({ children }) => <em className="italic text-slate-300">{children}</em>,
        hr: () => <hr className="my-3 border-slate-600/50" />,
        table: ({ children }) => (
          <div className="mb-2 overflow-x-auto last:mb-0">
            <table className="w-full border-collapse text-xs">{children}</table>
          </div>
        ),
        th: ({ children }) => (
          <th className="border border-slate-600 bg-slate-700/60 px-2 py-1.5 text-left font-medium text-slate-200">
            {children}
          </th>
        ),
        td: ({ children }) => (
          <td className="border border-slate-700 px-2 py-1.5 text-slate-300">{children}</td>
        ),
      }}
    >
      {content}
    </ReactMarkdown>
  )
}

function MessageBubble({ msg }: { msg: Message }) {
  if (msg.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[75%] rounded-2xl rounded-tr-sm bg-indigo-600 px-4 py-2.5 text-sm text-white">
          <MdContent content={msg.content} />
        </div>
      </div>
    )
  }

  const assistant = msg as AssistantMessage
  const toolCalls = assistant.toolCalls ?? []

  return (
    <div className="flex justify-start">
      <div className="max-w-[85%] space-y-2">
        {toolCalls.length === 1 && <ToolCallBadge tc={toolCalls[0]} />}
        {toolCalls.length > 1 && <ToolCallGroup toolCalls={toolCalls} />}

        {(assistant.content || assistant.streaming) && (
          <div className="rounded-2xl rounded-tl-sm bg-slate-700 px-4 py-2.5 text-sm text-slate-100">
            <MdContent content={assistant.content} />
            {assistant.streaming && (
              <span className="ml-0.5 inline-block h-4 w-0.5 animate-pulse bg-slate-400" />
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Conversation sidebar item
// ---------------------------------------------------------------------------

interface ConvItemProps {
  conv: ConversationSummary
  active: boolean
  onSelect: () => void
  onDelete: () => void
}

function ConvItem({ conv, active, onSelect, onDelete }: ConvItemProps) {
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onSelect()
        }
      }}
      className={`group w-full flex items-start gap-2 rounded-lg px-3 py-2.5 text-left transition-colors cursor-pointer ${
        active
          ? 'bg-violet-600/20 text-slate-100'
          : 'text-slate-400 hover:bg-slate-700/50 hover:text-slate-200'
      }`}
    >
      <div className="flex-1 min-w-0">
        <p className="truncate text-xs font-medium leading-snug">
          {conv.title ?? 'New conversation'}
        </p>
        <p className="text-[10px] text-slate-500 mt-0.5">{fmtDate(conv.updated_at)}</p>
      </div>
      <button
        onClick={(e) => {
          e.stopPropagation()
          onDelete()
        }}
        className="shrink-0 mt-0.5 opacity-0 group-hover:opacity-100 text-slate-500 hover:text-red-400 transition-all"
        title="Delete conversation"
      >
        <Trash2 size={12} />
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Mentions helpers
// ---------------------------------------------------------------------------

function fmtMsgTime(iso: string): string {
  const d = new Date(iso)
  const now = new Date()
  const diffMs = now.getTime() - d.getTime()
  const diffDays = Math.floor(diffMs / 86400000)
  if (diffDays === 0) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (diffDays === 1) return 'Yesterday'
  if (diffDays < 7) return d.toLocaleDateString([], { weekday: 'short' }) + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function MentionMsgRow({
  msg,
  highlighted,
}: {
  msg: MentionMessage
  highlighted: boolean
}) {
  return (
    <div
      className={`rounded-lg px-3 py-2 text-xs leading-relaxed transition-colors ${
        highlighted
          ? 'bg-amber-500/10 border border-amber-500/30'
          : 'bg-slate-700/30'
      }`}
    >
      <div className="flex items-baseline justify-between gap-2 mb-0.5">
        <span className={`font-medium truncate ${highlighted ? 'text-amber-300' : 'text-slate-300'}`}>
          {msg.sender_name || 'Unknown'}
        </span>
        <span className="shrink-0 text-[10px] text-slate-500">{fmtMsgTime(msg.date)}</span>
      </div>
      <p className="text-slate-400 leading-snug line-clamp-3">
        {msg.text || <span className="italic text-slate-500">{msg.media_type ? `[${msg.media_type}]` : '[no text]'}</span>}
      </p>
    </div>
  )
}

// Tag color palette (same as Tags page)
const TAG_COLORS: Record<string, string> = {
  slate:  'bg-slate-500/20 text-slate-400',
  red:    'bg-red-500/20 text-red-400',
  orange: 'bg-orange-500/20 text-orange-400',
  amber:  'bg-amber-500/20 text-amber-400',
  yellow: 'bg-yellow-500/20 text-yellow-400',
  green:  'bg-green-500/20 text-green-400',
  teal:   'bg-teal-500/20 text-teal-400',
  cyan:   'bg-cyan-500/20 text-cyan-400',
  blue:   'bg-blue-500/20 text-blue-400',
  indigo: 'bg-indigo-500/20 text-indigo-400',
  violet: 'bg-violet-500/20 text-violet-400',
  purple: 'bg-purple-500/20 text-purple-400',
  pink:   'bg-pink-500/20 text-pink-400',
}

function MentionCard({
  group,
  onNavigate,
}: {
  group: MentionGroup
  onNavigate: (chatId: number, msgId: number) => void
}) {
  const [expanded, setExpanded] = useState(false)

  const chatTypeColor = {
    group: 'bg-purple-500/20 text-purple-400',
    channel: 'bg-amber-500/20 text-amber-400',
    private: 'bg-indigo-500/20 text-indigo-400',
  }[group.chat.type] ?? 'bg-slate-500/20 text-slate-400'

  const allMsgs = [
    ...group.context_before.map((m) => ({ msg: m, highlighted: false })),
    { msg: group.mention_msg, highlighted: true },
    ...group.context_after.map((m) => ({ msg: m, highlighted: false })),
  ]

  const trackedBorder = group.chat.is_tracked
    ? 'border-violet-500/40 bg-violet-900/10'
    : 'border-slate-700/60 bg-slate-800/60'

  const mentionText = group.mention_msg.text || ''
  const senderName = group.mention_msg.sender_name || 'Unknown'

  return (
    <div className={`rounded-xl border ${trackedBorder} overflow-hidden transition-colors`}>
      {/* Header row — always visible, click to expand */}
      <div
        role="button"
        tabIndex={0}
        onClick={() => setExpanded((v) => !v)}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpanded((v) => !v) } }}
        className="flex items-center gap-2 px-3 py-2.5 cursor-pointer hover:bg-slate-700/30 transition-colors select-none"
      >
        {/* Expand chevron */}
        {expanded ? <ChevronDown size={12} className="shrink-0 text-slate-500" /> : <ChevronRight size={12} className="shrink-0 text-slate-500" />}

        {/* Tracked bookmark */}
        {group.chat.is_tracked && <Bookmark size={11} className="shrink-0 text-violet-400" />}

        {/* Chat type badge */}
        <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium shrink-0 ${chatTypeColor}`}>
          {group.chat.type}
        </span>

        {/* Chat name */}
        <span className="flex-1 min-w-0 truncate text-xs font-semibold text-slate-200">{group.chat.name}</span>

        {/* Tags */}
        {group.chat.tags.slice(0, 2).map((t) => (
          <span key={t.id} className={`rounded px-1.5 py-0.5 text-[10px] font-medium shrink-0 ${TAG_COLORS[t.color] ?? TAG_COLORS.slate}`}>
            {t.name}
          </span>
        ))}

        {/* Replied badge */}
        {group.replied && (
          <span className="flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] font-medium bg-emerald-500/15 text-emerald-400 shrink-0">
            <CheckCheck size={9} />
            replied
          </span>
        )}

        {/* Mention icon + time */}
        <AtSign size={11} className="shrink-0 text-amber-400" />
        <span className="shrink-0 text-[10px] text-slate-500">{fmtMsgTime(group.mention_msg.date)}</span>
      </div>

      {/* Collapsed preview: just the mention message one-liner */}
      {!expanded && (
        <div
          role="button"
          tabIndex={0}
          onClick={() => onNavigate(group.chat.id, group.mention_msg.telegram_id)}
          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onNavigate(group.chat.id, group.mention_msg.telegram_id) } }}
          className="px-3 pb-2.5 cursor-pointer"
        >
          <p className="text-xs text-slate-500 truncate">
            <span className="text-amber-300/70 font-medium">{senderName}: </span>
            {mentionText || <span className="italic">{group.mention_msg.media_type ? `[${group.mention_msg.media_type}]` : '[no text]'}</span>}
          </p>
        </div>
      )}

      {/* Expanded: full context messages + navigate button */}
      {expanded && (
        <div className="border-t border-slate-700/50">
          <div className="px-3 py-2.5 space-y-1.5">
            {allMsgs.map(({ msg, highlighted }, i) => (
              <MentionMsgRow key={`${msg.telegram_id}-${i}`} msg={msg} highlighted={highlighted} />
            ))}
          </div>
          <div className="px-3 pb-3">
            <button
              onClick={() => onNavigate(group.chat.id, group.mention_msg.telegram_id)}
              className="w-full rounded-lg bg-slate-700 hover:bg-slate-600 px-3 py-1.5 text-xs text-slate-300 transition-colors"
            >
              Jump to message →
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Mentions tab
// ---------------------------------------------------------------------------

const TIME_OPTIONS = [
  { label: '7 days', value: 7 },
  { label: '14 days', value: 14 },
  { label: '1 month', value: 30 },
]

function MentionsTab() {
  const navigate = useNavigate()
  const [groups, setGroups] = useState<MentionGroup[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // Filters
  const [days, setDays] = useState(7)
  const [groupSearch, setGroupSearch] = useState('')
  const [tagFilter, setTagFilter] = useState('')
  const [availableTags, setAvailableTags] = useState<{ id: number; name: string }[]>([])

  // Debounced group search for fetching
  const [debouncedGroupSearch, setDebouncedGroupSearch] = useState('')
  useEffect(() => {
    const t = setTimeout(() => setDebouncedGroupSearch(groupSearch), 350)
    return () => clearTimeout(t)
  }, [groupSearch])

  const fetchMentions = useCallback(async (opts?: { days?: number; groupName?: string; tagName?: string }) => {
    setLoading(true)
    setError('')
    try {
      const data = await getMentions(opts)
      setGroups(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load mentions')
    } finally {
      setLoading(false)
    }
  }, [])

  // Fetch tags once on mount
  useEffect(() => {
    getTags().then(setAvailableTags).catch(() => {})
  }, [])

  // Re-fetch when filters change
  useEffect(() => {
    fetchMentions({ days, groupName: debouncedGroupSearch, tagName: tagFilter })
  }, [fetchMentions, days, debouncedGroupSearch, tagFilter])

  const handleNavigate = (chatId: number, msgId: number) => {
    navigate(`/chats?chatId=${chatId}&msgId=${msgId}`)
  }

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      {/* Header */}
      <div className="border-b border-slate-700/50 px-4 pt-3 pb-0">
        <div className="flex items-center justify-between mb-2">
          <div>
            <p className="text-sm font-semibold text-slate-100">Mentions</p>
            <p className="text-xs text-slate-500">Messages where you were tagged</p>
          </div>
          <button
            onClick={() => fetchMentions({ days, groupName: debouncedGroupSearch, tagName: tagFilter })}
            disabled={loading}
            className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs text-slate-400 hover:bg-slate-700 disabled:opacity-50 transition-colors"
            title="Refresh mentions"
          >
            <RefreshCw size={13} className={loading ? 'animate-spin' : ''} />
            Refresh
          </button>
        </div>

        {/* Filter bar */}
        <div className="flex items-center gap-2 pb-3 flex-wrap">
          {/* Group name search */}
          <div className="relative flex-1 min-w-32">
            <Search size={11} className="absolute left-2 top-1/2 -translate-y-1/2 text-slate-500 pointer-events-none" />
            <input
              type="text"
              value={groupSearch}
              onChange={(e) => setGroupSearch(e.target.value)}
              placeholder="Filter by group…"
              className="w-full rounded-lg border border-slate-700 bg-slate-800 pl-6 pr-2 py-1 text-xs text-slate-200 placeholder-slate-500 outline-none focus:border-amber-500/50"
            />
          </div>

          {/* Tag filter */}
          <div className="relative">
            <Tag size={11} className="absolute left-2 top-1/2 -translate-y-1/2 text-slate-500 pointer-events-none" />
            <select
              value={tagFilter}
              onChange={(e) => setTagFilter(e.target.value)}
              className="appearance-none rounded-lg border border-slate-700 bg-slate-800 pl-6 pr-3 py-1 text-xs text-slate-200 outline-none focus:border-amber-500/50 cursor-pointer"
            >
              <option value="">All tags</option>
              {availableTags.map((t) => (
                <option key={t.id} value={t.name}>{t.name}</option>
              ))}
            </select>
          </div>

          {/* Time window */}
          <div className="flex rounded-lg border border-slate-700 overflow-hidden">
            {TIME_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                onClick={() => setDays(opt.value)}
                className={`px-2.5 py-1 text-xs transition-colors ${
                  days === opt.value
                    ? 'bg-amber-600/80 text-white'
                    : 'text-slate-400 hover:bg-slate-700'
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto px-4 py-4">
        {loading && (
          <div className="flex items-center justify-center py-16 gap-2 text-slate-500">
            <Loader2 size={16} className="animate-spin" />
            <span className="text-sm">Loading mentions…</span>
          </div>
        )}

        {!loading && error && (
          <div className="flex items-center gap-2 rounded-lg bg-red-900/20 border border-red-700/30 px-3 py-2 text-xs text-red-400">
            <AlertCircle size={13} />
            {error}
          </div>
        )}

        {!loading && !error && groups.length === 0 && (
          <div className="flex flex-col items-center justify-center py-16 gap-3 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-amber-500/10 text-amber-400">
              <AtSign size={24} />
            </div>
            <div>
              <p className="text-sm font-medium text-slate-300">No mentions found</p>
              <p className="mt-1 text-xs text-slate-500">
                Try adjusting the time window or filters.
              </p>
            </div>
          </div>
        )}

        {!loading && !error && groups.length > 0 && (
          <div className="space-y-2">
            {groups.map((group, i) => (
              <MentionCard
                key={`${group.chat.id}-${group.mention_msg.telegram_id}-${i}`}
                group={group}
                onNavigate={handleNavigate}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Briefing tab
// ---------------------------------------------------------------------------

async function* streamBriefing(days: number, signal: AbortSignal): AsyncGenerator<Record<string, unknown>> {
  const res = await fetch(`/api/intelligence/briefing?days=${days}`, { signal })
  if (!res.ok || !res.body) throw new Error('Briefing stream unavailable')
  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += dec.decode(value, { stream: true })
      const parts = buf.split('\n\n')
      buf = parts.pop() ?? ''
      for (const part of parts) {
        const lines = part.trim().split('\n')
        let evt = ''
        let data = ''
        for (const l of lines) {
          if (l.startsWith('event: ')) evt = l.slice(7)
          if (l.startsWith('data: ')) data = l.slice(6)
        }
        if (data) {
          try {
            yield { _event: evt, ...(JSON.parse(data) as Record<string, unknown>) }
          } catch { /* skip */ }
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}

/** Check if briefing should auto-run based on prefs */
function shouldAutoRun(schedule: string, lastRun: string | null): boolean {
  const now = new Date()
  const nineAM = new Date(now)
  nineAM.setHours(9, 0, 0, 0)

  if (!lastRun) return true // Never run

  const last = new Date(lastRun)

  if (schedule === 'weekly') {
    // Run if last run was before the start of this week (Monday)
    const monday = new Date(now)
    monday.setHours(0, 0, 0, 0)
    const day = monday.getDay() // 0=Sun
    monday.setDate(monday.getDate() - (day === 0 ? 6 : day - 1))
    return last < monday
  }

  // Daily: run if it's past 9am and last run was before 9am today
  if (now >= nineAM && last < nineAM) return true
  // Or if last run was yesterday or earlier
  const todayMidnight = new Date(now)
  todayMidnight.setHours(0, 0, 0, 0)
  return last < todayMidnight
}

function BriefingTab() {
  const [summaries, setSummaries] = useState<BriefingSummary[]>([])
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState<{ current: number; total: number; chatName: string } | null>(null)
  const [error, setError] = useState('')
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [schedule, setSchedule] = useState<'daily' | 'weekly'>('daily')
  const abortRef = useRef<AbortController | null>(null)
  const runningRef = useRef(false)

  const days = schedule === 'weekly' ? 7 : 1

  const runBriefing = useCallback(async () => {
    if (runningRef.current) return
    runningRef.current = true
    abortRef.current?.abort()
    abortRef.current = new AbortController()
    setRunning(true)
    setError('')
    setProgress(null)
    setSummaries([])
    try {
      for await (const evt of streamBriefing(days, abortRef.current.signal)) {
        const type = evt._event as string
        if (type === 'start') {
          setProgress({ current: 0, total: evt.total as number, chatName: '' })
        } else if (type === 'progress') {
          setProgress((p) => ({ current: evt.current as number, total: p?.total ?? 0, chatName: evt.chat_name as string }))
        } else if (type === 'chat_done') {
          setSummaries((prev) => [...prev, evt as unknown as BriefingSummary])
        } else if (type === 'done') {
          setLastUpdated(new Date())
          setBriefingLastRun().catch(() => {})
        } else if (type === 'error') {
          setError((evt.message as string) ?? 'Unknown error')
        }
      }
    } catch (e: unknown) {
      const err = e as Error
      if (err?.name !== 'AbortError') setError('Briefing failed: ' + err?.message)
    } finally {
      runningRef.current = false
      setRunning(false)
      setProgress(null)
    }
  }, [days])

  // Load schedule pref and check auto-run on mount
  useEffect(() => {
    getPrefs().then((prefs: Record<string, string>) => {
      const sched = (prefs.briefing_schedule as 'daily' | 'weekly') ?? 'daily'
      setSchedule(sched)
      if (shouldAutoRun(sched, prefs.briefing_last_run ?? null)) {
        // slight delay so UI is ready
        setTimeout(() => runBriefing(), 500)
      }
    }).catch(() => {})
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const trackedSummaries = summaries.filter((s) => s.category === 'tracked')
  const briefingSummaries = summaries.filter((s) => s.category === 'briefing')

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      {/* Header bar */}
      <div className="flex shrink-0 items-center justify-between border-b border-slate-700/50 bg-slate-800/40 px-4 py-2.5">
        <div className="flex items-center gap-2">
          <Newspaper size={14} className="text-emerald-400" />
          <span className="text-xs font-medium text-slate-300">
            {schedule === 'weekly' ? 'Weekly' : 'Daily'} Briefing
          </span>
          {lastUpdated && (
            <span className="text-xs text-slate-500">
              · Updated {lastUpdated.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
            </span>
          )}
        </div>
        <button
          onClick={runBriefing}
          disabled={running}
          className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium text-emerald-400 hover:bg-emerald-900/30 hover:text-emerald-300 disabled:opacity-40 transition-colors border border-emerald-600/30"
        >
          <RotateCcw size={12} className={running ? 'animate-spin' : ''} />
          {running ? 'Updating…' : 'Update Now'}
        </button>
      </div>

      {/* Progress bar */}
      {running && progress && (
        <div className="shrink-0 px-4 py-2 bg-slate-800/60 border-b border-slate-700/50">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-xs text-slate-400">
              Summarising <span className="text-slate-200 font-medium">{progress.chatName}</span>…
            </span>
            <span className="text-xs text-slate-500">{progress.current}/{progress.total}</span>
          </div>
          <div className="h-1 w-full rounded-full bg-slate-700">
            <div
              className="h-1 rounded-full bg-emerald-500 transition-all duration-300"
              style={{ width: progress.total > 0 ? `${(progress.current / progress.total) * 100}%` : '0%' }}
            />
          </div>
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="shrink-0 mx-4 mt-3 flex items-center gap-2 rounded-lg border border-rose-700/40 bg-rose-900/20 px-3 py-2 text-xs text-rose-400">
          <AlertCircle size={13} />
          {error}
        </div>
      )}

      {/* Empty state */}
      {!running && summaries.length === 0 && !error && (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 text-slate-500">
          <Newspaper size={32} className="opacity-30" />
          <p className="text-sm">No summaries yet. Click "Update Now" to generate briefings.</p>
        </div>
      )}

      {/* Summaries */}
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-6">
        {trackedSummaries.length > 0 && (
          <section>
            <div className="mb-3 flex items-center gap-2">
              <Radio size={13} className="text-violet-400" />
              <h3 className="text-xs font-semibold uppercase tracking-wider text-violet-400">
                Tracked Chats
              </h3>
              <span className="rounded-full bg-violet-500/20 px-1.5 py-0.5 text-[10px] text-violet-400">
                {trackedSummaries.length}
              </span>
            </div>
            <div className="space-y-2">
              {trackedSummaries.map((s) => (
                <div
                  key={s.chat_id}
                  className="rounded-xl border border-violet-500/20 bg-violet-900/10 px-4 py-3"
                >
                  <p className="mb-1 text-xs font-medium text-violet-300">{s.chat_name}</p>
                  <p className="text-xs leading-relaxed text-slate-300">{s.summary}</p>
                </div>
              ))}
            </div>
          </section>
        )}

        {briefingSummaries.length > 0 && (
          <section>
            <div className="mb-3 flex items-center gap-2">
              <Newspaper size={13} className="text-emerald-400" />
              <h3 className="text-xs font-semibold uppercase tracking-wider text-emerald-400">
                Briefing Chats
              </h3>
              <span className="rounded-full bg-emerald-500/20 px-1.5 py-0.5 text-[10px] text-emerald-400">
                {briefingSummaries.length}
              </span>
            </div>
            <div className="space-y-3">
              {briefingSummaries.map((s) => (
                <div
                  key={s.chat_id}
                  className="rounded-xl border border-emerald-500/20 bg-emerald-900/10 px-4 py-3"
                >
                  <p className="mb-2 text-xs font-medium text-emerald-300">{s.chat_name}</p>
                  <div className="text-xs leading-relaxed text-slate-300 whitespace-pre-line">
                    {s.summary}
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function Intelligence() {
  // Top-level tab
  const [activeTab, setActiveTab] = useState<'chat' | 'mentions' | 'briefing'>('chat')

  // Sidebar state
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [convList, setConvList] = useState<ConversationSummary[]>([])
  const [activeConvId, setActiveConvId] = useState<number | null>(null)
  const [loadingConvs, setLoadingConvs] = useState(true)

  // Chat state
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')

  // Title editing state
  const [editingTitle, setEditingTitle] = useState(false)
  const [titleDraft, setTitleDraft] = useState('')

  const abortRef = useRef<AbortController | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const titleInputRef = useRef<HTMLInputElement>(null)

  // Whether a title has been auto-suggested for the current conversation
  const suggestedRef = useRef(false)

  // ---------------------------------------------------------------------------
  // Load conversation list on mount
  // ---------------------------------------------------------------------------

  const refreshConvList = useCallback(async () => {
    try {
      const list = await listConversations()
      setConvList(list)
      return list
    } catch {
      return []
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      setLoadingConvs(true)
      const list = await refreshConvList()
      if (cancelled) return
      if (list.length === 0) {
        // Auto-create a first conversation
        try {
          const conv = await createConversation()
          setConvList([
            {
              id: conv.id,
              title: conv.title,
              created_at: conv.created_at,
              updated_at: conv.updated_at,
              message_count: 0,
            },
          ])
          setActiveConvId(conv.id)
          setMessages([])
          suggestedRef.current = false
        } catch {
          // ignore
        }
      } else {
        // Load the most recent conversation
        const first = list[0]
        setActiveConvId(first.id)
        suggestedRef.current = first.title !== null
        try {
          const detail = await getConversation(first.id)
          if (!cancelled) {
            setMessages(
              detail.messages.map((m) => ({
                role: m.role as 'user' | 'assistant',
                content: m.content,
              }))
            )
          }
        } catch {
          // ignore
        }
      }
      if (!cancelled) setLoadingConvs(false)
    })()
    return () => {
      cancelled = true
    }
  }, [refreshConvList])

  // ---------------------------------------------------------------------------
  // Scroll to bottom on new messages
  // ---------------------------------------------------------------------------

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // ---------------------------------------------------------------------------
  // Focus title input when editing starts
  // ---------------------------------------------------------------------------

  useEffect(() => {
    if (editingTitle) titleInputRef.current?.focus()
  }, [editingTitle])

  // ---------------------------------------------------------------------------
  // Select a conversation
  // ---------------------------------------------------------------------------

  const selectConv = useCallback(
    async (id: number) => {
      if (id === activeConvId) return
      // Abort any running stream
      if (abortRef.current) {
        abortRef.current.abort()
        abortRef.current = null
      }
      setRunning(false)
      setError('')
      setActiveConvId(id)
      setEditingTitle(false)
      const conv = convList.find((c) => c.id === id)
      suggestedRef.current = conv?.title !== null
      setMessages([])
      try {
        const detail = await getConversation(id)
        setMessages(
          detail.messages.map((m) => ({
            role: m.role as 'user' | 'assistant',
            content: m.content,
          }))
        )
      } catch {
        // ignore
      }
    },
    [activeConvId, convList]
  )

  // ---------------------------------------------------------------------------
  // Create new conversation
  // ---------------------------------------------------------------------------

  const newConv = useCallback(async () => {
    if (abortRef.current) {
      abortRef.current.abort()
      abortRef.current = null
    }
    setRunning(false)
    setError('')
    try {
      const conv = await createConversation()
      const summary: ConversationSummary = {
        id: conv.id,
        title: conv.title,
        created_at: conv.created_at,
        updated_at: conv.updated_at,
        message_count: 0,
      }
      setConvList((prev) => [summary, ...prev])
      setActiveConvId(conv.id)
      setMessages([])
      setEditingTitle(false)
      suggestedRef.current = false
    } catch {
      // ignore
    }
  }, [])

  // ---------------------------------------------------------------------------
  // Delete conversation
  // ---------------------------------------------------------------------------

  const deleteConv = useCallback(
    async (id: number) => {
      try {
        await deleteConversation(id)
        const next = convList.filter((c) => c.id !== id)
        if (id === activeConvId) {
          // Switch to next available conversation
          if (next.length > 0) {
            setConvList(next)
            await selectConv(next[0].id)
          } else {
            // Create a new one
            setConvList([])
            const conv = await createConversation()
            const summary: ConversationSummary = {
              id: conv.id,
              title: conv.title,
              created_at: conv.created_at,
              updated_at: conv.updated_at,
              message_count: 0,
            }
            setConvList([summary])
            setActiveConvId(conv.id)
            setMessages([])
            suggestedRef.current = false
          }
        } else {
          setConvList(next)
        }
      } catch {
        // ignore
      }
    },
    [activeConvId, convList, selectConv]
  )

  // ---------------------------------------------------------------------------
  // Title editing
  // ---------------------------------------------------------------------------

  const activeConv = convList.find((c) => c.id === activeConvId)

  const startEditTitle = () => {
    setTitleDraft(activeConv?.title ?? '')
    setEditingTitle(true)
  }

  const commitTitle = async () => {
    const trimmed = titleDraft.trim()
    if (!trimmed || !activeConvId) {
      setEditingTitle(false)
      return
    }
    setEditingTitle(false)
    try {
      await updateConversationTitle(activeConvId, trimmed)
      setConvList((prev) =>
        prev.map((c) =>
          c.id === activeConvId ? { ...c, title: trimmed } : c
        )
      )
      suggestedRef.current = true
    } catch {
      // ignore
    }
  }

  const cancelEditTitle = () => setEditingTitle(false)

  // ---------------------------------------------------------------------------
  // Send message
  // ---------------------------------------------------------------------------

  const send = async () => {
    const text = input.trim()
    if (!text || running || !activeConvId) return

    setInput('')
    setError('')

    const userMsg: UserMessage = { role: 'user', content: text }
    const history = [...messages, userMsg]
    setMessages(history)

    // Persist user message
    try {
      await appendConversationMessage(activeConvId, 'user', text)
    } catch {
      // ignore persistence errors — chat still works
    }

    // Placeholder assistant message
    const assistantMsg: AssistantMessage = {
      role: 'assistant',
      content: '',
      toolCalls: [],
      streaming: true,
    }
    setMessages([...history, assistantMsg])
    setRunning(true)

    const ctrl = new AbortController()
    abortRef.current = ctrl

    // Build message array for API (only role+content)
    const apiMessages = history.map((m) => ({ role: m.role, content: m.content }))

    let finalContent = ''

    try {
      for await (const { event, data } of streamChat(apiMessages, ctrl.signal)) {
        if (event === 'tool_call') {
          setMessages((prev) => {
            const next = [...prev]
            const last = { ...(next[next.length - 1] as AssistantMessage) }
            last.toolCalls = [
              ...(last.toolCalls ?? []),
              { name: data.name as string, args: (data.args as Record<string, unknown>) ?? {} },
            ]
            next[next.length - 1] = last
            return next
          })
        } else if (event === 'tool_result') {
          setMessages((prev) => {
            const next = [...prev]
            const last = { ...(next[next.length - 1] as AssistantMessage) }
            last.toolCalls = (last.toolCalls ?? []).map((tc) =>
              tc.name === (data.name as string) && !tc.result
                ? { ...tc, result: data.content as string }
                : tc,
            )
            next[next.length - 1] = last
            return next
          })
        } else if (event === 'text') {
          const chunk = data.content as string
          finalContent += chunk
          setMessages((prev) => {
            const next = [...prev]
            const last = { ...(next[next.length - 1] as AssistantMessage) }
            last.content += chunk
            next[next.length - 1] = last
            return next
          })
        } else if (event === 'done') {
          setMessages((prev) => {
            const next = [...prev]
            const last = { ...(next[next.length - 1] as AssistantMessage) }
            last.streaming = false
            next[next.length - 1] = last
            return next
          })
          break
        } else if (event === 'error') {
          setError((data.message as string) || 'Something went wrong')
          setMessages((prev) => {
            const next = [...prev]
            const last = { ...(next[next.length - 1] as AssistantMessage) }
            last.streaming = false
            next[next.length - 1] = last
            return next
          })
          break
        }
      }
    } catch (e) {
      if (!(e instanceof DOMException && e.name === 'AbortError')) {
        setError(e instanceof Error ? e.message : 'Connection failed')
      }
      setMessages((prev) => {
        const next = [...prev]
        const last = { ...(next[next.length - 1] as AssistantMessage) }
        last.streaming = false
        next[next.length - 1] = last
        return next
      })
    } finally {
      setRunning(false)
      abortRef.current = null
    }

    // Persist assistant reply
    if (finalContent) {
      try {
        await appendConversationMessage(activeConvId, 'assistant', finalContent)
      } catch {
        // ignore
      }

      // Refresh sidebar message counts
      setConvList((prev) =>
        prev.map((c) =>
          c.id === activeConvId
            ? { ...c, message_count: c.message_count + 2, updated_at: new Date().toISOString() }
            : c
        )
      )

      // Auto-suggest title after first user+assistant exchange (background)
      if (!suggestedRef.current) {
        suggestedRef.current = true
        suggestConversationTitle(activeConvId)
          .then(({ title }) => {
            if (title) {
              setConvList((prev) =>
                prev.map((c) => (c.id === activeConvId ? { ...c, title } : c))
              )
            }
          })
          .catch(() => {
            // ignore
          })
      }
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      {/* ------------------------------------------------------------------ */}
      {/* Top tab bar                                                          */}
      {/* ------------------------------------------------------------------ */}
      <div className="flex shrink-0 border-b border-slate-700/50 bg-slate-800/60 px-4 gap-1">
        <button
          onClick={() => setActiveTab('chat')}
          className={`flex items-center gap-1.5 px-3 py-2.5 text-xs font-medium border-b-2 transition-colors ${
            activeTab === 'chat'
              ? 'border-violet-500 text-violet-400'
              : 'border-transparent text-slate-500 hover:text-slate-300'
          }`}
        >
          <Brain size={13} />
          Chat
        </button>
        <button
          onClick={() => setActiveTab('mentions')}
          className={`flex items-center gap-1.5 px-3 py-2.5 text-xs font-medium border-b-2 transition-colors ${
            activeTab === 'mentions'
              ? 'border-amber-500 text-amber-400'
              : 'border-transparent text-slate-500 hover:text-slate-300'
          }`}
        >
          <AtSign size={13} />
          Mentions
        </button>
        <button
          onClick={() => setActiveTab('briefing')}
          className={`flex items-center gap-1.5 px-3 py-2.5 text-xs font-medium border-b-2 transition-colors ${
            activeTab === 'briefing'
              ? 'border-emerald-500 text-emerald-400'
              : 'border-transparent text-slate-500 hover:text-slate-300'
          }`}
        >
          <Newspaper size={13} />
          Briefing
        </button>
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Mentions tab                                                         */}
      {/* ------------------------------------------------------------------ */}
      {activeTab === 'mentions' && <MentionsTab />}

      {/* ------------------------------------------------------------------ */}
      {/* Briefing tab                                                         */}
      {/* ------------------------------------------------------------------ */}
      {activeTab === 'briefing' && <BriefingTab />}

      {/* ------------------------------------------------------------------ */}
      {/* Chat tab                                                             */}
      {/* ------------------------------------------------------------------ */}
      {activeTab === 'chat' && (
      <div className="flex flex-1 overflow-hidden">
      {/* ------------------------------------------------------------------ */}
      {/* Sidebar                                                              */}
      {/* ------------------------------------------------------------------ */}
      {sidebarOpen && (
        <div className="flex w-60 shrink-0 flex-col border-r border-slate-700/50 bg-slate-800/40">
          {/* Sidebar header */}
          <div className="flex items-center gap-2 px-3 py-3 border-b border-slate-700/50">
            <button
              onClick={newConv}
              className="flex flex-1 items-center gap-1.5 rounded-lg bg-violet-600/80 px-3 py-1.5 text-xs font-medium text-white hover:bg-violet-500 transition-colors"
            >
              <Plus size={13} />
              New chat
            </button>
            <button
              onClick={() => setSidebarOpen(false)}
              className="text-slate-500 hover:text-slate-300 transition-colors"
              title="Collapse sidebar"
            >
              <PanelLeftClose size={16} />
            </button>
          </div>

          {/* Conversation list */}
          <div className="flex-1 overflow-y-auto px-2 py-2 space-y-0.5">
            {loadingConvs && (
              <div className="flex justify-center py-6">
                <Loader2 size={16} className="animate-spin text-slate-500" />
              </div>
            )}
            {!loadingConvs && convList.length === 0 && (
              <p className="px-3 py-4 text-center text-xs text-slate-600">No conversations yet</p>
            )}
            {convList.map((conv) => (
              <ConvItem
                key={conv.id}
                conv={conv}
                active={conv.id === activeConvId}
                onSelect={() => selectConv(conv.id)}
                onDelete={() => deleteConv(conv.id)}
              />
            ))}
          </div>
        </div>
      )}

      {/* ------------------------------------------------------------------ */}
      {/* Main chat area                                                       */}
      {/* ------------------------------------------------------------------ */}
      <div className="flex flex-1 flex-col overflow-hidden">
        {/* Header */}
        <div className="flex items-center gap-3 border-b border-slate-700/50 px-4 py-3">
          {!sidebarOpen && (
            <button
              onClick={() => setSidebarOpen(true)}
              className="text-slate-500 hover:text-slate-300 transition-colors shrink-0"
              title="Open sidebar"
            >
              <PanelLeftOpen size={16} />
            </button>
          )}
          <Brain size={18} className="text-violet-400 shrink-0" />

          {/* Conversation title (editable) */}
          <div className="flex flex-1 items-center gap-1.5 min-w-0">
            {editingTitle ? (
              <>
                <input
                  ref={titleInputRef}
                  value={titleDraft}
                  onChange={(e) => setTitleDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') commitTitle()
                    if (e.key === 'Escape') cancelEditTitle()
                  }}
                  className="flex-1 min-w-0 rounded bg-slate-700 px-2 py-0.5 text-sm text-slate-100 outline-none focus:ring-1 focus:ring-violet-500/50"
                />
                <button onClick={commitTitle} className="text-emerald-400 hover:text-emerald-300">
                  <Check size={14} />
                </button>
                <button onClick={cancelEditTitle} className="text-slate-500 hover:text-slate-300">
                  <X size={14} />
                </button>
              </>
            ) : (
              <>
                <div className="min-w-0">
                  <button
                    onClick={startEditTitle}
                    className="group flex items-center gap-1 text-left"
                    title="Click to rename"
                  >
                    <span className="truncate text-sm font-semibold text-slate-100">
                      {activeConv?.title ?? 'Intelligence'}
                    </span>
                    <Pencil
                      size={11}
                      className="shrink-0 text-slate-600 group-hover:text-slate-400 transition-colors"
                    />
                  </button>
                  <p className="text-xs text-slate-500">Ask anything about your chats and messages</p>
                </div>
              </>
            )}
          </div>
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
          {messages.length === 0 && !loadingConvs && (
            <div className="flex flex-col items-center justify-center h-full gap-4 text-center">
              <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-violet-500/10 text-violet-400">
                <Brain size={28} />
              </div>
              <div>
                <p className="text-sm font-medium text-slate-300">What would you like to know?</p>
                <p className="mt-1 text-xs text-slate-500">
                  Ask about your chats, search messages, or explore topics across your conversations.
                </p>
              </div>
              <div className="flex flex-wrap justify-center gap-2 mt-2">
                {[
                  'Who have I been talking to about fundraising?',
                  'What are my tracked chats?',
                  'Show me recent messages from my Work chats',
                  'Search for messages about the project deadline',
                ].map((s) => (
                  <button
                    key={s}
                    onClick={() => {
                      setInput(s)
                      textareaRef.current?.focus()
                    }}
                    className="rounded-full border border-slate-600 bg-slate-800 px-3 py-1.5 text-xs text-slate-400 hover:border-slate-500 hover:text-slate-300 transition-colors"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map((msg, i) => (
            <MessageBubble key={i} msg={msg} />
          ))}

          {error && (
            <div className="flex items-center gap-2 rounded-lg bg-red-900/20 border border-red-700/30 px-3 py-2 text-xs text-red-400">
              <AlertCircle size={13} />
              {error}
            </div>
          )}

          <div ref={bottomRef} />
        </div>

        {/* Input */}
        <div className="border-t border-slate-700/50 px-4 py-3">
          <div className="flex items-end gap-2 rounded-xl border border-slate-600 bg-slate-800 px-3 py-2 focus-within:border-violet-500/50 focus-within:ring-1 focus-within:ring-violet-500/20 transition-all">
            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask about your chats\u2026"
              rows={1}
              disabled={running || !activeConvId}
              className="flex-1 resize-none bg-transparent text-sm text-slate-100 placeholder-slate-500 outline-none disabled:opacity-50"
              style={{ maxHeight: '120px', overflowY: 'auto' }}
            />
            <button
              onClick={send}
              disabled={!input.trim() || running || !activeConvId}
              className="mb-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-violet-600 text-white hover:bg-violet-500 disabled:opacity-40 transition-colors"
            >
              {running ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
            </button>
          </div>
          <p className="mt-1.5 text-center text-[10px] text-slate-600">
            Enter to send &middot; Shift+Enter for new line
          </p>
        </div>
      </div>
      </div>
      )}
    </div>
  )
}
