import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { conversationUrl } from '../lib/routes'
import { conversationTitle } from '../lib/conversation'
import ChannelIcon from '../components/channels/ChannelIcon'
import ConversationAvatar from '../components/chat/ConversationAvatar'
import { ChevronLeft, ChevronRight, List, MoreVertical, Plus, Settings2, X } from 'lucide-react'
import type { BoardData, Conversation } from '../api/types'
import {
  addBoardColumn,
  addWorkspaceBoard,
  deleteBoardColumn,
  deleteWorkspaceBoard,
  getBoard,
  moveBoardConversation,
  reorderBoardColumns,
  updateBoardColumn,
  updateWorkspaceBoard,
} from '../api/client'
import { useWorkspaces } from '../context/WorkspaceContext'
import { useWsEvent } from '../hooks/useWebSocket'
import { useCoalescedRefetch } from '../hooks/useCoalescedRefetch'
import { useConfirm } from '../components/ui'
import { errorMessage, reportError } from '../lib/toast'
import { ageDays as serverAgeDays } from '../utils/time'
import WorkspaceDialog from '../components/layout/WorkspaceDialog'
import { readStoredId, storeId } from '../lib/idStorage'

const COLUMN_ACCENTS: Record<string, string> = {
  indigo: 'border-indigo-500/40', emerald: 'border-emerald-500/40', rose: 'border-rose-500/40',
  amber: 'border-amber-500/40', violet: 'border-violet-500/40', sky: 'border-sky-500/40',
  slate: 'border-slate-500/40',
}

function relativeAge(iso: string | null | undefined): string {
  const days = serverAgeDays(iso)
  if (days == null) return '—'
  if (days <= 0) return 'today'
  if (days === 1) return '1d'
  if (days < 30) return `${days}d`
  if (days < 365) return `${Math.floor(days / 30)}mo`
  return `${Math.floor(days / 365)}y`
}

function ageDays(iso: string | null | undefined): number {
  return serverAgeDays(iso) ?? 9999
}

const storedBoardFor = (workspaceId: number): number | null => readStoredId(`activeBoard:${workspaceId}`)

const storeBoardFor = (workspaceId: number, boardId: number | null) => storeId(`activeBoard:${workspaceId}`, boardId)

/** Optimistically move a card: pluck it from wherever it is and place it. */
export function applyMove(prev: BoardData, conversationId: number, toTagId: number | null): BoardData {
  let moved: Conversation | undefined
  const strip = (chats: Conversation[]) => chats.filter((c) => {
    if (c.id === conversationId) { moved = c; return false }
    return true
  })
  const next: BoardData = {
    ...prev,
    columns: prev.columns.map((col) => ({ ...col, conversations: strip(col.conversations) })),
    unallocated: strip(prev.unallocated),
    backlog: {
      active: strip(prev.backlog.active),
      cooling: strip(prev.backlog.cooling),
      dormant: strip(prev.backlog.dormant),
    },
  }
  if (moved) {
    const card = moved
    if (toTagId === null) next.backlog.active = [card, ...next.backlog.active]
    else next.columns = next.columns.map((col) =>
      col.tag_id === toTagId ? { ...col, conversations: [card, ...col.conversations] } : col,
    )
  }
  return next
}

function CardAvatar({ chat }: { chat: Conversation }) {
  return <ConversationAvatar conversation={chat} size={24} />
}

