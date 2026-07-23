import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ChevronLeft, ChevronRight, List, MoreVertical, Plus, X } from 'lucide-react'
import type { BoardData, Chat, Tag, WSEvent } from '../api/types'
import {
  addBoardColumn,
  deleteBoardColumn,
  getBoard,
  getTags,
  moveBoardChat,
  updateBoardColumn,
} from '../api/client'
import { useWorkspaces } from '../context/WorkspaceContext'
import { useWebSocket } from '../hooks/useWebSocket'

const AVATAR_COLORS = [
  'bg-rose-600', 'bg-amber-600', 'bg-emerald-600', 'bg-sky-600',
  'bg-violet-600', 'bg-pink-600', 'bg-teal-600', 'bg-orange-600',
]

const COLUMN_ACCENTS: Record<string, string> = {
  indigo: 'border-indigo-500/40', emerald: 'border-emerald-500/40', rose: 'border-rose-500/40',
  amber: 'border-amber-500/40', violet: 'border-violet-500/40', sky: 'border-sky-500/40',
  slate: 'border-slate-500/40',
}

function relativeAge(iso: string | null | undefined): string {
  if (!iso) return '—'
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000)
  if (days <= 0) return 'today'
  if (days === 1) return '1d'
  if (days < 30) return `${days}d`
  if (days < 365) return `${Math.floor(days / 30)}mo`
  return `${Math.floor(days / 365)}y`
}

function ageDays(iso: string | null | undefined): number {
  if (!iso) return 9999
  return Math.floor((Date.now() - new Date(iso).getTime()) / 86400000)
}

function CardAvatar({ chat }: { chat: Chat }) {
  const initial = (chat.name || '?')[0].toUpperCase()
  const bg = AVATAR_COLORS[Math.abs(chat.telegram_id) % AVATAR_COLORS.length]
  return (
    <span className={`flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full text-[10px] font-semibold text-white ${bg}`}>
      {initial}
    </span>
  )
}

function ChatCard({
  chat,
  onOpen,
  onDismiss,
  snippetHighlight,
}: {
  chat: Chat
  onOpen: () => void
  onDismiss?: () => void
  snippetHighlight?: boolean
}) {
  const stale = ageDays(chat.last_activity_at) > 14
  return (
    <div
      draggable
      onDragStart={(e) => {
        e.dataTransfer.setData('text/x-chat-id', String(chat.telegram_id))
        e.dataTransfer.effectAllowed = 'move'
      }}
      onClick={onOpen}
      className={`group cursor-pointer rounded-lg border border-slate-700/60 bg-slate-800 p-2.5 transition-colors hover:border-slate-500 ${
        stale && !snippetHighlight ? 'opacity-60' : ''
      }`}
    >
      <div className="flex items-center gap-2">
        <CardAvatar chat={chat} />
        <span className="min-w-0 flex-1 truncate text-xs font-medium text-gray-200">{chat.name}</span>
        {(chat.unread_count ?? 0) > 0 && (
          <span className="flex h-4 min-w-4 items-center justify-center rounded-full bg-indigo-500 px-1 text-[9px] font-bold text-white">
            {chat.unread_count}
          </span>
        )}
        {onDismiss && (
          <button
            onClick={(e) => { e.stopPropagation(); onDismiss() }}
            title="Not now — back to backlog"
            className="hidden text-slate-500 hover:text-slate-300 group-hover:block"
          >
            <X size={12} />
          </button>
        )}
      </div>
      {chat.last_message && (
        <p className={`mt-1.5 truncate text-[11px] ${snippetHighlight ? 'text-amber-300/90' : 'text-slate-500'}`}>
          {chat.last_message.sender_name && <span className="text-slate-400">{chat.last_message.sender_name}: </span>}
          {chat.last_message.text}
        </p>
      )}
      <p className="mt-1 text-[10px] text-slate-600">{relativeAge(chat.last_activity_at)}</p>
    </div>
  )
}

