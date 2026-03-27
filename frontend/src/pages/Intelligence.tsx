import { useState, useRef, useEffect, useCallback, useMemo } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useWebSocket } from '../hooks/useWebSocket'
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
  Star,
} from 'lucide-react'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import {
  reactToMessage,
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
  getBriefingHistory,
  deleteBriefingRun,
  setBriefingDay,
  getBriefingPrompts,
  setBriefingPrompts,
  getBriefingChats,
  setBriefing,
} from '../api/client'
import type { Chat, ConversationSummary, MentionGroup, MentionMessage, BriefingHistoryEntry, BriefingSummary, BriefingRun, BriefingSource, WSEvent } from '../api/types'
import { getSessionToken, authenticatedMediaUrl, notifySessionExpired } from '../api/session'

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
  conversationId: number,
  message: string,
  signal: AbortSignal,
): AsyncGenerator<{ event: string; data: Record<string, unknown> }> {
  // Send only the conversation_id + new message; backend loads full history from DB.
  // This ensures context is always the full persisted conversation, isolated per chat.
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = getSessionToken()
  if (token) headers['X-Session-Token'] = token
  const resp = await fetch('/api/intelligence/chat', {
    method: 'POST',
    headers,
    body: JSON.stringify({ conversation_id: conversationId, message }),
    signal,
  })

  if (resp.status === 401) {
    notifySessionExpired()
    throw new Error('Session expired')
  }
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
  const hasPhoto = msg.media_url && msg.media_type === 'photo'
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
      {msg.text && (
        <p className="text-slate-400 leading-snug line-clamp-3">{msg.text}</p>
      )}
      {hasPhoto && (
        <img
          src={authenticatedMediaUrl(msg.media_url!)}
          alt="photo"
          className="mt-1 rounded-md object-cover"
          style={{ maxWidth: 220, maxHeight: 180 }}
        />
      )}
      {msg.media_type && !hasPhoto && !msg.text && (
        <span className="italic text-slate-500">[{msg.media_type}]</span>
      )}
      {!msg.text && !msg.media_type && (
        <span className="italic text-slate-500">[no text]</span>
      )}
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
  selected = false,
  onToggleExpand,
}: {
  group: MentionGroup
  onNavigate: (chatId: number, msgId: number) => void
  selected?: boolean
  onToggleExpand?: (expanded: boolean) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const handleToggle = useCallback(() => {
    const next = !expanded
    setExpanded(next)
    onToggleExpand?.(next)
  }, [expanded, onToggleExpand])

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
    <div className={`rounded-xl border ${trackedBorder} overflow-hidden transition-colors${selected ? ' ring-1 ring-amber-500/50' : ''}`}>
      {/* Header row — always visible, click to expand */}
      <div
        role="button"
        tabIndex={0}
        onClick={handleToggle}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleToggle() } }}
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

        {/* Acknowledged badge (user reacted to the mention) */}
        {group.mention_msg.my_reaction && !group.replied && (
          <span className="flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] font-medium bg-amber-500/15 text-amber-400 shrink-0">
            <span className="text-[10px] leading-none">{group.mention_msg.my_reaction}</span>
            acknowledged
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
          onClick={() => onNavigate(group.chat.id, group.mention_msg.tg_message_id)}
          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onNavigate(group.chat.id, group.mention_msg.tg_message_id) } }}
          className="px-3 pb-2.5 cursor-pointer"
        >
          <p className="text-xs text-slate-500 truncate">
            <span className="text-amber-300/70 font-medium">{senderName}: </span>
            {mentionText || <span className="italic">{group.mention_msg.media_type ? `[${group.mention_msg.media_type}]` : '[no text]'}</span>}
          </p>
        </div>
      )}

      {/* Expanded: full context messages + optional reply section + navigate buttons */}
      {expanded && (
        <div className="border-t border-slate-700/50">
          {/* Context messages (before, mention, after) */}
          <div className="px-3 py-2.5 space-y-1.5">
            {allMsgs.map(({ msg, highlighted }, i) => (
              <MentionMsgRow key={`${msg.tg_message_id}-${i}`} msg={msg} highlighted={highlighted} />
            ))}
          </div>

          {/* User reply section */}
          {group.user_reply_msg && (
            <div className="px-3 pb-2.5 space-y-1.5">
              {/* [...] gap indicator */}
              {group.reply_has_gap && (
                <div className="text-center text-[11px] text-slate-500 py-0.5 select-none">
                  <span className="rounded bg-slate-700/40 px-2 py-0.5">···</span>
                </div>
              )}
              {/* The user's reply */}
              <div className="relative">
                <MentionMsgRow msg={group.user_reply_msg} highlighted={false} />
                {/* Jump-to-reply button overlaid on the message */}
                <button
                  onClick={() => onNavigate(group.chat.id, group.user_reply_msg!.tg_message_id)}
                  className="absolute right-1 top-1 rounded px-1.5 py-0.5 text-[10px] text-indigo-400 hover:bg-indigo-900/30 hover:text-indigo-300 transition-colors"
                  title="Jump to your reply in chat"
                >
                  → jump
                </button>
              </div>
            </div>
          )}

          {/* Jump to mention button */}
          <div className="px-3 pb-3">
            <button
              onClick={() => onNavigate(group.chat.id, group.mention_msg.tg_message_id)}
              className="w-full rounded-lg bg-slate-700 hover:bg-slate-600 px-3 py-1.5 text-xs text-slate-300 transition-colors"
            >
              Jump to mention →
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

type ActionFilter = 'all' | 'not_acted' | 'acknowledged' | 'replied'

const ACTION_FILTER_OPTIONS: { label: string; value: ActionFilter }[] = [
  { label: 'All', value: 'all' },
  { label: 'Not acted', value: 'not_acted' },
  { label: 'Acknowledged', value: 'acknowledged' },
  { label: 'Replied', value: 'replied' },
]

function MentionsTab() {
  const navigate = useNavigate()
  const [groups, setGroups] = useState<MentionGroup[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selectedIdx, setSelectedIdx] = useState(0)
  const selectedCardRef = useRef<HTMLDivElement | null>(null)

  // Filters
  const [days, setDays] = useState(7)
  const [groupSearch, setGroupSearch] = useState('')
  const [tagFilter, setTagFilter] = useState('')
  const [actionFilter, setActionFilter] = useState<ActionFilter>('all')
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

  // Re-fetch when server-side filters change
  useEffect(() => {
    fetchMentions({ days, groupName: debouncedGroupSearch, tagName: tagFilter })
  }, [fetchMentions, days, debouncedGroupSearch, tagFilter])

  // Live update: re-fetch when a new incoming message arrives (potential mention)
  useWebSocket((evt: WSEvent) => {
    if (evt.type === 'new_message' && !evt.is_outgoing) {
      // Re-fetch after a short delay so the backend has processed the message
      setTimeout(() => {
        fetchMentions({ days, groupName: debouncedGroupSearch, tagName: tagFilter })
      }, 1500)
    }
    if (evt.type === 'bulk_sync_complete' || evt.type === 'chat_synced') {
      fetchMentions({ days, groupName: debouncedGroupSearch, tagName: tagFilter })
    }
  })

  const handleNavigate = (chatId: number, msgId: number) => {
    navigate(`/chats?chatId=${chatId}&msgId=${msgId}`)
  }

  // Client-side action filter applied on top of server results
  const visibleGroups = groups.filter((g) => {
    const isReplied = g.replied
    const isAcknowledged = !!g.mention_msg.my_reaction && !isReplied
    if (actionFilter === 'replied') return isReplied
    if (actionFilter === 'acknowledged') return isAcknowledged
    if (actionFilter === 'not_acted') return !isReplied && !isAcknowledged
    return true
  })

  // Keep selectedIdx in bounds when list changes
  useEffect(() => {
    if (visibleGroups.length > 0) setSelectedIdx((i) => Math.min(i, visibleGroups.length - 1))
  }, [visibleGroups.length])

  // Scroll selected card into view
  useEffect(() => {
    selectedCardRef.current?.scrollIntoView({ block: 'nearest' })
  }, [selectedIdx])

  // Keyboard shortcuts for mentions navigation
  const mentionShortcuts = useMemo(() => ({
    'j': () => setSelectedIdx((i) => Math.min(i + 1, visibleGroups.length - 1)),
    'arrowdown': () => setSelectedIdx((i) => Math.min(i + 1, visibleGroups.length - 1)),
    'k': () => setSelectedIdx((i) => Math.max(i - 1, 0)),
    'arrowup': () => setSelectedIdx((i) => Math.max(i - 1, 0)),
    'a': () => {
      const g = visibleGroups[selectedIdx]
      if (!g) return
      const emoji = g.mention_msg.my_reaction ? null : '👀'
      reactToMessage(g.chat.telegram_id, g.mention_msg.tg_message_id, emoji)
        .then(() => {
          setGroups((prev) => prev.map((pg) =>
            pg.mention_msg.tg_message_id === g.mention_msg.tg_message_id
              ? { ...pg, mention_msg: { ...pg.mention_msg, my_reaction: emoji } }
              : pg
          ))
        })
        .catch(() => {})
    },
    'r': () => {
      const g = visibleGroups[selectedIdx]
      if (g) navigate(`/chats?chatId=${g.chat.telegram_id}&msgId=${g.mention_msg.tg_message_id}`)
    },
  }), [visibleGroups, selectedIdx, navigate])

  useKeyboardShortcuts(mentionShortcuts)

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

        {/* Action filter row */}
        <div className="flex items-center gap-1.5 pb-3">
          {ACTION_FILTER_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              onClick={() => setActionFilter(opt.value)}
              className={`rounded-full px-2.5 py-0.5 text-[11px] font-medium transition-colors ${
                actionFilter === opt.value
                  ? opt.value === 'not_acted'
                    ? 'bg-slate-500/30 text-slate-200'
                    : opt.value === 'acknowledged'
                      ? 'bg-amber-500/25 text-amber-300'
                      : opt.value === 'replied'
                        ? 'bg-emerald-500/25 text-emerald-300'
                        : 'bg-slate-600 text-slate-200'
                  : 'text-slate-500 hover:text-slate-300 hover:bg-slate-700/50'
              }`}
            >
              {opt.label}
            </button>
          ))}
          {groups.length > 0 && (
            <span className="ml-auto text-[10px] text-slate-500">{visibleGroups.length} / {groups.length}</span>
          )}
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

        {!loading && !error && visibleGroups.length === 0 && (
          <div className="flex flex-col items-center justify-center py-16 gap-3 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-amber-500/10 text-amber-400">
              <AtSign size={24} />
            </div>
            <div>
              <p className="text-sm font-medium text-slate-300">No mentions found</p>
              <p className="mt-1 text-xs text-slate-500">
                {actionFilter !== 'all'
                  ? `No ${actionFilter.replace('_', ' ')} mentions. Try a different filter.`
                  : 'Try adjusting the time window or filters.'}
              </p>
            </div>
          </div>
        )}

        {!loading && !error && visibleGroups.length > 0 && (
          <div className="space-y-2">
            {visibleGroups.map((group, i) => (
              <div key={`${group.chat.id}-${group.mention_msg.tg_message_id}-${i}`} ref={i === selectedIdx ? selectedCardRef : null}>
                <MentionCard
                  group={group}
                  onNavigate={handleNavigate}
                  selected={i === selectedIdx}
                  onToggleExpand={() => setSelectedIdx(i)}
                />
              </div>
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
  const headers: Record<string, string> = {}
  const bToken = getSessionToken()
  if (bToken) headers['X-Session-Token'] = bToken
  const res = await fetch(`/api/intelligence/briefing?days=${days}`, { headers, signal })
  if (res.status === 401) {
    notifySessionExpired()
    throw new Error('Session expired')
  }
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

async function* streamRegenerateBriefingEntry(
  runId: number,
  entryId: number,
  signal: AbortSignal,
): AsyncGenerator<Record<string, unknown>> {
  const regenHeaders: Record<string, string> = {}
  const rToken = getSessionToken()
  if (rToken) regenHeaders['X-Session-Token'] = rToken
  const res = await fetch(`/api/intelligence/briefing/history/${runId}/entries/${entryId}/regenerate`, {
    method: 'POST',
    headers: regenHeaders,
    signal,
  })
  if (res.status === 401) {
    notifySessionExpired()
    throw new Error('Session expired')
  }
  if (!res.ok || !res.body) throw new Error('Regeneration stream unavailable')
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const blocks = buffer.split('\n\n')
      buffer = blocks.pop() ?? ''
      for (const block of blocks) {
        const lines = block.trim().split('\n')
        let evt = ''
        let data = ''
        for (const l of lines) {
          if (l.startsWith('event: ')) evt = l.slice(7)
          if (l.startsWith('data: ')) data = l.slice(6)
        }
        if (!data) continue
        try {
          yield { _event: evt, ...(JSON.parse(data) as Record<string, unknown>) }
        } catch {
          // ignore malformed chunks
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}

/** Normalise summary text before rendering:
 *  - Expand comma-list citations [1, 2, 3] → [1][2][3]
 *  - Strip raw markdown links [[label]](url) → label
 *  - Ensure every bullet point (•) starts on its own line
 */
function normaliseSummary(text: string): string {
  // Expand [1, 2, 3] → [1][2][3]
  text = text.replace(/\[(\d+(?:,\s*\d+)+)\]/g, (_match, inner: string) =>
    inner.split(',').map((n) => `[${n.trim()}]`).join('')
  )
  // Strip markdown links [[label]](url) or [label](url) — keep just label
  text = text.replace(/\[{1,2}([^\]]+)\]{1,2}\([^)]+\)/g, '$1')
  // Ensure every • bullet starts on its own line (LLM sometimes omits \n between them)
  text = text.replace(/([^\n])\s*•/g, '$1\n•')
  return text.trim()
}

// Renders summary text with inline [1][2] citation markers as clickable badges
function SummaryWithSources({
  summary,
  sources,
  onNavigate,
  className,
}: {
  summary: string
  sources?: BriefingSource[]
  onNavigate: (chatId: number, msgId: number) => void
  className?: string
}) {
  const normalised = normaliseSummary(summary)
  // Split on [n] tokens, keeping them as separate parts
  const parts = normalised.split(/(\[\d+\])/g)
  return (
    <span className={className}>
      {parts.map((part, i) => {
        const match = part.match(/^\[(\d+)\]$/)
        if (match) {
          const idx = parseInt(match[1], 10) - 1
          const src = sources?.[idx]
          if (src) {
            return (
              <button
                key={i}
                onClick={() => onNavigate(src.chat_id, src.telegram_id)}
                className="inline-flex items-center mx-0.5 rounded px-1 py-0 text-[10px] font-medium text-indigo-400 bg-indigo-500/15 border border-indigo-500/25 hover:bg-indigo-500/30 transition-colors align-baseline leading-none"
                title="Jump to source message"
              >
                {part}
              </button>
            )
          }
          // Citation number out of range — render as plain text
          return <span key={i} className="text-slate-500">{part}</span>
        }
        return <span key={i}>{part}</span>
      })}
    </span>
  )
}

// Sub-component: renders the chat summaries within a briefing run
function BriefingRunSummaries({
  entries,
  onRegenerateEntry,
  onStopRegenerate,
  onToggleRegenerateFlow,
  regenerationFlowOpenByEntry,
  regenerationReasoningByEntry,
  regenerationTokensByEntry,
  regeneratingEntryId,
}: {
  entries: (BriefingSummary | BriefingHistoryEntry)[]
  onRegenerateEntry?: (entry: BriefingHistoryEntry) => void
  onStopRegenerate?: (entryId: number) => void
  onToggleRegenerateFlow?: (entryId: number) => void
  regenerationFlowOpenByEntry?: Record<number, boolean>
  regenerationReasoningByEntry?: Record<number, string>
  regenerationTokensByEntry?: Record<number, number>
  regeneratingEntryId?: number | null
}) {
  const nav = useNavigate()
  const handleNavigate = (chatId: number, msgId: number) => {
    nav(`/chats?chatId=${chatId}&msgId=${msgId}`)
  }

  const isHistoryEntry = (entry: BriefingSummary | BriefingHistoryEntry): entry is BriefingHistoryEntry =>
    typeof (entry as BriefingHistoryEntry).id === 'number'

  const trackedSummaries = entries.filter((s) => s.category === 'tracked')
  const briefingSummaries = entries.filter((s) => s.category === 'briefing')
  const importantContactSummaries = entries.filter((s) => s.category === 'important_contact')
  return (
    <div className="space-y-4">
      {importantContactSummaries.length > 0 && (
        <section>
          <div className="mb-2 flex items-center gap-2">
            <Star size={12} className="fill-amber-400 text-amber-400" />
            <h3 className="text-[11px] font-semibold uppercase tracking-wider text-amber-400">
              Important Contacts
            </h3>
            <span className="rounded-full bg-amber-500/20 px-1.5 py-0.5 text-[10px] text-amber-400">
              {importantContactSummaries.length}
            </span>
          </div>
          <div className="space-y-2">
            {importantContactSummaries.map((s, i) => (
              <div
                key={isHistoryEntry(s) ? s.id : `${s.chat_id}-${i}-important`}
                className="rounded-lg border border-amber-500/20 bg-amber-900/10 px-3 py-2"
              >
                <div className="mb-1 flex items-center justify-between gap-2">
                  <p className="text-[11px] font-medium text-amber-300">{s.chat_name}</p>
                  {onRegenerateEntry && isHistoryEntry(s) && (
                    <div className="inline-flex items-center gap-1">
                      {regeneratingEntryId === s.id ? (
                        <button
                          onClick={() => onStopRegenerate?.(s.id)}
                          className="inline-flex items-center gap-1 rounded border border-rose-500/30 px-1.5 py-0.5 text-[10px] text-rose-300 hover:bg-rose-900/40"
                          title="Stop regenerating"
                        >
                          <X size={10} />
                          Stop
                        </button>
                      ) : (
                        <button
                          onClick={() => onRegenerateEntry(s)}
                          className="inline-flex items-center gap-1 rounded border border-amber-500/30 px-1.5 py-0.5 text-[10px] text-amber-300 hover:bg-amber-900/40"
                          title="Regenerate this summary only"
                        >
                          <RefreshCw size={10} />
                          Regenerate
                        </button>
                      )}
                      {(regeneratingEntryId === s.id || (regenerationReasoningByEntry?.[s.id]?.length ?? 0) > 0) && (
                        <button
                          onClick={() => onToggleRegenerateFlow?.(s.id)}
                          className="inline-flex items-center gap-1 rounded border border-amber-500/20 px-1.5 py-0.5 text-[10px] text-amber-200 hover:bg-amber-900/30"
                          title="Show or hide reasoning flow"
                        >
                          {(regenerationFlowOpenByEntry?.[s.id] ?? false) ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
                          Flow
                          {regeneratingEntryId === s.id && typeof regenerationTokensByEntry?.[s.id] === 'number' && (
                            <span className="text-[9px] opacity-80">{regenerationTokensByEntry[s.id]}</span>
                          )}
                        </button>
                      )}
                    </div>
                  )}
                </div>
                <p className="text-[11px] leading-relaxed text-slate-300 whitespace-pre-line">
                  <SummaryWithSources summary={s.summary} sources={s.sources} onNavigate={handleNavigate} />
                </p>
                {isHistoryEntry(s) && (regenerationFlowOpenByEntry?.[s.id] ?? false) && (
                  <div className="mt-2 rounded border border-amber-500/20 bg-slate-900/60 p-2">
                    <pre className="whitespace-pre-wrap break-words text-[11px] leading-relaxed text-slate-400">
                      {(regenerationReasoningByEntry?.[s.id] ?? '') || 'Waiting for reasoning stream…'}
                    </pre>
                  </div>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      {trackedSummaries.length > 0 && (
        <section>
          <div className="mb-2 flex items-center gap-2">
            <Radio size={12} className="text-violet-400" />
            <h3 className="text-[11px] font-semibold uppercase tracking-wider text-violet-400">
              Tracked Chats
            </h3>
            <span className="rounded-full bg-violet-500/20 px-1.5 py-0.5 text-[10px] text-violet-400">
              {trackedSummaries.length}
            </span>
          </div>
          <div className="space-y-2">
            {trackedSummaries.map((s, i) => (
              <div
                key={isHistoryEntry(s) ? s.id : `${s.chat_id}-${i}-tracked`}
                className="rounded-lg border border-violet-500/20 bg-violet-900/10 px-3 py-2"
              >
                <div className="mb-1 flex items-center justify-between gap-2">
                  <p className="text-[11px] font-medium text-violet-300">{s.chat_name}</p>
                  {onRegenerateEntry && isHistoryEntry(s) && (
                    <div className="inline-flex items-center gap-1">
                      {regeneratingEntryId === s.id ? (
                        <button
                          onClick={() => onStopRegenerate?.(s.id)}
                          className="inline-flex items-center gap-1 rounded border border-rose-500/30 px-1.5 py-0.5 text-[10px] text-rose-300 hover:bg-rose-900/40"
                          title="Stop regenerating"
                        >
                          <X size={10} />
                          Stop
                        </button>
                      ) : (
                        <button
                          onClick={() => onRegenerateEntry(s)}
                          className="inline-flex items-center gap-1 rounded border border-violet-500/30 px-1.5 py-0.5 text-[10px] text-violet-300 hover:bg-violet-900/40"
                          title="Regenerate this summary only"
                        >
                          <RefreshCw size={10} />
                          Regenerate
                        </button>
                      )}
                      {(regeneratingEntryId === s.id || (regenerationReasoningByEntry?.[s.id]?.length ?? 0) > 0) && (
                        <button
                          onClick={() => onToggleRegenerateFlow?.(s.id)}
                          className="inline-flex items-center gap-1 rounded border border-violet-500/20 px-1.5 py-0.5 text-[10px] text-violet-200 hover:bg-violet-900/30"
                          title="Show or hide reasoning flow"
                        >
                          {(regenerationFlowOpenByEntry?.[s.id] ?? false) ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
                          Flow
                          {regeneratingEntryId === s.id && typeof regenerationTokensByEntry?.[s.id] === 'number' && (
                            <span className="text-[9px] opacity-80">{regenerationTokensByEntry[s.id]}</span>
                          )}
                        </button>
                      )}
                    </div>
                  )}
                </div>
                <p className="text-[11px] leading-relaxed text-slate-300 whitespace-pre-line">
                  <SummaryWithSources summary={s.summary} sources={s.sources} onNavigate={handleNavigate} />
                </p>
                {isHistoryEntry(s) && (regenerationFlowOpenByEntry?.[s.id] ?? false) && (
                  <div className="mt-2 rounded border border-violet-500/20 bg-slate-900/60 p-2">
                    <pre className="whitespace-pre-wrap break-words text-[11px] leading-relaxed text-slate-400">
                      {(regenerationReasoningByEntry?.[s.id] ?? '') || 'Waiting for reasoning stream…'}
                    </pre>
                  </div>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      {briefingSummaries.length > 0 && (
        <section>
          <div className="mb-2 flex items-center gap-2">
            <Newspaper size={12} className="text-emerald-400" />
            <h3 className="text-[11px] font-semibold uppercase tracking-wider text-emerald-400">
              Briefing Chats
            </h3>
            <span className="rounded-full bg-emerald-500/20 px-1.5 py-0.5 text-[10px] text-emerald-400">
              {briefingSummaries.length}
            </span>
          </div>
          <div className="space-y-2">
            {briefingSummaries.map((s, i) => (
              <div
                key={isHistoryEntry(s) ? s.id : `${s.chat_id}-${i}-briefing`}
                className="rounded-lg border border-emerald-500/20 bg-emerald-900/10 px-3 py-2"
              >
                <div className="mb-1 flex items-center justify-between gap-2">
                  <p className="text-[11px] font-medium text-emerald-300">{s.chat_name}</p>
                  {onRegenerateEntry && isHistoryEntry(s) && (
                    <div className="inline-flex items-center gap-1">
                      {regeneratingEntryId === s.id ? (
                        <button
                          onClick={() => onStopRegenerate?.(s.id)}
                          className="inline-flex items-center gap-1 rounded border border-rose-500/30 px-1.5 py-0.5 text-[10px] text-rose-300 hover:bg-rose-900/40"
                          title="Stop regenerating"
                        >
                          <X size={10} />
                          Stop
                        </button>
                      ) : (
                        <button
                          onClick={() => onRegenerateEntry(s)}
                          className="inline-flex items-center gap-1 rounded border border-emerald-500/30 px-1.5 py-0.5 text-[10px] text-emerald-300 hover:bg-emerald-900/40"
                          title="Regenerate this summary only"
                        >
                          <RefreshCw size={10} />
                          Regenerate
                        </button>
                      )}
                      {(regeneratingEntryId === s.id || (regenerationReasoningByEntry?.[s.id]?.length ?? 0) > 0) && (
                        <button
                          onClick={() => onToggleRegenerateFlow?.(s.id)}
                          className="inline-flex items-center gap-1 rounded border border-emerald-500/20 px-1.5 py-0.5 text-[10px] text-emerald-200 hover:bg-emerald-900/30"
                          title="Show or hide reasoning flow"
                        >
                          {(regenerationFlowOpenByEntry?.[s.id] ?? false) ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
                          Flow
                          {regeneratingEntryId === s.id && typeof regenerationTokensByEntry?.[s.id] === 'number' && (
                            <span className="text-[9px] opacity-80">{regenerationTokensByEntry[s.id]}</span>
                          )}
                        </button>
                      )}
                    </div>
                  )}
                </div>
                <div className="text-[11px] leading-relaxed text-slate-300 whitespace-pre-line">
                  <SummaryWithSources summary={s.summary} sources={s.sources} onNavigate={handleNavigate} />
                </div>
                {isHistoryEntry(s) && (regenerationFlowOpenByEntry?.[s.id] ?? false) && (
                  <div className="mt-2 rounded border border-emerald-500/20 bg-slate-900/60 p-2">
                    <pre className="whitespace-pre-wrap break-words text-[11px] leading-relaxed text-slate-400">
                      {(regenerationReasoningByEntry?.[s.id] ?? '') || 'Waiting for reasoning stream…'}
                    </pre>
                  </div>
                )}
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}

const TIMEFRAME_OPTIONS = [
  { label: '1d',  value: 1  },
  { label: '3d',  value: 3  },
  { label: '7d',  value: 7  },
  { label: '14d', value: 14 },
  { label: '30d', value: 30 },
]

const DAY_OPTIONS = [
  { label: 'Mon', value: 'monday'    },
  { label: 'Tue', value: 'tuesday'   },
  { label: 'Wed', value: 'wednesday' },
  { label: 'Thu', value: 'thursday'  },
  { label: 'Fri', value: 'friday'    },
  { label: 'Sat', value: 'saturday'  },
  { label: 'Sun', value: 'sunday'    },
]

function BriefingTab() {
  // Live run state (current generation)
  const [liveSummaries, setLiveSummaries] = useState<BriefingSummary[]>([])
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState<{ current: number; total: number; chatName: string; tokens?: number } | null>(null)
  const [showFlow, setShowFlow] = useState(false)
  const [flowOrder, setFlowOrder] = useState<string[]>([])
  const [flowByChat, setFlowByChat] = useState<Record<string, string>>({})
  const [error, setError] = useState('')
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  // Schedule prefs (for auto-run display)
  const [schedule, setSchedule] = useState<'daily' | 'weekly'>('daily')
  const [briefingTime, setBriefingTimeState] = useState<string>('09:00')
  const [briefingDay, setBriefingDayState] = useState<string>('monday')
  const [showPromptEditor, setShowPromptEditor] = useState(false)
  const [trackedPromptDraft, setTrackedPromptDraft] = useState('')
  const [briefingPromptDraft, setBriefingPromptDraft] = useState('')
  const [trackedPromptSaved, setTrackedPromptSaved] = useState('')
  const [briefingPromptSaved, setBriefingPromptSaved] = useState('')
  const [trackedPromptDefault, setTrackedPromptDefault] = useState('')
  const [briefingPromptDefault, setBriefingPromptDefault] = useState('')
  const [trackedIsCustom, setTrackedIsCustom] = useState(false)
  const [briefingIsCustom, setBriefingIsCustom] = useState(false)
  const [savingPrompt, setSavingPrompt] = useState(false)
  // Manual generation timeframe (independent of schedule)
  const [selectedDays, setSelectedDays] = useState(1)
  const scheduleRef = useRef<'daily' | 'weekly'>('daily')
  const abortRef = useRef<AbortController | null>(null)
  const runningRef = useRef(false)

  // History state
  const [history, setHistory] = useState<BriefingRun[]>([])
  const [expandedRunId, setExpandedRunId] = useState<number | null>(null)
  const [regeneratingEntryId, setRegeneratingEntryId] = useState<number | null>(null)
  const [regenerationFlowOpenByEntry, setRegenerationFlowOpenByEntry] = useState<Record<number, boolean>>({})
  const [regenerationReasoningByEntry, setRegenerationReasoningByEntry] = useState<Record<number, string>>({})
  const [regenerationTokensByEntry, setRegenerationTokensByEntry] = useState<Record<number, number>>({})
  const regenerationAbortRef = useRef<AbortController | null>(null)
  const promptDirty = trackedPromptDraft !== trackedPromptSaved || briefingPromptDraft !== briefingPromptSaved

  // Sources (briefing chat management) state
  const [showSources, setShowSources] = useState(false)
  const [briefingChats, setBriefingChats] = useState<Chat[]>([])
  const [sourceSearch, setSourceSearch] = useState('')

  const handleRemoveSource = useCallback(async (chat: Chat) => {
    await setBriefing(chat.telegram_id, false)
    setBriefingChats((prev) => prev.filter((c) => c.telegram_id !== chat.telegram_id))
  }, [])

  const loadHistory = useCallback(async () => {
    try {
      const runs = await getBriefingHistory()
      setHistory(runs)
      return runs
    } catch {
      return []
    }
  }, [])

  // daysOverride: used by auto-run to force schedule-derived lookback
  const runBriefing = useCallback(async (daysOverride?: number) => {
    if (runningRef.current) return
    runningRef.current = true
    abortRef.current?.abort()
    abortRef.current = new AbortController()
    setRunning(true)
    setError('')
    setProgress(null)
    setLiveSummaries([])
    setShowFlow(false)
    setFlowOrder([])
    setFlowByChat({})
    const days = daysOverride ?? selectedDays
    try {
      for await (const evt of streamBriefing(days, abortRef.current.signal)) {
        const type = evt._event as string
        if (type === 'start') {
          setProgress({ current: 0, total: evt.total as number, chatName: '' })
        } else if (type === 'progress') {
          const chatName = evt.chat_name as string
          setProgress((p) => ({ current: evt.current as number, total: p?.total ?? 0, chatName, tokens: 0 }))
          setFlowOrder((prev) => (prev.includes(chatName) ? prev : [...prev, chatName]))
        } else if (type === 'thinking') {
          const chatName = evt.chat_name as string
          const tokens = evt.tokens as number | undefined
          const reasoning = evt.reasoning as string | undefined
          if (typeof tokens === 'number') {
            setProgress((p) => p ? { ...p, chatName, tokens } : p)
          }
          if (typeof reasoning === 'string' && reasoning.length > 0) {
            setFlowOrder((prev) => (prev.includes(chatName) ? prev : [...prev, chatName]))
            setFlowByChat((prev) => ({ ...prev, [chatName]: (prev[chatName] ?? '') + reasoning }))
          }
        } else if (type === 'chat_done') {
          setLiveSummaries((prev) => [...prev, evt as unknown as BriefingSummary])
        } else if (type === 'done') {
          setLastUpdated(new Date())
          setBriefingLastRun().catch(() => {})
          const runs = await loadHistory()
          if (runs && runs.length > 0) setExpandedRunId(runs[0].id)
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
  }, [selectedDays, loadHistory])

  const handleSetBriefingDay = useCallback((day: string) => {
    setBriefingDayState(day)
    setBriefingDay(day).catch(() => {})
  }, [])

  const handleSavePrompt = useCallback(async () => {
    setSavingPrompt(true)
    try {
      const trackedTrimmed = trackedPromptDraft.trim()
      const briefingTrimmed = briefingPromptDraft.trim()
      await setBriefingPrompts(trackedTrimmed, briefingTrimmed)
      setTrackedPromptSaved(trackedTrimmed)
      setBriefingPromptSaved(briefingTrimmed)
      setTrackedPromptDraft(trackedTrimmed)
      setBriefingPromptDraft(briefingTrimmed)
      setTrackedIsCustom(trackedTrimmed.length > 0)
      setBriefingIsCustom(briefingTrimmed.length > 0)
    } catch (e: unknown) {
      const err = e as Error
      setError(`Failed to save briefing prompt: ${err.message}`)
    } finally {
      setSavingPrompt(false)
    }
  }, [trackedPromptDraft, briefingPromptDraft])

  const handleResetPrompt = useCallback(async () => {
    setSavingPrompt(true)
    try {
      await setBriefingPrompts('', '')
      setTrackedPromptSaved('')
      setBriefingPromptSaved('')
      setTrackedPromptDraft('')
      setBriefingPromptDraft('')
      setTrackedIsCustom(false)
      setBriefingIsCustom(false)
    } catch (e: unknown) {
      const err = e as Error
      setError(`Failed to reset briefing prompt: ${err.message}`)
    } finally {
      setSavingPrompt(false)
    }
  }, [])

  // Load schedule pref, history, and briefing chats on mount
  // Note: auto-run is handled by the backend scheduler, not the frontend
  useEffect(() => {
    loadHistory()
    getBriefingChats().then(setBriefingChats).catch(() => {})
    getPrefs().then((prefs) => {
      const sched = (prefs.briefing_schedule as 'daily' | 'weekly') ?? 'daily'
      const btime = (prefs.briefing_time as string) ?? '09:00'
      const bday  = (prefs.briefing_day as string)  ?? 'monday'
      setSchedule(sched)
      setBriefingTimeState(btime)
      setBriefingDayState(bday)
      scheduleRef.current = sched
    }).catch(() => {})
    getBriefingPrompts().then(({ tracked, briefing }) => {
      setTrackedPromptDefault(tracked.default)
      setBriefingPromptDefault(briefing.default)
      setTrackedPromptSaved((tracked.custom ?? '').trim())
      setBriefingPromptSaved((briefing.custom ?? '').trim())
      setTrackedPromptDraft((tracked.custom ?? '').trim())
      setBriefingPromptDraft((briefing.custom ?? '').trim())
      setTrackedIsCustom(!!tracked.is_custom)
      setBriefingIsCustom(!!briefing.is_custom)
    }).catch(() => {})
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleDeleteRun = async (runId: number) => {
    try {
      await deleteBriefingRun(runId)
      setHistory((prev) => prev.filter((r) => r.id !== runId))
      if (expandedRunId === runId) setExpandedRunId(null)
    } catch {
      // ignore
    }
  }

  const handleRegenerateEntry = useCallback(async (runId: number, entry: BriefingHistoryEntry) => {
    regenerationAbortRef.current?.abort()
    const controller = new AbortController()
    regenerationAbortRef.current = controller
    setRegeneratingEntryId(entry.id)
    setRegenerationFlowOpenByEntry((prev) => ({ ...prev, [entry.id]: false }))
    setRegenerationReasoningByEntry((prev) => ({ ...prev, [entry.id]: '' }))
    setRegenerationTokensByEntry((prev) => ({ ...prev, [entry.id]: 0 }))
    try {
      for await (const evt of streamRegenerateBriefingEntry(runId, entry.id, controller.signal)) {
        const type = evt._event as string
        if (type === 'thinking') {
          if (typeof evt.tokens === 'number') {
            setRegenerationTokensByEntry((prev) => ({ ...prev, [entry.id]: evt.tokens as number }))
          }
          if (typeof evt.reasoning === 'string' && (evt.reasoning as string).length > 0) {
            setRegenerationReasoningByEntry((prev) => ({
              ...prev,
              [entry.id]: (prev[entry.id] ?? '') + (evt.reasoning as string),
            }))
          }
        } else if (type === 'done') {
          const updated = evt.entry as BriefingHistoryEntry
          setHistory((prev) => prev.map((run) => {
            if (run.id !== runId) return run
            return {
              ...run,
              entries: run.entries.map((e) => (e.id === entry.id ? updated : e)),
            }
          }))
        } else if (type === 'error') {
          setError(`Regeneration failed: ${String(evt.message ?? 'Unknown error')}`)
        }
      }
    } catch (e: unknown) {
      const err = e as Error
      if (err?.name !== 'AbortError') setError(`Regeneration failed: ${err.message}`)
    } finally {
      setRegeneratingEntryId(null)
      regenerationAbortRef.current = null
    }
  }, [])

  const handleStopRegenerate = useCallback((entryId: number) => {
    if (regeneratingEntryId === entryId) {
      regenerationAbortRef.current?.abort()
    }
  }, [regeneratingEntryId])

  const toggleRegenerationFlow = useCallback((entryId: number) => {
    setRegenerationFlowOpenByEntry((prev) => ({ ...prev, [entryId]: !prev[entryId] }))
  }, [])

  const formatRunDate = (isoStr: string) => {
    const d = new Date(isoStr)
    return d.toLocaleString([], {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  }

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      {/* Header bar */}
      <div className="flex shrink-0 items-center justify-between border-b border-slate-700/50 bg-slate-800/40 px-4 py-2.5">
        {/* Left: schedule info */}
        <div className="flex items-center gap-2 text-xs text-slate-400">
          <span className="font-medium text-slate-300 capitalize">{schedule}</span>
          {schedule === 'weekly' && (
            <>
              <span className="text-slate-600">·</span>
              <div className="flex items-center gap-0.5">
                {DAY_OPTIONS.map((d) => (
                  <button
                    key={d.value}
                    onClick={() => handleSetBriefingDay(d.value)}
                    className={`rounded px-1.5 py-0.5 text-[10px] font-medium transition-colors ${
                      briefingDay === d.value
                        ? 'bg-slate-600/60 text-slate-200'
                        : 'text-slate-500 hover:text-slate-300'
                    }`}
                  >
                    {d.label}
                  </button>
                ))}
              </div>
            </>
          )}
          <span className="text-slate-600">·</span>
          <span>{new Date(`1970-01-01T${briefingTime}`).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
          {lastUpdated && (
            <span className="text-slate-500">
              · Updated {lastUpdated.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
            </span>
          )}
        </div>

        {/* Right: timeframe picker + generate button */}
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1">
            {TIMEFRAME_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                onClick={() => setSelectedDays(opt.value)}
                disabled={running}
                className={`rounded-full px-2 py-0.5 text-[10px] font-medium transition-colors disabled:opacity-40 ${
                  selectedDays === opt.value
                    ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/40'
                    : 'text-slate-500 hover:text-slate-300 border border-transparent hover:border-slate-600/50'
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
          {running ? (
            <button
              onClick={() => abortRef.current?.abort()}
              className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium text-rose-400 hover:bg-rose-900/30 hover:text-rose-300 transition-colors border border-rose-600/30"
            >
              <X size={12} />
              Stop
            </button>
          ) : (
            <button
              onClick={() => runBriefing()}
              className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium text-emerald-400 hover:bg-emerald-900/30 hover:text-emerald-300 transition-colors border border-emerald-600/30"
            >
              <RotateCcw size={12} />
              Generate Now
            </button>
          )}
        </div>
      </div>

      {/* Settings drawers: Briefing Prompts + Sources */}
      <div className="shrink-0 border-b border-slate-700/40 bg-slate-900/20 px-4 py-2">
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowPromptEditor((v) => !v)}
            className="inline-flex items-center gap-1 rounded border border-slate-600/60 px-2 py-0.5 text-[10px] text-slate-300 hover:bg-slate-700/30"
          >
            {showPromptEditor ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
            Briefing Prompts
          </button>
          <button
            onClick={() => setShowSources((v) => !v)}
            className="inline-flex items-center gap-1 rounded border border-slate-600/60 px-2 py-0.5 text-[10px] text-slate-300 hover:bg-slate-700/30"
          >
            {showSources ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
            Sources
            <span className="ml-0.5 text-slate-500">({briefingChats.length})</span>
          </button>
        </div>

        {/* Sources panel */}
        {showSources && (() => {
          const filteredSources = briefingChats.filter((c) =>
            c.name.toLowerCase().includes(sourceSearch.toLowerCase())
          )
          return (
            <div className="mt-2 rounded border border-slate-700/60 bg-slate-900/50 p-2">
              <input
                type="text"
                placeholder="Filter chats…"
                value={sourceSearch}
                onChange={(e) => setSourceSearch(e.target.value)}
                className="mb-2 w-full rounded border border-slate-600 bg-slate-800 px-2 py-1 text-[11px] text-gray-200 placeholder:text-slate-500 outline-none focus:border-emerald-500/60"
              />
              <div className="max-h-40 overflow-y-auto space-y-0.5">
                {filteredSources.length === 0 ? (
                  <p className="py-3 text-center text-[11px] text-slate-500">
                    {briefingChats.length === 0
                      ? 'No briefing chats. Mark chats for briefing from the Chats page.'
                      : 'No matches.'}
                  </p>
                ) : (
                  filteredSources.map((chat) => (
                    <div
                      key={chat.telegram_id}
                      className="group flex items-center gap-2 rounded px-2 py-1 hover:bg-slate-700/40"
                    >
                      <div className="flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full bg-emerald-500/20 text-[9px] font-medium text-emerald-400">
                        {chat.name.charAt(0).toUpperCase()}
                      </div>
                      <span className="flex-1 truncate text-[11px] text-slate-200" title={chat.name}>{chat.name}</span>
                      <span className="text-[9px] text-slate-500">{chat.type}</span>
                      <button
                        onClick={() => handleRemoveSource(chat)}
                        title="Remove from briefing"
                        className="hidden group-hover:flex rounded p-0.5 text-slate-500 hover:text-rose-400 transition-colors"
                      >
                        <X size={11} />
                      </button>
                    </div>
                  ))
                )}
              </div>
            </div>
          )
        })()}

        {showPromptEditor && (
          <div className="mt-2 rounded border border-slate-700/60 bg-slate-900/50 p-2">
            <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-violet-300">
              Tracked Chats Prompt {trackedIsCustom ? '(Custom)' : '(Default)'}
            </p>
            <textarea
              value={trackedPromptDraft || trackedPromptDefault}
              onChange={(e) => setTrackedPromptDraft(e.target.value)}
              placeholder="Leave empty to use built-in default prompt."
              className="h-28 w-full resize-y rounded border border-violet-700/60 bg-slate-950/70 px-2 py-1.5 text-[11px] leading-relaxed text-slate-200 outline-none focus:border-violet-500/60"
            />
            <p className="mb-1 mt-3 text-[10px] font-semibold uppercase tracking-wide text-emerald-300">
              Briefing Chats Prompt {briefingIsCustom ? '(Custom)' : '(Default)'}
            </p>
            <textarea
              value={briefingPromptDraft || briefingPromptDefault}
              onChange={(e) => setBriefingPromptDraft(e.target.value)}
              placeholder="Leave empty to use built-in default prompt."
              className="h-32 w-full resize-y rounded border border-slate-700 bg-slate-950/70 px-2 py-1.5 text-[11px] leading-relaxed text-slate-200 outline-none focus:border-emerald-500/60"
            />
            <div className="mt-2 flex items-center gap-2">
              <button
                onClick={handleSavePrompt}
                disabled={savingPrompt || !promptDirty}
                className="rounded border border-emerald-600/40 px-2 py-1 text-[10px] text-emerald-300 hover:bg-emerald-900/30 disabled:opacity-50"
              >
                {savingPrompt ? 'Saving…' : 'Save Prompt'}
              </button>
              <button
                onClick={handleResetPrompt}
                disabled={savingPrompt || (!trackedPromptSaved && !briefingPromptSaved)}
                className="rounded border border-slate-600/50 px-2 py-1 text-[10px] text-slate-300 hover:bg-slate-700/30 disabled:opacity-50"
              >
                Reset to Default
              </button>
              {promptDirty && <span className="text-[10px] text-amber-300">Unsaved changes</span>}
            </div>
          </div>
        )}
      </div>

      {/* Progress bar */}
      {running && progress && (
        <div className="shrink-0 px-4 py-2 bg-slate-800/60 border-b border-slate-700/50">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-xs text-slate-400">
              Summarising <span className="text-slate-200 font-medium">{progress.chatName}</span>…
              {progress.tokens != null && progress.tokens > 0 && (
                <span className="ml-1.5 text-amber-400/80">({progress.tokens} tokens)</span>
              )}
            </span>
            <div className="flex items-center gap-2">
              <span className="text-xs text-slate-500">{progress.current}/{progress.total}</span>
              <button
                onClick={() => abortRef.current?.abort()}
                className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium text-rose-400 hover:bg-rose-900/30 hover:text-rose-300 transition-colors border border-rose-600/30"
              >
                <X size={9} />
                Stop
              </button>
            </div>
          </div>
          <div className="h-1 w-full rounded-full bg-slate-700">
            <div
              className="h-1 rounded-full bg-emerald-500 transition-all duration-300"
              style={{ width: progress.total > 0 ? `${(progress.current / progress.total) * 100}%` : '0%' }}
            />
          </div>

          <div className="mt-2">
            <button
              onClick={() => setShowFlow((v) => !v)}
              className="inline-flex items-center gap-1 rounded border border-slate-600/60 px-2 py-0.5 text-[10px] text-slate-300 hover:bg-slate-700/40"
            >
              {showFlow ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
              {showFlow ? 'Hide' : 'Show'} LLM Flow
            </button>
          </div>

          {showFlow && (
            <div className="mt-2 max-h-64 overflow-y-auto rounded border border-slate-700/60 bg-slate-900/60 p-2 space-y-2">
              {flowOrder.length === 0 ? (
                <p className="text-[11px] text-slate-500">Waiting for reasoning stream…</p>
              ) : (
                flowOrder.map((chatName) => (
                  <div key={chatName} className="rounded border border-slate-700/50 bg-slate-800/50 p-2">
                    <p className="mb-1 text-[10px] font-medium uppercase tracking-wide text-slate-300">{chatName}</p>
                    <pre className="whitespace-pre-wrap break-words text-[11px] leading-relaxed text-slate-400">
                      {flowByChat[chatName] || '(no reasoning chunks yet)'}
                    </pre>
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="shrink-0 mx-4 mt-3 flex items-center gap-2 rounded-lg border border-rose-700/40 bg-rose-900/20 px-3 py-2 text-xs text-rose-400">
          <AlertCircle size={13} />
          {error}
        </div>
      )}

      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
        {/* Live generation results */}
        {running && liveSummaries.length > 0 && (
          <section>
            <div className="mb-3 flex items-center gap-2">
              <Loader2 size={13} className="text-emerald-400 animate-spin" />
              <h3 className="text-xs font-semibold text-emerald-400">Generating…</h3>
              <span className="rounded-full bg-emerald-500/10 border border-emerald-500/30 px-1.5 py-0.5 text-[10px] text-emerald-500">
                Last {selectedDays === 1 ? '24h' : selectedDays === 7 ? '7 days' : selectedDays === 14 ? '14 days' : selectedDays === 30 ? '30 days' : `${selectedDays} days`}
              </span>
            </div>
            <BriefingRunSummaries entries={liveSummaries} />
          </section>
        )}

        {/* History */}
        {history.length === 0 && !running && (
          <div className="flex flex-1 flex-col items-center justify-center gap-3 text-slate-500 py-16">
            <Newspaper size={32} className="opacity-30" />
            <p className="text-sm">No briefings yet. Select a timeframe and click "Generate Now".</p>
          </div>
        )}

        {history.map((run) => {
          const isExpanded = expandedRunId === run.id
          return (
            <div key={run.id} className="rounded-xl border border-slate-700/50 bg-slate-800/30 overflow-hidden">
              {/* Run header */}
              <div
                className="flex items-center justify-between px-4 py-3 cursor-pointer hover:bg-slate-700/20 transition-colors"
                onClick={() => setExpandedRunId(isExpanded ? null : run.id)}
              >
                <div className="flex items-center gap-2">
                  {isExpanded ? (
                    <ChevronDown size={14} className="text-slate-400" />
                  ) : (
                    <ChevronRight size={14} className="text-slate-400" />
                  )}
                  <span className="text-xs font-medium text-slate-200">
                    {formatRunDate(run.generated_at)}
                  </span>
                  <span className="rounded-full bg-slate-700/60 px-1.5 py-0.5 text-[10px] text-slate-400">
                    Last {run.days === 1 ? '24h' : run.days === 7 ? '7 days' : run.days === 14 ? '14 days' : run.days === 30 ? '30 days' : `${run.days} days`} · {run.entries.length} chat{run.entries.length !== 1 ? 's' : ''}
                  </span>
                </div>
                <button
                  onClick={(e) => { e.stopPropagation(); handleDeleteRun(run.id) }}
                  className="rounded-md p-1.5 text-slate-500 hover:bg-rose-900/30 hover:text-rose-400 transition-colors"
                  title="Delete this briefing"
                >
                  <Trash2 size={13} />
                </button>
              </div>

              {/* Run entries (expanded) */}
              {isExpanded && (
                <div className="px-4 pb-4 pt-1 border-t border-slate-700/40">
                  <BriefingRunSummaries
                    entries={run.entries}
                    onRegenerateEntry={(entry) => handleRegenerateEntry(run.id, entry)}
                    onStopRegenerate={handleStopRegenerate}
                    onToggleRegenerateFlow={toggleRegenerationFlow}
                    regenerationFlowOpenByEntry={regenerationFlowOpenByEntry}
                    regenerationReasoningByEntry={regenerationReasoningByEntry}
                    regenerationTokensByEntry={regenerationTokensByEntry}
                    regeneratingEntryId={regeneratingEntryId}
                  />
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function Intelligence() {
  // Top-level tab — read initial tab from URL query param
  const [searchParams] = useSearchParams()
  const initialTab = (['chat', 'mentions', 'briefing'] as const).includes(
    searchParams.get('tab') as 'chat' | 'mentions' | 'briefing'
  ) ? (searchParams.get('tab') as 'chat' | 'mentions' | 'briefing') : 'chat'
  const [activeTab, setActiveTab] = useState<'chat' | 'mentions' | 'briefing'>(initialTab)

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

    // Persist user message BEFORE calling the backend so the backend can load it
    // as part of the conversation history context.
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

    let finalContent = ''

    try {
      // Backend loads full conversation history from DB (conversation_id) and
      // appends the new user message — ensures context is always the persisted history.
      for await (const { event, data } of streamChat(activeConvId, text, ctrl.signal)) {
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
              placeholder="Ask about your chats…"
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
