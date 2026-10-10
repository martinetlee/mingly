import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AlertCircle,
  Brain,
  Check,
  ChevronDown,
  ChevronRight,
  Loader2,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  Plus,
  Send,
  Trash2,
  Wrench,
  X,
} from 'lucide-react'
import {
  createAiConversation,
  deleteAiConversation,
  getAiConversation,
  isAbortError,
  listAiConversations,
  stream,
  suggestAiConversationTitle,
  updateAiConversationTitle,
} from '../../api/client'
import type { AiConversationDetail, AiConversationSummary } from '../../api/types'
import Markdown from '../../components/ui/Markdown'
import { useConfirm } from '../../components/ui'
import { errorMessage, reportError } from '../../lib/toast'
import { fmtDate } from '../../utils/time'

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

export type ChatMessage = UserMessage | AssistantMessage

const TOOL_LABELS: Record<string, (args: Record<string, unknown>) => string> = {
  get_chat_messages: (a) => `Reading messages in "${a.chat_name}"`,
  find_chats_by_tag: (a) => `Finding chats tagged "${a.tag_name}"`,
  search_messages: (a) => `Searching messages for "${a.keyword}"`,
  search_chats_by_title: (a) => `Searching chat titles for "${a.keyword}"`,
  tag_chat: (a) => `Tagging "${a.chat_name}" with "${a.tag_name}"`,
  tag_chats_matching: (a) => `Tagging all chats matching "${a.keyword}" with "${a.tag_name}"`,
  search_messages_in_tagged_chats: (a) => `Searching "${a.tag_name}" chats for "${a.keyword}"`,
  sync_tagged_chats: (a) => `Syncing chats tagged "${a.tag_name}"`,
  find_chats_by_category: (a) => `Finding ${a.category} chats`,
  find_chats_by_member_count: (a) => `Finding chats with ${String(a.operator).replace('_', ' ')} ${a.count} members`,
}

const toMessages = (detail: AiConversationDetail): ChatMessage[] =>
  detail.messages
    .filter((m) => m.role === 'user' || m.role === 'assistant')
    .map((m) => ({ role: m.role as 'user' | 'assistant', content: m.content }))

const toSummary = (conv: AiConversationDetail): AiConversationSummary => ({
  id: conv.id,
  title: conv.title,
  created_at: conv.created_at,
  updated_at: conv.updated_at,
  message_count: conv.messages?.length ?? 0,
})

/** Apply `fn` to the trailing assistant placeholder */
function patchLastAssistant(prev: ChatMessage[], fn: (m: AssistantMessage) => AssistantMessage): ChatMessage[] {
  const last = prev[prev.length - 1]
  if (!last || last.role !== 'assistant') return prev
  const next = [...prev]
  next[next.length - 1] = fn(last)
  return next
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
        {tc.result ? <Wrench size={11} className="shrink-0" /> : <Loader2 size={11} className="shrink-0 animate-spin" />}
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
  return (
    <div className="rounded-lg border border-violet-500/20 bg-violet-500/5 text-xs overflow-hidden">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-violet-400 hover:bg-violet-500/10 transition-colors"
      >
        {allDone ? <Wrench size={11} className="shrink-0" /> : <Loader2 size={11} className="shrink-0 animate-spin" />}
        <span className="flex-1">{allDone ? `Used ${toolCalls.length} tools` : 'Running tools…'}</span>
        {open ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
      </button>
      {open && (
        <div className="border-t border-violet-500/20 p-2 space-y-1.5">
          {toolCalls.map((tc, i) => <ToolCallBadge key={i} tc={tc} />)}
        </div>
      )}
    </div>
  )
}

function MessageBubble({ msg }: { msg: ChatMessage }) {
  if (msg.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[75%] rounded-2xl rounded-tr-sm bg-indigo-600 px-4 py-2.5 text-white">
          <Markdown content={msg.content} />
        </div>
      </div>
    )
  }
  const toolCalls = msg.toolCalls ?? []
  return (
    <div className="flex justify-start">
      <div className="max-w-[85%] space-y-2">
        {toolCalls.length === 1 && <ToolCallBadge tc={toolCalls[0]} />}
        {toolCalls.length > 1 && <ToolCallGroup toolCalls={toolCalls} />}
        {(msg.content || msg.streaming) && (
          <div className="rounded-2xl rounded-tl-sm bg-slate-700 px-4 py-2.5 text-slate-100">
            <Markdown content={msg.content} />
            {msg.streaming && <span className="ml-0.5 inline-block h-4 w-0.5 animate-pulse bg-slate-400" />}
          </div>
        )}
      </div>
    </div>
  )
}

