import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, cleanup, within, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { BoardData, Chat, Workspace } from '../../api/types'
import { ConfirmProvider } from '../../components/ui/ConfirmDialog'
import { resetToasts } from '../../lib/toast'

function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const api = vi.hoisted(() => ({
  getBoard: vi.fn(),
  moveBoardChat: vi.fn(),
  reorderBoardColumns: vi.fn(),
}))
const ws = vi.hoisted(() => ({
  // Latest handler per type, like the real useWsEvent (one subscription that
  // reads the current handler through a ref) — no duplicate triggers
  handlers: new Map<string, (e: unknown) => void>(),
  activeWorkspace: null as Workspace | null,
  refreshWorkspaces: vi.fn(async () => {}),
}))

vi.mock('../../api/client', () => {
  const fwd = (k: keyof typeof api) => (...a: unknown[]) => api[k](...a)
  const noop = vi.fn(async () => ({}))
  return {
    getBoard: fwd('getBoard'),
    moveBoardChat: fwd('moveBoardChat'),
    reorderBoardColumns: fwd('reorderBoardColumns'),
    addBoardColumn: noop, addWorkspaceBoard: noop, deleteBoardColumn: noop, deleteWorkspaceBoard: noop,
    updateBoardColumn: noop, updateWorkspaceBoard: noop, getTags: vi.fn(async () => []),
    createWorkspace: noop, updateWorkspace: noop, deleteWorkspace: noop,
  }
})
vi.mock('../../hooks/useWebSocket', () => ({
  useWsEvent: (type: string, handler: (e: unknown) => void) => { ws.handlers.set(type, handler) },
  useWebSocket: () => {},
}))
vi.mock('../../context/WorkspaceContext', async (orig) => ({
  ...(await orig<typeof import('../../context/WorkspaceContext')>()),
  useWorkspaces: () => ({
    workspaces: ws.activeWorkspace ? [ws.activeWorkspace] : [],
    activeWorkspace: ws.activeWorkspace,
    loaded: true,
    setActiveWorkspaceId: () => {},
    refreshWorkspaces: ws.refreshWorkspaces,
  }),
}))

import Board from '../Board'

const chat = (telegram_id: number, name: string): Chat => ({
  id: telegram_id, telegram_id, name, type: 'group', username: null, unread_count: 0,
  last_activity_at: new Date().toISOString(), last_message: null,
} as unknown as Chat)

const workspace = (id: number, name: string): Workspace => ({
  id, name, emoji: null, color: 'indigo', match_mode: 'any', view_mode: 'kanban', position: 0,
  tag_ids: [1], member_count: 1, unallocated_count: 0, main_board_id: id * 10,
})

function boardData(wsId: number, opts: { inColumn: boolean; columnName?: string }): BoardData {
  const alice = chat(5, 'Alice chat')
  return {
    workspace: { id: wsId, name: `WS${wsId}`, emoji: null, color: 'indigo', view_mode: 'kanban' },
    boards: [{ id: wsId * 10, name: 'Main', scope_tag_id: null, scope_tag_name: null, position: 0, unallocated_count: 0 }],
    board_id: wsId * 10,
    columns: [{ tag_id: 100, name: opts.columnName ?? 'Doing', color: 'indigo', position: 0, chats: opts.inColumn ? [alice] : [] }],
    unallocated: [],
    backlog: { active: opts.inColumn ? [] : [alice], cooling: [], dormant: [] },
    classified_chat_ids: [],
  }
}

const column = (name: string) => screen.getByText(name).closest('.w-64') as HTMLElement

function renderBoard() {
  return render(
    <MemoryRouter initialEntries={['/board']}>
      <ConfirmProvider><Board /></ConfirmProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  resetToasts()
  ws.handlers.clear()
  ws.activeWorkspace = workspace(1, 'WS one')
  try { localStorage.clear() } catch { /* jsdom */ }
})
afterEach(() => cleanup())

