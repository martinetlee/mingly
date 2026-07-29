/**
 * Regression tests for ?chatId= navigation into the Chats page.
 *
 * Guards two bugs from 2026-07:
 * 1. Messages vanishing: the ?chatId= param was re-parsed on every list reload,
 *    re-selecting the open chat and wiping its messages. Fix: params are
 *    consumed once and stripped from the URL.
 * 2. Palette clicks doing nothing while already on /chats: after fix (1), the
 *    nav processor only ran when the chat list changed, so in-page navigation
 *    (GlobalSearch → navigate('/chats?chatId=…')) never fired. Fix: navSeq
 *    counter deterministically triggers the processor.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useNavigate, useLocation } from 'react-router-dom'
import type { Chat } from '../../api/types'

// ── Mocks ──────────────────────────────────────────────────────────────────

const makeChat = (telegram_id: number, name: string): Chat => ({
  id: telegram_id,
  telegram_id,
  name,
  type: 'group',
  username: null,
  is_monitored: false,
  is_hidden: false,
  is_briefing: false,
  is_tracked: false,
  is_pinned: false,
  is_archived: false,
  is_muted: false,
  muted_until: null,
  is_marked_unread: false,
  unread_count: 0,
  last_synced_at: '2026-07-01T00:00:00Z',
  last_activity_at: '2026-07-01T00:00:00Z',
  last_message: null,
})

const chatAztec = makeChat(-100111, 'Aztec <> ETHTaipei')
const chatPolygon = makeChat(-100222, 'Polygon <> ETHTaipei 2024')

const getChats = vi.fn(async () => [chatAztec, chatPolygon])
const getMessages = vi.fn(async () => [])
const ensureChat = vi.fn(async (): Promise<Chat | null> => null)

vi.mock('../../api/client', () => {
  const resolve = (value: unknown = null) => vi.fn(async () => value)
  return {
    getChats: (...args: unknown[]) => getChats(...(args as [])),
    getMessages: (...args: unknown[]) => getMessages(...(args as [])),
    getFolders: resolve([]),
    getScamFlags: resolve({ flags: [], flagged_chat_ids: [] }),
    getTags: resolve([]),
    getChatsByIds: resolve([]),
    getHiddenChats: resolve([]),
    getArchivedChats: resolve([]),
    getPinnedMessage: resolve(null),
    getUserPresence: resolve(null),
    getContactByTelegram: resolve(null),
    getTopicMessages: resolve([]),
    getChatDateOffset: resolve({ offset: 0 }),
    searchChatMessages: resolve([]),
    searchChatsLocal: resolve({ local: [] }),
    searchChatsTelegram: resolve([]),
    searchMessagesGlobal: resolve([]),
    ensureChat: (...args: unknown[]) => ensureChat(...(args as [])),
    syncChat: resolve(null),
    sendMessage: resolve(null),
    editMessage: resolve(null),
    deleteMessages: resolve(null),
    forwardMessages: resolve(null),
    sendFiles: resolve(null),
    setHidden: resolve(null),
    setBriefing: resolve(null),
    setTracked: resolve(null),
    markChatRead: resolve(null),
    reactToMessage: resolve(null),
    setContactImportantByTelegram: resolve(null),
    setPinned: resolve(null),
    setArchived: resolve(null),
    setMuted: resolve(null),
    setMarkedUnread: resolve(null),
    sendTypingAction: resolve(null),
    pinMessage: resolve(null),
  }
})

vi.mock('../../hooks/useWebSocket', () => ({ useWebSocket: () => {} }))
vi.mock('../../hooks/useKeyboardShortcuts', () => ({ useKeyboardShortcuts: () => {} }))
vi.mock('../../context/WorkspaceContext', () => ({
  useWorkspaces: () => ({
    workspaces: [],
    activeWorkspace: null,
    loaded: true,
    setActiveWorkspaceId: () => {},
    refreshWorkspaces: async () => {},
  }),
}))
vi.mock('../../api/session', () => ({ authenticatedMediaUrl: (p: string) => p }))
vi.mock('../../components/MessageList', () => ({ default: () => <div data-testid="message-list" /> }))
vi.mock('../../components/ComposeBar', () => ({ default: () => null }))
vi.mock('../../components/ChatInfoPanel', () => ({ default: () => null }))
vi.mock('../../components/ForumTopicsPanel', () => ({ default: () => null }))
vi.mock('../../components/ChatPickerDialog', () => ({ default: () => null }))
vi.mock('../../components/DeleteConfirmDialog', () => ({ default: () => null }))
vi.mock('../../components/NewChatDialog', () => ({ default: () => null }))
vi.mock('../../components/ReportScamDialog', () => ({ default: () => null }))

import Chats from '../Chats'

// Captures router controls so tests can navigate like GlobalSearch does
const routerControls: { navigate: ReturnType<typeof useNavigate> | null; search: string } = {
  navigate: null,
  search: '',
}
function RouterProbe() {
  routerControls.navigate = useNavigate()
  routerControls.search = useLocation().search
  return null
}

function renderChats(initialEntry: string) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <RouterProbe />
      <Routes>
        <Route path="/chats" element={<Chats />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  getChats.mockClear()
  getChats.mockImplementation(async () => [chatAztec, chatPolygon])
  getMessages.mockClear()
  ensureChat.mockClear()
  ensureChat.mockImplementation(async () => null)
})

// ── Tests ──────────────────────────────────────────────────────────────────

describe('?chatId= navigation', () => {
  it('selects the chat when landing directly on /chats?chatId=…', async () => {
    renderChats(`/chats?chatId=${chatPolygon.telegram_id}`)

    await waitFor(() => {
      expect(getMessages).toHaveBeenCalledWith(
        chatPolygon.telegram_id, expect.anything(), expect.anything(), undefined,
      )
    })
    // Param must be consumed (stripped) so list reloads can't re-trigger selection
    expect(routerControls.search).toBe('')
  })

  it('switches chats when navigating in-page (palette click regression)', async () => {
    renderChats('/chats')
    await waitFor(() => expect(getChats).toHaveBeenCalled())
    expect(getMessages).not.toHaveBeenCalled()

    // Simulate GlobalSearch: navigate to ?chatId while ALREADY on /chats.
    // Pre-fix, the nav processor never fired here (no chats/loadChats change).
    act(() => {
      routerControls.navigate!(`/chats?chatId=${chatPolygon.telegram_id}`)
    })

    await waitFor(() => {
      expect(getMessages).toHaveBeenCalledWith(
        chatPolygon.telegram_id, expect.anything(), expect.anything(), undefined,
      )
    })
    expect(routerControls.search).toBe('')
  })

  it('navigating to a second chat switches selection again', async () => {
    renderChats('/chats')
    await waitFor(() => expect(getChats).toHaveBeenCalled())

    act(() => { routerControls.navigate!(`/chats?chatId=${chatAztec.telegram_id}`) })
    await waitFor(() => {
      expect(getMessages).toHaveBeenCalledWith(
        chatAztec.telegram_id, expect.anything(), expect.anything(), undefined,
      )
    })

    act(() => { routerControls.navigate!(`/chats?chatId=${chatPolygon.telegram_id}`) })
    await waitFor(() => {
      expect(getMessages).toHaveBeenCalledWith(
        chatPolygon.telegram_id, expect.anything(), expect.anything(), undefined,
      )
    })
  })

  // Watches every committed DOM frame for the empty-state text. Inspects
  // mutation records (not just final DOM) so a frame that is added and removed
  // between observer callbacks is still caught.
  const EMPTY_STATE = 'Select a chat to start'
  const watchFlash = (container: HTMLElement) => {
    const state = { flashed: false }
    const checkNow = () => {
      if (container.textContent?.includes(EMPTY_STATE)) state.flashed = true
    }
    const obs = new MutationObserver((records) => {
      for (const rec of records) {
        rec.addedNodes.forEach((node) => {
          if (node.textContent?.includes(EMPTY_STATE)) state.flashed = true
        })
      }
      checkNow()
    })
    obs.observe(container, { subtree: true, childList: true, characterData: true })
    checkNow()
    return {
      stop: () => { checkNow(); obs.disconnect(); return state.flashed },
    }
  }

  it('never flashes "Select a chat to start" while navigation is resolving', async () => {
    // Slow the list load so the nav processor has to wait, like production
    getChats.mockImplementation(async () => {
      await new Promise((r) => setTimeout(r, 15))
      return [chatAztec, chatPolygon]
    })
    const { container } = renderChats(`/chats?chatId=${chatPolygon.telegram_id}`)
    const watcher = watchFlash(container)

    await waitFor(() => expect(getMessages).toHaveBeenCalled())
    expect(watcher.stop()).toBe(false)
  })

  it('never flashes the empty state for a chat beyond page 1 (ensureChat path)', async () => {
    // The Board→chat case: target chat is NOT in the paginated list, so the
    // nav processor resolves it via ensureChat with real-world latency.
    const offPage = makeChat(-100999, 'Beyond Page One')
    getChats.mockImplementation(async () => {
      await new Promise((r) => setTimeout(r, 15))
      return [chatAztec, chatPolygon]
    })
    ensureChat.mockImplementation(async () => {
      await new Promise((r) => setTimeout(r, 60))
      return offPage
    })
    const { container } = renderChats(`/chats?chatId=${offPage.telegram_id}`)
    const watcher = watchFlash(container)

    // Regression (2026-07-26): reload the chat list WHILE ensureChat is still
    // in flight. The nav processor re-runs on every list identity change and
    // used to hit its "already retried — give up" branch, blanking the pane
    // ("Select a chat to start" flash) until ensureChat resolved.
    await waitFor(() => expect(ensureChat).toHaveBeenCalled())
    await act(async () => { fireEvent.click(screen.getByText('Unread')) })

    await waitFor(() => {
      expect(getMessages).toHaveBeenCalledWith(
        offPage.telegram_id, expect.anything(), expect.anything(), undefined,
      )
    })
    expect(watcher.stop()).toBe(false)
  })

  it('never flashes the empty state on in-page navigation (palette/board click)', async () => {
    const { container } = renderChats('/chats')
    await waitFor(() => expect(getChats).toHaveBeenCalled())

    // The empty state is legitimately visible now (nothing selected).
    // From the navigation onward it must disappear and never come back.
    act(() => {
      routerControls.navigate!(`/chats?chatId=${chatPolygon.telegram_id}`)
    })
    const watcher = watchFlash(container)
    await waitFor(() => expect(getMessages).toHaveBeenCalled())
    expect(watcher.stop()).toBe(false)
  })

  it('renders the chat list without wiping selection on list reload', async () => {
    renderChats(`/chats?chatId=${chatAztec.telegram_id}`)
    await waitFor(() => expect(getMessages).toHaveBeenCalled())
    const callsAfterSelect = getMessages.mock.calls.length

    // A bare re-navigation to /chats (no params) must not re-select or reload messages
    act(() => { routerControls.navigate!('/chats') })
    await waitFor(() => expect(screen.getAllByText(chatAztec.name).length).toBeGreaterThan(0))
    expect(getMessages.mock.calls.length).toBe(callsAfterSelect)
  })
})