function ConvItem({ conv, active, onSelect, onDelete }: {
  conv: AiConversationSummary
  active: boolean
  onSelect: () => void
  onDelete: () => void
}) {
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
        active ? 'bg-violet-600/20 text-slate-100' : 'text-slate-400 hover:bg-slate-700/50 hover:text-slate-200'
      }`}
    >
      <div className="flex-1 min-w-0">
        <p className="truncate text-xs font-medium leading-snug">{conv.title ?? 'New conversation'}</p>
        <p className="text-[10px] text-slate-500 mt-0.5">{fmtDate(conv.updated_at)}</p>
      </div>
      <button
        onClick={(e) => {
          e.stopPropagation()
          onDelete()
        }}
        className="shrink-0 mt-0.5 opacity-0 group-hover:opacity-100 focus:opacity-100 text-slate-500 hover:text-red-400 transition-all"
        title="Delete conversation"
      >
        <Trash2 size={12} />
      </button>
    </div>
  )
}

const SUGGESTIONS = [
  'Who have I been talking to about fundraising?',
  'What are my tracked chats?',
  'Show me recent messages from my Work chats',
  'Search for messages about the project deadline',
]

// ---------------------------------------------------------------------------
// Chat tab
// ---------------------------------------------------------------------------

export default function ChatTab() {
  const confirm = useConfirm()
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [convList, setConvList] = useState<AiConversationSummary[]>([])
  const [activeConvId, setActiveConvId] = useState<number | null>(null)
  const [loadingConvs, setLoadingConvs] = useState(true)
  const [loadingMessages, setLoadingMessages] = useState(false)

  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')

  const [editingTitle, setEditingTitle] = useState(false)
  const [titleDraft, setTitleDraft] = useState('')

  // Identity refs: async results apply only to the conversation they belong to
  const activeConvIdRef = useRef<number | null>(null)
  const convListRef = useRef<AiConversationSummary[]>([])
  convListRef.current = convList
  const streamRef = useRef<AbortController | null>(null)
  const loadRef = useRef<AbortController | null>(null)
  const listSeqRef = useRef(0)
  const titleSuggestedRef = useRef(new Set<number>())

  const bottomRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const titleInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => () => {
    streamRef.current?.abort()
    loadRef.current?.abort()
  }, [])

  /** Latest-only list refresh; keeps a locally applied title if the list predates it */
  const refreshConvList = useCallback(async () => {
    const seq = ++listSeqRef.current
    const list = await listAiConversations()
    if (seq !== listSeqRef.current) return null
    setConvList((prev) => list.map((c) => {
      const local = prev.find((p) => p.id === c.id)
      return c.title == null && local?.title ? { ...c, title: local.title } : c
    }))
    return list
  }, [])

  const abortStream = () => {
    streamRef.current?.abort()
    streamRef.current = null
    setRunning(false)
  }

  /** Switch the view to `id` (or nothing). Aborts the running turn of the previous conversation. */
  const activate = useCallback((id: number | null) => {
    abortStream()
    loadRef.current?.abort()
    loadRef.current = null
    activeConvIdRef.current = id
    setActiveConvId(id)
    setMessages([])
    setError('')
    setEditingTitle(false)
    setLoadingMessages(false)
  }, [])

  const loadMessages = useCallback(async (id: number) => {
    const ctrl = new AbortController()
    loadRef.current = ctrl
    setLoadingMessages(true)
    try {
      const detail = await getAiConversation(id, ctrl.signal)
      if (activeConvIdRef.current !== id || loadRef.current !== ctrl) return
      setMessages(toMessages(detail))
    } catch (e) {
      if (activeConvIdRef.current === id && !isAbortError(e)) reportError(e, 'Could not load conversation')
    } finally {
      if (loadRef.current === ctrl) {
        loadRef.current = null
        setLoadingMessages(false)
      }
    }
  }, [])

  const selectConv = useCallback((id: number) => {
    if (id === activeConvIdRef.current) return
    activate(id)
    void loadMessages(id)
  }, [activate, loadMessages])

  const startNewConversation = useCallback(async () => {
    const conv = await createAiConversation()
    setConvList((prev) => [toSummary(conv), ...prev.filter((c) => c.id !== conv.id)])
    activate(conv.id)
  }, [activate])

  // Initial load: most recent conversation, or a fresh one
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const list = await refreshConvList()
        if (cancelled || !list) return
        if (list.length === 0) {
          await startNewConversation()
        } else {
          activate(list[0].id)
          void loadMessages(list[0].id)
        }
      } catch (e) {
        if (!cancelled) reportError(e, 'Could not load conversations')
      } finally {
        if (!cancelled) setLoadingConvs(false)
      }
    })()
    return () => { cancelled = true }
  }, [refreshConvList, startNewConversation, activate, loadMessages])

  useEffect(() => {
    bottomRef.current?.scrollIntoView?.({ behavior: 'smooth' })
  }, [messages])

  useEffect(() => {
    if (editingTitle) titleInputRef.current?.focus()
  }, [editingTitle])

  const newConv = async () => {
    try {
      await startNewConversation()
    } catch (e) {
      reportError(e, 'Could not create conversation')
    }
  }

  const deleteConv = async (conv: AiConversationSummary) => {
    await confirm({
      title: 'Delete conversation?',
      message: `"${conv.title ?? 'New conversation'}" and its ${conv.message_count} message${conv.message_count === 1 ? '' : 's'} will be removed.`,
      destructive: true,
      confirmLabel: 'Delete',
      onConfirm: async () => {
        await deleteAiConversation(conv.id)
        const next = convListRef.current.filter((c) => c.id !== conv.id)
        setConvList(next)
        if (conv.id !== activeConvIdRef.current) return
        if (next.length > 0) {
          selectConv(next[0].id)
        } else {
          activate(null)
          await startNewConversation().catch((e) => reportError(e, 'Could not create conversation'))
        }
      },
    })
  }

  // ---------------------------------------------------------------------------
  // Title editing
  // ---------------------------------------------------------------------------

  const activeConv = convList.find((c) => c.id === activeConvId)

  const startEditTitle = () => {
    setTitleDraft(activeConv?.title ?? '')
    setEditingTitle(true)
  }

  const commitTitle = async () => {
    const convId = activeConvIdRef.current
    const trimmed = titleDraft.trim()
    setEditingTitle(false)
    if (!trimmed || convId == null) return
    const previous = convListRef.current.find((c) => c.id === convId)?.title ?? null
    if (trimmed === previous) return
    setConvList((prev) => prev.map((c) => (c.id === convId ? { ...c, title: trimmed } : c)))
    try {
      await updateAiConversationTitle(convId, trimmed)
    } catch (e) {
      setConvList((prev) => prev.map((c) => (c.id === convId && c.title === trimmed ? { ...c, title: previous } : c)))
      reportError(e, 'Could not rename conversation')
    }
  }

  /**
   * Ask the server for a title once per untitled conversation. The server
   * writes it compare-and-set; locally it's applied only to that same
   * conversation and only while it's still untitled (a manual rename wins).
   */
  const maybeSuggestTitle = (convId: number) => {
    if (titleSuggestedRef.current.has(convId)) return
    const conv = convListRef.current.find((c) => c.id === convId)
    if (!conv || conv.title != null) return
    titleSuggestedRef.current.add(convId)
    suggestAiConversationTitle(convId)
      .then(({ title }) => {
        if (!title) return
        setConvList((prev) => prev.map((c) => (c.id === convId && c.title == null ? { ...c, title } : c)))
      })
      .catch((e) => reportError(e, 'Title suggestion failed'))
  }

  // ---------------------------------------------------------------------------
  // Send — the backend persists both the user message and the reply
  // ---------------------------------------------------------------------------

  const send = async () => {
    const text = input.trim()
    const convId = activeConvIdRef.current
    if (!text || running || convId == null) return

    setInput('')
    setError('')
    setMessages((prev) => [
      ...prev,
      { role: 'user', content: text },
      { role: 'assistant', content: '', toolCalls: [], streaming: true },
    ])
    setRunning(true)

    const ctrl = new AbortController()
    streamRef.current = ctrl
    // Updates land only while this turn's stream is current for this conversation
    const isCurrent = () => streamRef.current === ctrl && activeConvIdRef.current === convId
    const patch = (fn: (m: AssistantMessage) => AssistantMessage) => {
      if (isCurrent()) setMessages((prev) => patchLastAssistant(prev, fn))
    }

    let completed = false
    try {
      const events = stream<Record<string, unknown>>('/api/intelligence/chat', {
        method: 'POST',
        body: { ai_conversation_id: convId, message: text },
        signal: ctrl.signal,
      })
      for await (const { event, data } of events) {
        if (!isCurrent()) break
        if (event === 'tool_call') {
          patch((m) => ({
            ...m,
            toolCalls: [...(m.toolCalls ?? []), { name: String(data.name), args: (data.args as Record<string, unknown>) ?? {} }],
          }))
        } else if (event === 'tool_result') {
          let matched = false
          patch((m) => ({
            ...m,
            toolCalls: (m.toolCalls ?? []).map((tc) => {
              if (matched || tc.name !== data.name || tc.result) return tc
              matched = true
              return { ...tc, result: String(data.content ?? '') }
            }),
          }))
        } else if (event === 'text') {
          const chunk = String(data.content ?? '')
          patch((m) => ({ ...m, content: m.content + chunk }))
        } else if (event === 'done') {
          completed = true
          break
        } else if (event === 'error') {
          if (isCurrent()) setError(String(data.message || 'Something went wrong'))
          break
        }
      }
      if (!completed && isCurrent() && !ctrl.signal.aborted) {
        setError((e) => e || 'The connection closed before the reply finished.')
      }
    } catch (e) {
      if (!isAbortError(e) && isCurrent()) setError(errorMessage(e))
    } finally {
      patch((m) => ({ ...m, streaming: false }))
      if (streamRef.current === ctrl) {
        streamRef.current = null
        setRunning(false)
      }
    }

    if (completed) {
      // Server-side message_count/updated_at changed; then name it if untitled
      refreshConvList()
        .catch((e) => reportError(e, 'Could not refresh conversations'))
        .finally(() => maybeSuggestTitle(convId))
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      void send()
    }
  }

  return (
    <div className="flex flex-1 overflow-hidden">
      {sidebarOpen && (
        <div className="flex w-60 shrink-0 flex-col border-r border-slate-700/50 bg-slate-800/40">
          <div className="flex items-center gap-2 px-3 py-3 border-b border-slate-700/50">
            <button
              onClick={() => void newConv()}
              className="flex flex-1 items-center gap-1.5 rounded-lg bg-violet-600/80 px-3 py-1.5 text-xs font-medium text-white hover:bg-violet-500 transition-colors"
            >
              <Plus size={13} />
              New chat
            </button>
            <button onClick={() => setSidebarOpen(false)} className="text-slate-500 hover:text-slate-300 transition-colors" title="Collapse sidebar">
              <PanelLeftClose size={16} />
            </button>
          </div>

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
                onDelete={() => void deleteConv(conv)}
              />
            ))}
          </div>
        </div>
      )}

      <div className="flex flex-1 flex-col overflow-hidden">
        <div className="flex items-center gap-3 border-b border-slate-700/50 px-4 py-3">
          {!sidebarOpen && (
            <button onClick={() => setSidebarOpen(true)} className="text-slate-500 hover:text-slate-300 transition-colors shrink-0" title="Open sidebar">
              <PanelLeftOpen size={16} />
            </button>
          )}
          <Brain size={18} className="text-violet-400 shrink-0" />

          <div className="flex flex-1 items-center gap-1.5 min-w-0">
            {editingTitle ? (
              <>
                <input
                  ref={titleInputRef}
                  aria-label="Conversation title"
                  value={titleDraft}
                  onChange={(e) => setTitleDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') void commitTitle()
                    if (e.key === 'Escape') setEditingTitle(false)
                  }}
                  className="flex-1 min-w-0 rounded bg-slate-700 px-2 py-0.5 text-sm text-slate-100 outline-none focus:ring-1 focus:ring-violet-500/50"
                />
                <button onClick={() => void commitTitle()} className="text-emerald-400 hover:text-emerald-300" title="Save title">
                  <Check size={14} />
                </button>
                <button onClick={() => setEditingTitle(false)} className="text-slate-500 hover:text-slate-300" title="Cancel">
                  <X size={14} />
                </button>
              </>
            ) : (
              <div className="min-w-0">
                <button onClick={startEditTitle} className="group flex items-center gap-1 text-left" title="Click to rename" disabled={activeConvId == null}>
                  <span className="truncate text-sm font-semibold text-slate-100">{activeConv?.title ?? 'Intelligence'}</span>
                  <Pencil size={11} className="shrink-0 text-slate-600 group-hover:text-slate-400 transition-colors" />
                </button>
                <p className="text-xs text-slate-500">Ask anything about your chats and messages</p>
              </div>
            )}
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
          {loadingMessages && messages.length === 0 && (
            <div className="flex justify-center py-10">
              <Loader2 size={16} className="animate-spin text-slate-500" />
            </div>
          )}
          {messages.length === 0 && !loadingConvs && !loadingMessages && (
            <div className="flex flex-col items-center justify-center h-full gap-4 text-center">
              <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-violet-500/10 text-violet-400">
                <Brain size={28} />
              </div>
              <div>
                <p className="text-sm font-medium text-slate-300">What would you like to know?</p>
                <p className="mt-1 text-xs text-slate-500">Ask about your chats, search messages, or explore topics across your conversations.</p>
              </div>
              <div className="flex flex-wrap justify-center gap-2 mt-2">
                {SUGGESTIONS.map((s) => (
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

          {messages.map((msg, i) => <MessageBubble key={i} msg={msg} />)}

          {error && (
            <div className="flex items-center gap-2 rounded-lg bg-red-900/20 border border-red-700/30 px-3 py-2 text-xs text-red-400">
              <AlertCircle size={13} />
              {error}
            </div>
          )}

          <div ref={bottomRef} />
        </div>

        <div className="border-t border-slate-700/50 px-4 py-3">
          <div className="flex items-end gap-2 rounded-xl border border-slate-600 bg-slate-800 px-3 py-2 focus-within:border-violet-500/50 focus-within:ring-1 focus-within:ring-violet-500/20 transition-all">
            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask about your chats…"
              aria-label="Message"
              rows={1}
              disabled={running || activeConvId == null}
              className="flex-1 resize-none bg-transparent text-sm text-slate-100 placeholder-slate-500 outline-none disabled:opacity-50"
              style={{ maxHeight: '120px', overflowY: 'auto' }}
            />
            <button
              onClick={() => void send()}
              disabled={!input.trim() || running || activeConvId == null}
              aria-label="Send"
              className="mb-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-violet-600 text-white hover:bg-violet-500 disabled:opacity-40 transition-colors"
            >
              {running ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
            </button>
          </div>
          <p className="mt-1.5 text-center text-[10px] text-slate-600">Enter to send &middot; Shift+Enter for new line</p>
        </div>
      </div>
    </div>
  )
}