export default function Board() {
  const navigate = useNavigate()
  const { activeWorkspace, refreshWorkspaces } = useWorkspaces()
  const [board, setBoard] = useState<BoardData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [showBacklog, setShowBacklog] = useState(true)
  const [backlogQuery, setBacklogQuery] = useState('')
  const [dragOverCol, setDragOverCol] = useState<number | 'backlog' | null>(null)
  const [addingColumn, setAddingColumn] = useState(false)
  const [newColumnName, setNewColumnName] = useState('')
  const [stageTags, setStageTags] = useState<Tag[]>([])
  const [columnMenu, setColumnMenu] = useState<number | null>(null)
  const [renamingCol, setRenamingCol] = useState<number | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const [confirmingDeleteCol, setConfirmingDeleteCol] = useState<number | null>(null)
  const lastLoadRef = useRef(0)

  const workspaceId = activeWorkspace?.id

  const loadBoard = useCallback(async () => {
    if (workspaceId == null) return
    try {
      const data = await getBoard(workspaceId)
      setBoard(data)
      setError(null)
      lastLoadRef.current = Date.now()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load board')
    } finally {
      setLoading(false)
    }
  }, [workspaceId])

  // No active kanban workspace → this page has nothing to show
  useEffect(() => {
    if (!activeWorkspace || activeWorkspace.view_mode !== 'kanban') {
      navigate('/chats', { replace: true })
    }
  }, [activeWorkspace, navigate])

  useEffect(() => {
    setLoading(true)
    loadBoard()
  }, [loadBoard])

  const handleWsEvent = useCallback(
    (event: WSEvent) => {
      if (event.type !== 'new_message' && event.type !== 'chat_synced') return
      if (Date.now() - lastLoadRef.current < 5000) return
      loadBoard()
    },
    [loadBoard],
  )
  useWebSocket(handleWsEvent)

  const openChat = (chat: Chat) => navigate(`/chats?chatId=${chat.telegram_id}`)

  const move = async (chatId: number, toTagId: number | null) => {
    if (workspaceId == null || !board) return
    // Optimistic: pluck the chat from wherever it is and place it
    setBoard((prev) => {
      if (!prev) return prev
      let moved: Chat | undefined
      const strip = (chats: Chat[]) => chats.filter((c) => {
        if (c.telegram_id === chatId) { moved = c; return false }
        return true
      })
      const next: BoardData = {
        ...prev,
        columns: prev.columns.map((col) => ({ ...col, chats: strip(col.chats) })),
        unallocated: strip(prev.unallocated),
        backlog: {
          active: strip(prev.backlog.active),
          cooling: strip(prev.backlog.cooling),
          dormant: strip(prev.backlog.dormant),
        },
      }
      if (moved) {
        if (toTagId === null) next.backlog.active = [moved, ...next.backlog.active]
        else next.columns = next.columns.map((col) =>
          col.tag_id === toTagId ? { ...col, chats: [moved!, ...col.chats] } : col,
        )
      }
      return next
    })
    try {
      await moveBoardChat(workspaceId, chatId, toTagId)
      refreshWorkspaces()
    } catch {
      loadBoard() // resync on failure
    }
  }

  const onDropTo = (target: number | null) => (e: React.DragEvent) => {
    e.preventDefault()
    setDragOverCol(null)
    const raw = e.dataTransfer.getData('text/x-chat-id')
    if (raw) move(parseInt(raw, 10), target)
  }

  const startAddColumn = async () => {
    setAddingColumn(true)
    setNewColumnName('')
    try {
      const tags = await getTags()
      setStageTags(tags.filter((t) => t.category === 'stage'))
    } catch { /* autocomplete just stays empty */ }
  }

  const submitAddColumn = async (name: string) => {
    if (workspaceId == null || !name.trim()) return
    try {
      await addBoardColumn(workspaceId, name.trim())
      setAddingColumn(false)
      loadBoard()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to add column')
    }
  }

  const shiftColumn = async (tagId: number, dir: -1 | 1) => {
    if (workspaceId == null || !board) return
    const idx = board.columns.findIndex((c) => c.tag_id === tagId)
    const other = board.columns[idx + dir]
    if (!other) return
    await updateBoardColumn(workspaceId, tagId, { position: other.position })
    await updateBoardColumn(workspaceId, other.tag_id, { position: board.columns[idx].position })
    setColumnMenu(null)
    loadBoard()
  }

  const removeColumn = async (tagId: number) => {
    if (workspaceId == null) return
    await deleteBoardColumn(workspaceId, tagId, true)
    setColumnMenu(null)
    loadBoard()
  }

  const submitRename = async (tagId: number) => {
    if (workspaceId == null || !renameValue.trim()) { setRenamingCol(null); return }
    try {
      await updateBoardColumn(workspaceId, tagId, { name: renameValue.trim() })
      setRenamingCol(null)
      loadBoard()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Rename failed')
      setRenamingCol(null)
    }
  }

  const backlogSections = useMemo(() => {
    if (!board) return []
    const q = backlogQuery.trim().toLowerCase()
    const f = (chats: Chat[]) => (q ? chats.filter((c) => c.name.toLowerCase().includes(q)) : chats)
    return [
      { label: 'Active', hint: '< 7 days', chats: f(board.backlog.active) },
      { label: 'Cooling', hint: '7–30 days', chats: f(board.backlog.cooling) },
      { label: 'Dormant', hint: '> 30 days', chats: f(board.backlog.dormant) },
    ]
  }, [board, backlogQuery])

  if (!activeWorkspace || activeWorkspace.view_mode !== 'kanban') return null

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-3 border-b border-slate-700/50 px-4 py-2.5">
        <span className="text-lg">{activeWorkspace.emoji || '▦'}</span>
        <h1 className="text-sm font-semibold text-gray-200">{activeWorkspace.name}</h1>
        <span className="text-xs text-slate-500">{activeWorkspace.member_count} chats</span>
        <div className="flex-1" />
        <button
          onClick={() => setShowBacklog((v) => !v)}
          className={`rounded-lg border px-2.5 py-1 text-xs transition-colors ${
            showBacklog
              ? 'border-indigo-500/30 bg-indigo-500/15 text-indigo-300'
              : 'border-slate-600 text-slate-400 hover:text-slate-200'
          }`}
        >
          Backlog
        </button>
        <button
          onClick={() => navigate('/chats')}
          title="List view"
          className="flex items-center gap-1 rounded-lg border border-slate-600 px-2.5 py-1 text-xs text-slate-400 transition-colors hover:text-slate-200"
        >
          <List size={13} /> List
        </button>
      </div>

      {error && (
        <div className="mx-4 mt-2 rounded-md border border-rose-500/30 bg-rose-500/10 px-3 py-1.5 text-xs text-rose-400">
          {error}
          <button onClick={() => setError(null)} className="ml-2 text-rose-300">✕</button>
        </div>
      )}

      <div className="flex flex-1 overflow-hidden">
        {/* Backlog drawer */}
        {showBacklog && board && (
          <div
            className={`flex w-60 flex-shrink-0 flex-col border-r border-slate-700/50 bg-slate-900/60 ${
              dragOverCol === 'backlog' ? 'bg-slate-800/80' : ''
            }`}
            onDragOver={(e) => { e.preventDefault(); setDragOverCol('backlog') }}
            onDragLeave={() => setDragOverCol(null)}
            onDrop={onDropTo(null)}
          >
            <div className="border-b border-slate-700/50 p-2">
              <input
                type="text"
                placeholder="Filter backlog…"
                value={backlogQuery}
                onChange={(e) => setBacklogQuery(e.target.value)}
                className="w-full rounded-md border border-slate-600 bg-slate-700 px-2 py-1 text-xs text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500"
              />
            </div>
            <div className="flex-1 overflow-y-auto p-2">
              {backlogSections.map(({ label, hint, chats }) => (
                <div key={label} className="mb-3">
                  <p className="mb-1 px-1 text-[10px] font-medium uppercase tracking-wider text-slate-500">
                    {label} <span className="normal-case text-slate-600">({chats.length}, {hint})</span>
                  </p>
                  <div className="flex flex-col gap-1">
                    {chats.map((chat) => (
                      <div
                        key={chat.telegram_id}
                        draggable
                        onDragStart={(e) => {
                          e.dataTransfer.setData('text/x-chat-id', String(chat.telegram_id))
                          e.dataTransfer.effectAllowed = 'move'
                        }}
                        onClick={() => openChat(chat)}
                        className="flex cursor-pointer items-center gap-1.5 rounded-md px-1.5 py-1 hover:bg-slate-700/50"
                      >
                        <CardAvatar chat={chat} />
                        <span className="min-w-0 flex-1 truncate text-[11px] text-slate-300">{chat.name}</span>
                        <span className="text-[9px] text-slate-600">{relativeAge(chat.last_activity_at)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Columns */}
        <div className="flex flex-1 gap-3 overflow-x-auto p-4">
          {loading && !board ? (
            <p className="text-sm text-slate-500">Loading board…</p>
          ) : board ? (
            <>
              {/* Unallocated ghost column */}
              {board.unallocated.length > 0 && (
                <div
                  className={`flex w-64 flex-shrink-0 flex-col rounded-xl border-2 border-dashed border-amber-500/40 bg-amber-500/5 ${
                    dragOverCol === -1 ? 'border-amber-400' : ''
                  }`}
                >
                  <div className="flex items-center gap-2 px-3 py-2.5">
                    <span className="text-xs font-semibold text-amber-300">Unallocated</span>
                    <span className="rounded-full bg-amber-500/20 px-1.5 text-[10px] text-amber-300">
                      {board.unallocated.length}
                    </span>
                    <span className="text-[10px] text-slate-500">new activity — drag to a column</span>
                  </div>
                  <div className="flex flex-1 flex-col gap-2 overflow-y-auto px-2.5 pb-2.5">
                    {board.unallocated.map((chat) => (
                      <ChatCard
                        key={chat.telegram_id}
                        chat={chat}
                        onOpen={() => openChat(chat)}
                        onDismiss={() => move(chat.telegram_id, null)}
                        snippetHighlight
                      />
                    ))}
                  </div>
                </div>
              )}

              {board.columns.map((col, idx) => (
                <div
                  key={col.tag_id}
                  onDragOver={(e) => { e.preventDefault(); setDragOverCol(col.tag_id) }}
                  onDragLeave={() => setDragOverCol((v) => (v === col.tag_id ? null : v))}
                  onDrop={onDropTo(col.tag_id)}
                  className={`flex w-64 flex-shrink-0 flex-col rounded-xl border bg-slate-800/40 transition-colors ${
                    dragOverCol === col.tag_id
                      ? 'border-indigo-400 bg-indigo-500/10'
                      : COLUMN_ACCENTS[col.color] ?? 'border-slate-700/60'
                  }`}
                >
                  <div className="relative flex items-center gap-2 px-3 py-2.5">
                    {renamingCol === col.tag_id ? (
                      <input
                        autoFocus
                        value={renameValue}
                        onChange={(e) => setRenameValue(e.target.value)}
                        onKeyDown={(e) => { if (e.key === 'Enter') submitRename(col.tag_id); if (e.key === 'Escape') setRenamingCol(null) }}
                        onBlur={() => submitRename(col.tag_id)}
                        className="w-28 rounded border border-indigo-500 bg-slate-700 px-1.5 py-0.5 text-xs text-gray-200 outline-none"
                      />
                    ) : (
                      <span className="text-xs font-semibold text-gray-200">{col.name}</span>
                    )}
                    <span className="rounded-full bg-slate-700 px-1.5 text-[10px] text-slate-400">{col.chats.length}</span>
                    <div className="flex-1" />
                    <button
                      onClick={() => setColumnMenu((v) => (v === col.tag_id ? null : col.tag_id))}
                      className="text-slate-500 hover:text-slate-300"
                    >
                      <MoreVertical size={13} />
                    </button>
                    {columnMenu === col.tag_id && (
                      <div className="absolute right-2 top-8 z-20 w-40 rounded-lg border border-slate-600 bg-slate-800 py-1 text-xs shadow-xl">
                        <button
                          onClick={() => { setRenamingCol(col.tag_id); setRenameValue(col.name); setColumnMenu(null) }}
                          className="block w-full px-3 py-1.5 text-left text-slate-300 hover:bg-slate-700/50"
                        >
                          Rename
                        </button>
                        <button
                          onClick={() => shiftColumn(col.tag_id, -1)}
                          disabled={idx === 0}
                          className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left text-slate-300 hover:bg-slate-700/50 disabled:opacity-40"
                        >
                          <ChevronLeft size={12} /> Move left
                        </button>
                        <button
                          onClick={() => shiftColumn(col.tag_id, 1)}
                          disabled={idx === board.columns.length - 1}
                          className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left text-slate-300 hover:bg-slate-700/50 disabled:opacity-40"
                        >
                          <ChevronRight size={12} /> Move right
                        </button>
                        {confirmingDeleteCol === col.tag_id ? (
                          <div className="px-3 py-1.5">
                            <p className="mb-1 text-[10px] text-slate-400">
                              {col.chats.length} chats return to backlog.
                            </p>
                            <button
                              onClick={() => { setConfirmingDeleteCol(null); removeColumn(col.tag_id) }}
                              className="mr-2 font-medium text-rose-400 hover:text-rose-300"
                            >
                              Delete
                            </button>
                            <button
                              onClick={() => setConfirmingDeleteCol(null)}
                              className="text-slate-500 hover:text-slate-300"
                            >
                              Cancel
                            </button>
                          </div>
                        ) : (
                          <button
                            onClick={() => setConfirmingDeleteCol(col.tag_id)}
                            className="block w-full px-3 py-1.5 text-left text-rose-400 hover:bg-slate-700/50"
                          >
                            Delete column
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                  <div className="flex flex-1 flex-col gap-2 overflow-y-auto px-2.5 pb-2.5">
                    {col.chats.map((chat) => (
                      <ChatCard key={chat.telegram_id} chat={chat} onOpen={() => openChat(chat)} />
                    ))}
                    {col.chats.length === 0 && (
                      <p className="px-1 py-4 text-center text-[11px] text-slate-600">Drop chats here</p>
                    )}
                  </div>
                </div>
              ))}

              {/* Add column */}
              <div className="w-56 flex-shrink-0">
                {addingColumn ? (
                  <div className="rounded-xl border border-slate-600 bg-slate-800 p-2">
                    <input
                      autoFocus
                      type="text"
                      placeholder="Column name…"
                      value={newColumnName}
                      onChange={(e) => setNewColumnName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') submitAddColumn(newColumnName)
                        if (e.key === 'Escape') setAddingColumn(false)
                      }}
                      className="w-full rounded-md border border-slate-600 bg-slate-700 px-2 py-1.5 text-xs text-gray-200 outline-none focus:border-indigo-500"
                    />
                    {stageTags.filter((t) => t.name.toLowerCase().includes(newColumnName.toLowerCase()) && !board.columns.some((c) => c.tag_id === t.id)).slice(0, 5).map((t) => (
                      <button
                        key={t.id}
                        onClick={() => submitAddColumn(t.name)}
                        className="mt-1 block w-full rounded px-2 py-1 text-left text-[11px] text-slate-300 hover:bg-slate-700/50"
                      >
                        {t.name} <span className="text-slate-500">(existing stage)</span>
                      </button>
                    ))}
                    <div className="mt-1.5 flex justify-end gap-1.5">
                      <button onClick={() => setAddingColumn(false)} className="rounded px-2 py-1 text-[11px] text-slate-400 hover:bg-slate-700/50">Cancel</button>
                      <button onClick={() => submitAddColumn(newColumnName)} className="rounded bg-indigo-500 px-2.5 py-1 text-[11px] font-medium text-white hover:bg-indigo-400">Add</button>
                    </div>
                  </div>
                ) : (
                  <button
                    onClick={startAddColumn}
                    className="flex w-full items-center justify-center gap-1.5 rounded-xl border border-dashed border-slate-700 py-3 text-xs text-slate-500 transition-colors hover:border-slate-500 hover:text-slate-300"
                  >
                    <Plus size={13} /> Add column
                  </button>
                )}
              </div>
            </>
          ) : null}
        </div>
      </div>
    </div>
  )
}