function ChatCard({
  chat,
  onOpen,
  onDismiss,
  snippetHighlight,
}: {
  chat: Conversation
  onOpen: () => void
  onDismiss?: () => void
  snippetHighlight?: boolean
}) {
  const stale = ageDays(chat.last_activity_at) > 14
  return (
    <div
      draggable
      onDragStart={(e) => {
        e.dataTransfer.setData('text/x-chat-id', String(chat.id))
        e.dataTransfer.effectAllowed = 'move'
      }}
      onClick={onOpen}
      className={`group cursor-pointer rounded-lg border border-slate-700/60 bg-slate-800 p-2.5 transition-colors hover:border-slate-500 ${
        stale && !snippetHighlight ? 'opacity-60' : ''
      }`}
    >
      <div className="flex items-center gap-2">
        <CardAvatar chat={chat} />
        <span className="min-w-0 flex-1 truncate text-xs font-medium text-gray-200">{conversationTitle(chat)}</span>
        <ChannelIcon channel={chat.channel} />
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
  const confirm = useConfirm()
  const { activeWorkspace, loaded: workspacesLoaded, refreshWorkspaces } = useWorkspaces()
  const [boardState, setBoardState] = useState<BoardData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [showBacklog, setShowBacklog] = useState(true)
  const [backlogQuery, setBacklogQuery] = useState('')
  // Default to the triage view: only chats not yet classified into any stream
  const [backlogFilter, setBacklogFilter] = useState<'unclassified' | 'all'>('unclassified')
  const [dragOverCol, setDragOverCol] = useState<number | 'backlog' | null>(null)
  const [addingColumn, setAddingColumn] = useState(false)
  const [newColumnName, setNewColumnName] = useState('')
  const [columnMenu, setColumnMenu] = useState<number | null>(null)
  const [renamingCol, setRenamingCol] = useState<number | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const [showSettings, setShowSettings] = useState(false)
  // Stream boards (tabs)
  const [addingBoard, setAddingBoard] = useState(false)
  const [newBoardName, setNewBoardName] = useState('')
  const [newBoardStream, setNewBoardStream] = useState('')
  const [boardMenu, setBoardMenu] = useState<number | null>(null)
  const [renamingBoard, setRenamingBoard] = useState<number | null>(null)
  const [boardRenameValue, setBoardRenameValue] = useState('')
  const [scopingBoard, setScopingBoard] = useState<number | null>(null)
  const [scopeValue, setScopeValue] = useState('')

  const workspaceId = activeWorkspace?.id

  // The selected tab belongs to a workspace: switching workspaces reads that
  // workspace's remembered tab in the same render, so a request can never pair
  // workspace B with workspace A's board id
  const [tabChoice, setTabChoice] = useState<{ wsId: number; boardId: number | null } | null>(null)
  const activeBoardId = workspaceId == null
    ? null
    : tabChoice?.wsId === workspaceId ? tabChoice.boardId : storedBoardFor(workspaceId)
  const setActiveBoardId = useCallback((boardId: number | null) => {
    if (workspaceId == null) return
    setTabChoice({ wsId: workspaceId, boardId })
  }, [workspaceId])

  const selectBoard = (boardId: number) => {
    setActiveBoardId(boardId)
    if (workspaceId != null) storeBoardFor(workspaceId, boardId)
  }

  // Never render (or act on) a board that belongs to another workspace
  const board = boardState && boardState.workspace.id === workspaceId ? boardState : null

  // Sequence guard over loads AND mutations: every load or optimistic change
  // bumps it, and a response is applied only if nothing newer happened since.
  // While a mutation is in flight, loads are deferred (a read issued before
  // the server applied the mutation would snap the card back) and run once
  // the last mutation settles. In-flight tracking is per workspace epoch: a
  // mutation still saving for the previous workspace never defers (or
  // swallows) the new workspace's loads.
  const boardSeqRef = useRef(0)
  const epochRef = useRef(0)
  const inflightRef = useRef(0)
  const reloadPendingRef = useRef(false)
  const keyRef = useRef({ workspaceId, activeBoardId })
  keyRef.current = { workspaceId, activeBoardId }

  const loadBoard = useCallback(async () => {
    if (workspaceId == null) return
    if (inflightRef.current > 0) { reloadPendingRef.current = true; return }
    const seq = ++boardSeqRef.current
    try {
      const data = await getBoard(workspaceId, activeBoardId ?? undefined)
      if (seq !== boardSeqRef.current) return
      setBoardState(data)
      // Backend falls back to the first board when the requested one is gone
      if (data.board_id !== activeBoardId) setActiveBoardId(data.board_id)
      setError(null)
    } catch (e) {
      if (seq !== boardSeqRef.current) return
      setError(`Couldn't load board: ${errorMessage(e)}`)
    } finally {
      if (seq === boardSeqRef.current) setLoading(false)
    }
  }, [workspaceId, activeBoardId, setActiveBoardId])
  const loadBoardRef = useRef(loadBoard)
  loadBoardRef.current = loadBoard

  /**
   * Run a board mutation. `optimistic` is applied immediately; a returned
   * BoardData is the committed board and replaces local state if no newer
   * load/mutation started meanwhile. Otherwise the board is re-read after
   * the last in-flight mutation settles. Failures toast and resync.
   */
  const mutate = useCallback(async (
    errorContext: string,
    fn: () => Promise<BoardData | unknown>,
    optimistic?: (prev: BoardData) => BoardData,
  ): Promise<boolean> => {
    const wsId = workspaceId
    const epoch = epochRef.current
    const seq = ++boardSeqRef.current
    inflightRef.current++
    if (optimistic) setBoardState((prev) => (prev && prev.workspace.id === wsId ? optimistic(prev) : prev))
    let ok = false
    try {
      const result = await fn()
      ok = true
      const isBoard = result != null && typeof result === 'object' && 'columns' in result && 'workspace' in result
      if (isBoard && seq === boardSeqRef.current && keyRef.current.workspaceId === wsId) {
        const data = result as BoardData
        if (data.workspace.id === wsId && data.board_id === keyRef.current.activeBoardId) {
          boardSeqRef.current++ // the newest snapshot: invalidate any older read
          setBoardState(data)
        } else {
          if (epoch === epochRef.current) reloadPendingRef.current = true
        }
      } else {
        if (epoch === epochRef.current) reloadPendingRef.current = true
      }
    } catch (e) {
      reportError(e, errorContext)
      if (epoch === epochRef.current) reloadPendingRef.current = true // undo the optimistic change
    } finally {
      // A mutation from a previous workspace: its counter was reset on the
      // switch and its result was already ignored above
      if (epoch === epochRef.current) {
        inflightRef.current--
        if (inflightRef.current === 0 && reloadPendingRef.current) {
          reloadPendingRef.current = false
          // Through a ref: the tab may have changed while this was in flight
          loadBoardRef.current()
        }
      }
    }
    return ok
  }, [workspaceId])

  // No active kanban workspace → this page has nothing to show.
  // Wait for the workspace list to load first, or a direct landing on /board
  // would bounce to /chats before the active workspace is even known.
  useEffect(() => {
    if (!workspacesLoaded) return
    if (!activeWorkspace || activeWorkspace.view_mode !== 'kanban') {
      navigate('/chats', { replace: true })
    }
  }, [activeWorkspace, workspacesLoaded, navigate])

  // Workspace switch: drop everything that belonged to the previous one
  useEffect(() => {
    boardSeqRef.current++
    epochRef.current++
    inflightRef.current = 0
    reloadPendingRef.current = false
    setBoardState(null)
    setColumnMenu(null)
    setBoardMenu(null)
    setRenamingCol(null)
    setRenamingBoard(null)
    setScopingBoard(null)
    setAddingColumn(false)
    setAddingBoard(false)
    setError(null)
  }, [workspaceId])

  const boardStateRef = useRef(boardState)
  boardStateRef.current = boardState
  useEffect(() => {
    // Adopting the server's fallback tab (board_id) changes activeBoardId;
    // the board on screen already is that board, so don't fetch it twice
    const cur = boardStateRef.current
    if (cur && cur.workspace.id === workspaceId && cur.board_id === activeBoardId) return
    setLoading(true)
    loadBoard()
  }, [loadBoard, workspaceId, activeBoardId])

  // Chat activity changes card snippets/backlog buckets: refresh at most
  // every 5s, with a trailing refresh so the last burst is never dropped
  const refreshSoon = useCoalescedRefetch(loadBoard, 5000)
  useWsEvent('new_message', refreshSoon)
  useWsEvent('conversations_synced', refreshSoon)
  useWsEvent('conversation_synced', refreshSoon)
  useWsEvent('resync', () => { loadBoard() })

  const openChat = (chat: Conversation) => navigate(conversationUrl(chat))

  const move = async (conversationId: number, toTagId: number | null) => {
    if (workspaceId == null || !board || board.board_id == null) return
    const wsId = workspaceId
    const boardId = board.board_id
    const ok = await mutate(
      'Move failed',
      () => moveBoardConversation(wsId, boardId, conversationId, toTagId),
      (prev) => applyMove(prev, conversationId, toTagId),
    )
    if (ok) refreshWorkspaces()
  }

  const onDropTo = (target: number | null) => (e: React.DragEvent) => {
    e.preventDefault()
    setDragOverCol(null)
    const raw = e.dataTransfer.getData('text/x-chat-id')
    if (raw) move(parseInt(raw, 10), target)
  }

  const startAddColumn = () => {
    setAddingColumn(true)
    setNewColumnName('')
  }

  const submitAddColumn = async (name: string) => {
    if (workspaceId == null || board?.board_id == null || !name.trim()) return
    const wsId = workspaceId
    const boardId = board.board_id
    if (await mutate('Couldn\'t add column', () => addBoardColumn(wsId, boardId, name.trim()))) {
      setAddingColumn(false)
    }
  }

  const shiftColumn = async (tagId: number, dir: -1 | 1) => {
    if (workspaceId == null || !board || board.board_id == null) return
    const idx = board.columns.findIndex((c) => c.tag_id === tagId)
    const target = idx + dir
    if (idx < 0 || target < 0 || target >= board.columns.length) return
    setColumnMenu(null)
    const order = board.columns.map((c) => c.tag_id)
    ;[order[idx], order[target]] = [order[target], order[idx]]
    const wsId = workspaceId
    const boardId = board.board_id
    // One atomic call; the server returns the reordered board
    await mutate(
      'Couldn\'t move column',
      () => reorderBoardColumns(wsId, boardId, order),
      (prev) => ({
        ...prev,
        columns: order
          .map((id) => prev.columns.find((c) => c.tag_id === id))
          .filter((c): c is BoardData['columns'][number] => c != null),
      }),
    )
  }

  const removeColumn = async (tagId: number) => {
    if (workspaceId == null || board?.board_id == null) return
    const col = board.columns.find((c) => c.tag_id === tagId)
    if (!col) return
    setColumnMenu(null)
    const wsId = workspaceId
    const boardId = board.board_id
    const n = col.conversations.length
    await confirm({
      title: `Delete column "${col.name}"?`,
      message: n > 0
        ? `${n} chat${n !== 1 ? 's' : ''} in this column return to the backlog. Their other tags are kept.`
        : 'The column is empty.',
      confirmLabel: 'Delete column',
      destructive: true,
      // Errors stay in the dialog; the board refreshes after success
      onConfirm: async () => {
        await deleteBoardColumn(wsId, boardId, tagId)
        loadBoardRef.current()
      },
    })
  }

  const submitRename = async (tagId: number) => {
    const value = renameValue.trim()
    const col = board?.columns.find((c) => c.tag_id === tagId)
    if (workspaceId == null || board?.board_id == null || !value || value === col?.name) { setRenamingCol(null); return }
    const wsId = workspaceId
    const boardId = board.board_id
    setRenamingCol(null)
    await mutate(
      'Rename failed',
      () => updateBoardColumn(wsId, boardId, tagId, { name: value }),
      (prev) => ({ ...prev, columns: prev.columns.map((c) => (c.tag_id === tagId ? { ...c, name: value } : c)) }),
    )
  }

  const submitAddBoard = async () => {
    if (workspaceId == null || !newBoardName.trim()) return
    try {
      const created = await addWorkspaceBoard(workspaceId, newBoardName.trim(), newBoardStream.trim() || undefined)
      setAddingBoard(false)
      setNewBoardName('')
      setNewBoardStream('')
      selectBoard(created.id)
    } catch (e) {
      reportError(e, 'Couldn\'t add board')
    }
  }

  const submitBoardRename = async (boardId: number) => {
    const value = boardRenameValue.trim()
    const current = board?.boards.find((b) => b.id === boardId)
    if (workspaceId == null || !value || value === current?.name) { setRenamingBoard(null); return }
    const wsId = workspaceId
    setRenamingBoard(null)
    await mutate(
      'Board rename failed',
      () => updateWorkspaceBoard(wsId, boardId, { name: value }),
      (prev) => ({ ...prev, boards: prev.boards.map((b) => (b.id === boardId ? { ...b, name: value } : b)) }),
    )
  }

  const submitBoardScope = async (boardId: number) => {
    if (workspaceId == null) { setScopingBoard(null); return }
    const wsId = workspaceId
    const v = scopeValue.trim()
    setScopingBoard(null)
    setBoardMenu(null)
    await mutate(
      'Couldn\'t set stream',
      () => updateWorkspaceBoard(wsId, boardId, v ? { scope_tag_name: v } : { clear_scope: true }),
    )
  }

  const removeBoard = async (boardId: number) => {
    if (workspaceId == null) return
    const target = board?.boards.find((b) => b.id === boardId)
    setBoardMenu(null)
    const wsId = workspaceId
    const ok = await confirm({
      title: `Delete board "${target?.name ?? ''}"?`,
      message: 'Its columns are removed and their cards return to the backlog. Workspace and stream tags are kept.',
      confirmLabel: 'Delete board',
      destructive: true,
      onConfirm: () => deleteWorkspaceBoard(wsId, boardId),
    })
    if (!ok || keyRef.current.workspaceId !== wsId) return
    if (activeBoardId === boardId) {
      storeBoardFor(wsId, null)
      setActiveBoardId(null) // reloads via loadBoard's deps
    } else {
      loadBoard()
    }
  }

  const activeBoardIsScoped = board?.boards.find((b) => b.id === board.board_id)?.scope_tag_id != null

  const backlogSections = useMemo(() => {
    if (!board) return []
    const q = backlogQuery.trim().toLowerCase()
    const classified = new Set(board.classified_conversation_ids)
    const f = (chats: Conversation[]) => {
      let out = chats
      // Unclassified triage view (only meaningful on unscoped boards —
      // scoped backlogs already contain a single stream)
      if (!activeBoardIsScoped && backlogFilter === 'unclassified') {
        out = out.filter((c) => !classified.has(c.id))
      }
      return q ? out.filter((c) => conversationTitle(c).toLowerCase().includes(q)) : out
    }
    return [
      { label: 'Active', hint: '< 7 days', chats: f(board.backlog.active) },
      { label: 'Cooling', hint: '7–30 days', chats: f(board.backlog.cooling) },
      { label: 'Dormant', hint: '> 30 days', chats: f(board.backlog.dormant) },
    ]
  }, [board, backlogQuery, backlogFilter, activeBoardIsScoped])

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
        <button
          onClick={() => setShowSettings(true)}
          title="Workspace settings (tags, name, view)"
          className="rounded-lg border border-slate-600 px-2 py-1 text-slate-400 transition-colors hover:text-slate-200"
        >
          <Settings2 size={13} />
        </button>
      </div>

      <WorkspaceDialog
        open={showSettings}
        workspace={activeWorkspace}
        onClose={() => setShowSettings(false)}
        onSaved={async (id, viewMode) => {
          setShowSettings(false)
          await refreshWorkspaces()
          if (id == null) navigate('/chats')            // workspace deleted
          else if (viewMode === 'list') navigate('/chats') // switched to list view
          else loadBoard()                                // tags/name may have changed
        }}
      />

      {/* Stream board tabs */}
      {board && (board.boards.length > 1 || addingBoard || board.boards.length === 1) && (
        <div className="flex items-center gap-1 border-b border-slate-700/50 px-4 py-1.5">
          {board.boards.map((b) => (
            <div key={b.id} className="relative">
              {renamingBoard === b.id ? (
                <input
                  autoFocus
                  value={boardRenameValue}
                  onChange={(e) => setBoardRenameValue(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') submitBoardRename(b.id); if (e.key === 'Escape') setRenamingBoard(null) }}
                  onBlur={() => submitBoardRename(b.id)}
                  className="w-28 rounded border border-indigo-500 bg-slate-700 px-1.5 py-0.5 text-xs text-gray-200 outline-none"
                />
              ) : (
                <button
                  onClick={() => selectBoard(b.id)}
                  onContextMenu={(e) => { e.preventDefault(); setBoardMenu((v) => (v === b.id ? null : b.id)) }}
                  title={b.scope_tag_name ? `Stream: ${b.scope_tag_name} (right-click to edit)` : 'All members (right-click to edit)'}
                  className={`flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                    board.board_id === b.id
                      ? 'bg-indigo-500/20 text-indigo-300'
                      : 'text-slate-400 hover:bg-slate-800 hover:text-slate-200'
                  }`}
                >
                  {b.name}
                  {b.unallocated_count > 0 && (
                    <span className="flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-amber-500/30 px-1 text-[9px] font-bold text-amber-300">
                      {b.unallocated_count}
                    </span>
                  )}
                </button>
              )}
              {boardMenu === b.id && (
                <div className="absolute left-0 top-8 z-30 w-48 rounded-lg border border-slate-600 bg-slate-800 py-1 text-xs shadow-xl">
                  <button
                    onClick={() => { setRenamingBoard(b.id); setBoardRenameValue(b.name); setBoardMenu(null) }}
                    className="block w-full px-3 py-1.5 text-left text-slate-300 hover:bg-slate-700/50"
                  >
                    Rename
                  </button>
                  {scopingBoard === b.id ? (
                    <div className="px-3 py-1.5">
                      <input
                        autoFocus
                        placeholder="Stream tag (empty = all)"
                        value={scopeValue}
                        onChange={(e) => setScopeValue(e.target.value)}
                        onKeyDown={(e) => { if (e.key === 'Enter') submitBoardScope(b.id); if (e.key === 'Escape') setScopingBoard(null) }}
                        className="w-full rounded border border-indigo-500 bg-slate-700 px-1.5 py-0.5 text-[11px] text-gray-200 outline-none"
                      />
                    </div>
                  ) : (
                    <button
                      onClick={() => { setScopingBoard(b.id); setScopeValue(b.scope_tag_name ?? '') }}
                      className="block w-full px-3 py-1.5 text-left text-slate-300 hover:bg-slate-700/50"
                    >
                      {b.scope_tag_name ? `Stream: ${b.scope_tag_name}` : 'Set stream…'}
                    </button>
                  )}
                  <button
                    onClick={() => removeBoard(b.id)}
                    className="block w-full px-3 py-1.5 text-left text-rose-400 hover:bg-slate-700/50"
                  >
                    Delete board
                  </button>
                </div>
              )}
            </div>
          ))}
          {addingBoard ? (
            <div className="flex items-center gap-1">
              <input
                autoFocus
                placeholder="Board name"
                value={newBoardName}
                onChange={(e) => setNewBoardName(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') submitAddBoard(); if (e.key === 'Escape') setAddingBoard(false) }}
                className="w-28 rounded border border-slate-600 bg-slate-700 px-1.5 py-0.5 text-xs text-gray-200 outline-none focus:border-indigo-500"
              />
              <input
                placeholder="Stream tag (optional)"
                value={newBoardStream}
                onChange={(e) => setNewBoardStream(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') submitAddBoard(); if (e.key === 'Escape') setAddingBoard(false) }}
                className="w-36 rounded border border-slate-600 bg-slate-700 px-1.5 py-0.5 text-xs text-gray-200 outline-none focus:border-indigo-500"
              />
              <button onClick={submitAddBoard} className="rounded bg-indigo-500 px-2 py-0.5 text-[11px] font-medium text-white hover:bg-indigo-400">Add</button>
              <button onClick={() => setAddingBoard(false)} className="px-1 text-[11px] text-slate-500 hover:text-slate-300">✕</button>
            </div>
          ) : (
            <button
              onClick={() => setAddingBoard(true)}
              title="New stream board"
              className="rounded-md px-2 py-1 text-xs text-slate-500 transition-colors hover:bg-slate-800 hover:text-slate-300"
            >
              <Plus size={13} />
            </button>
          )}
        </div>
      )}

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
            <div className="flex flex-col gap-1.5 border-b border-slate-700/50 p-2">
              <input
                type="text"
                placeholder="Filter backlog…"
                value={backlogQuery}
                onChange={(e) => setBacklogQuery(e.target.value)}
                className="w-full rounded-md border border-slate-600 bg-slate-700 px-2 py-1 text-xs text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500"
              />
              {!activeBoardIsScoped && (
                <div className="flex overflow-hidden rounded-md border border-slate-700 text-[10px]">
                  {(['unclassified', 'all'] as const).map((m) => (
                    <button
                      key={m}
                      onClick={() => setBacklogFilter(m)}
                      title={m === 'unclassified' ? 'Chats not yet assigned to any stream' : 'All workspace members'}
                      className={`flex-1 py-1 transition-colors ${
                        backlogFilter === m
                          ? 'bg-indigo-500/20 font-medium text-indigo-400'
                          : 'text-slate-500 hover:bg-slate-700/50 hover:text-slate-300'
                      }`}
                    >
                      {m === 'unclassified' ? 'Unclassified' : 'All'}
                    </button>
                  ))}
                </div>
              )}
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
                        key={chat.id}
                        draggable
                        onDragStart={(e) => {
                          e.dataTransfer.setData('text/x-chat-id', String(chat.id))
                          e.dataTransfer.effectAllowed = 'move'
                        }}
                        onClick={() => openChat(chat)}
                        className="flex cursor-pointer items-center gap-1.5 rounded-md px-1.5 py-1 hover:bg-slate-700/50"
                      >
                        <CardAvatar chat={chat} />
                        <span className="min-w-0 flex-1 truncate text-[11px] text-slate-300">{conversationTitle(chat)}</span>
                        <ChannelIcon channel={chat.channel} />
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
              {(() => {
                const meta = board.boards.find((b) => b.id === board.board_id)
                const empty = board.unallocated.length === 0 &&
                  board.columns.every((c) => c.conversations.length === 0) &&
                  Object.values(board.backlog).every((s) => s.length === 0)
                if (meta?.scope_tag_name && empty) {
                  return (
                    <p className="self-start rounded-lg border border-slate-700/60 bg-slate-800/40 px-3 py-2 text-xs text-slate-500">
                      No chats tagged "{meta.scope_tag_name}" yet — tag chats via the Intelligence
                      assistant or the chat info panel and they'll appear here.
                    </p>
                  )
                }
                return null
              })()}
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
                        key={chat.id}
                        chat={chat}
                        onOpen={() => openChat(chat)}
                        onDismiss={() => move(chat.id, null)}
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
                    <span className="rounded-full bg-slate-700 px-1.5 text-[10px] text-slate-400">{col.conversations.length}</span>
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
                        <button
                          onClick={() => removeColumn(col.tag_id)}
                          className="block w-full px-3 py-1.5 text-left text-rose-400 hover:bg-slate-700/50"
                        >
                          Delete column
                        </button>
                      </div>
                    )}
                  </div>
                  <div className="flex flex-1 flex-col gap-2 overflow-y-auto px-2.5 pb-2.5">
                    {col.conversations.map((chat) => (
                      <ChatCard key={chat.id} chat={chat} onOpen={() => openChat(chat)} />
                    ))}
                    {col.conversations.length === 0 && (
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