describe('Board', () => {
  it('a board read issued before a move cannot snap the card back', async () => {
    api.getBoard.mockResolvedValueOnce(boardData(1, { inColumn: false }))
    renderBoard()
    await screen.findByText('Doing')

    // WS activity starts a refresh that will return the pre-move board
    const staleRead = deferred<BoardData>()
    api.getBoard.mockReturnValueOnce(staleRead.promise)
    act(() => { ws.handlers.get('new_message')!({ type: 'new_message', chat_id: 5 }) })
    await waitFor(() => expect(api.getBoard).toHaveBeenCalledTimes(2))

    const move = deferred<BoardData>()
    api.moveBoardChat.mockReturnValueOnce(move.promise)
    fireEvent.drop(column('Doing'), { dataTransfer: { getData: () => '5' } })
    expect(within(column('Doing')).queryByText('Alice chat')).not.toBeNull() // optimistic

    await act(async () => { staleRead.resolve(boardData(1, { inColumn: false })) })
    expect(within(column('Doing')).queryByText('Alice chat')).not.toBeNull()

    // The server's committed board replaces local state
    await act(async () => { move.resolve(boardData(1, { inColumn: true, columnName: 'Doing' })) })
    expect(within(column('Doing')).queryByText('Alice chat')).not.toBeNull()
    expect(api.moveBoardChat).toHaveBeenCalledWith(1, 10, 5, 100)
  })

  it('a failed move resyncs from the server', async () => {
    api.getBoard.mockResolvedValueOnce(boardData(1, { inColumn: false }))
    renderBoard()
    await screen.findByText('Doing')
    api.moveBoardChat.mockRejectedValueOnce(new Error('boom'))
    api.getBoard.mockResolvedValueOnce(boardData(1, { inColumn: false }))
    await act(async () => { fireEvent.drop(column('Doing'), { dataTransfer: { getData: () => '5' } }) })
    await waitFor(() => expect(api.getBoard).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(within(column('Doing')).queryByText('Alice chat')).toBeNull())
  })

  it("switching workspace clears the previous workspace's board immediately", async () => {
    api.getBoard.mockResolvedValueOnce(boardData(1, { inColumn: false, columnName: 'Old column' }))
    const view = renderBoard()
    await screen.findByText('Old column')
    const next = deferred<BoardData>()
    api.getBoard.mockReturnValueOnce(next.promise)
    ws.activeWorkspace = workspace(2, 'WS two')
    view.rerender(
      <MemoryRouter initialEntries={['/board']}>
        <ConfirmProvider><Board /></ConfirmProvider>
      </MemoryRouter>,
    )
    expect(screen.queryByText('Old column')).toBeNull()
    expect(api.getBoard).toHaveBeenLastCalledWith(2, undefined)
    await act(async () => { next.resolve(boardData(2, { inColumn: false, columnName: 'New column' })) })
    expect(screen.getByText('New column')).not.toBeNull()
  })

  it('switching workspace while a move is saving still loads the new workspace', async () => {
    api.getBoard.mockResolvedValueOnce(boardData(1, { inColumn: false, columnName: 'Old column' }))
    const view = renderBoard()
    await screen.findByText('Old column')
    const move = deferred<BoardData>()
    api.moveBoardChat.mockReturnValueOnce(move.promise)
    fireEvent.drop(column('Old column'), { dataTransfer: { getData: () => '5' } })

    // Switch while the move is in flight
    api.getBoard.mockResolvedValueOnce(boardData(2, { inColumn: false, columnName: 'New column' }))
    ws.activeWorkspace = workspace(2, 'WS two')
    view.rerender(
      <MemoryRouter initialEntries={['/board']}>
        <ConfirmProvider><Board /></ConfirmProvider>
      </MemoryRouter>,
    )
    await act(async () => { move.resolve(boardData(1, { inColumn: true, columnName: 'Old column' })) })
    expect(await screen.findByText('New column')).not.toBeNull()
    expect(screen.queryByText('Old column')).toBeNull()
    expect(api.getBoard).toHaveBeenLastCalledWith(2, undefined)
  })

  it('reorders columns with one atomic call', async () => {
    const data = boardData(1, { inColumn: false })
    data.columns.push({ tag_id: 101, name: 'Done', color: 'indigo', position: 1, chats: [] })
    api.getBoard.mockResolvedValueOnce(data)
    api.reorderBoardColumns.mockResolvedValueOnce({ ...data, columns: [data.columns[1], data.columns[0]] })
    renderBoard()
    await screen.findByText('Done')
    const menuBtn = within(column('Done')).getAllByRole('button')[0]
    fireEvent.click(menuBtn)
    await act(async () => { fireEvent.click(screen.getByText('Move left')) })
    expect(api.reorderBoardColumns).toHaveBeenCalledWith(1, 10, [101, 100])
  })
})
