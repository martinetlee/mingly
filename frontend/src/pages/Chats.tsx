/**
 * Chats page: owns chat selection. Every way of opening a chat — list click,
 * j/k/arrow keys, ?chatId= URL (mentions, palette, notifications), Telegram
 * search result, message search result, avatar "Send message", new chat —
 * goes through the single selectChat(chat, {anchorMsgId, topic}) below, so
 * they all behave the same (mark read, unread separator, reset panes).
 *
 * Message state lives in ChatPane (keyed per chat) / useChatMessages; per-chat
 * transient UI (draft, reply, edit, upload progress, topic) in chatUiStore.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import type { VirtuosoHandle } from 'react-virtuoso'
import type { Chat, Tag } from '../api/types'
import type { MessageSearchResult } from '../api/client'
import {
  ensureChat, getChatsByIds, getFolders, getScamFlags, markChatRead, searchChatsTelegram, searchMessagesGlobal,
  setArchived, setBriefing, setHidden, setMarkedUnread, setMuted, setPinned, setTracked,
} from '../api/client'
import { useFeatures } from '../context/FeaturesContext'
import { useWorkspaces } from '../context/WorkspaceContext'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import { useWsEvent } from '../hooks/useWebSocket'
import { useLatestQuery } from '../hooks/useLatestQuery'
import { useDebouncedQuery } from '../hooks/useDebouncedQuery'
import { useCoalescedRefetch } from '../hooks/useCoalescedRefetch'
import { reportError } from '../lib/toast'
import ChatInfoPanel from '../components/ChatInfoPanel'
import NewChatDialog from '../components/NewChatDialog'
import ChatListPane from './chats/ChatListPane'
import type { ChatActions, SortBy } from './chats/ChatListPane'
import ChatPane from './chats/ChatPane'
import { useChatList } from './chats/useChatList'
import { getChatUi, updateChatUi, useChatUiSelect } from './chats/chatUiStore'

/** How long an open, focused chat waits before acknowledging new messages as read */
export const READ_ACK_MS = 1500

export interface SelectOptions {
  /** Open centred on this message (and highlight it) */
  anchorMsgId?: number | null
  /** Forum topic; undefined keeps the current one when re-selecting the same chat */
  topic?: { id: number; title: string } | null
  /** Keyboard navigation: keep focus in the chat list (don't focus the compose box) */
  viaKeyboard?: boolean
}

interface Selection {
  chat: Chat
  viaKeyboard: boolean
  anchorTgId: number | null
  unreadCount: number
  nonce: number
}

function isWindowFocused(): boolean {
  return document.visibilityState !== 'hidden' && document.hasFocus()
}

function parseNav(search: string): { chatId: number; msgId: number | null } | null {
  const params = new URLSearchParams(search)
  const chatId = parseInt(params.get('chatId') ?? '', 10)
  if (isNaN(chatId)) return null
  const msgId = parseInt(params.get('msgId') ?? '', 10)
  return { chatId, msgId: isNaN(msgId) || msgId === 0 ? null : msgId }
}

/** Move a chat to the top of the unpinned section with a new preview (mirrors the server's "recent" sort). */
function bumpChat(prev: Chat[], chatId: number, patch: Partial<Chat>): Chat[] {
  const idx = prev.findIndex((c) => c.telegram_id === chatId)
  if (idx === -1) return prev
  const updated = { ...prev[idx], ...patch }
  if (updated.is_pinned) {
    const next = [...prev]
    next[idx] = updated
    return next
  }
  const rest = prev.filter((_, i) => i !== idx)
  const firstUnpinned = rest.findIndex((c) => !c.is_pinned)
  const at = firstUnpinned === -1 ? rest.length : firstUnpinned
  return [...rest.slice(0, at), updated, ...rest.slice(at)]
}

export default function Chats() {
  const location = useLocation()
  const navigate = useNavigate()
  const { activeWorkspace } = useWorkspaces()
  const activeWorkspaceId = activeWorkspace?.id ?? null

  // ── List filters ───────────────────────────────────────────────────────────
  const [sortBy, setSortBy] = useState<SortBy>('recent')
  const [selectedTag, setSelectedTag] = useState<Tag | null>(null)
  const [selectedFolder, setSelectedFolder] = useState<number | null>(null)
  const [search, setSearch] = useState('')
  const list = useChatList({ sortBy, tagId: selectedTag?.id ?? null, workspaceId: activeWorkspaceId })
  const chatsRef = useRef(list.chats)
  chatsRef.current = list.chats

  const folders = useLatestQuery(['folders'], () => getFolders())
  const folder = selectedFolder == null ? null : (folders.data ?? []).find((f) => f.id === selectedFolder) ?? null
  // Folder members can live beyond the loaded pages: fetched by id, kept apart from the list
  const folderMembers = useLatestQuery(
    ['folder-members', folder?.id ?? null, folder?.chat_ids ?? []],
    () => getChatsByIds(folder!.chat_ids),
    { enabled: !!folder && folder.chat_ids.length > 0 },
  )

  const { shield } = useFeatures()
  const flaggedChats = useLatestQuery(
    ['scam-flagged-chats', shield],
    () => getScamFlags().then((r) => new Set(r.flagged_chat_ids)),
    { enabled: shield },
  )
  const refreshFlaggedChats = useCoalescedRefetch(flaggedChats.refetch, 3000)
  useWsEvent('scam_users_flagged', refreshFlaggedChats)
  useWsEvent('scam_verdict_changed', refreshFlaggedChats)

  // ── Selection ──────────────────────────────────────────────────────────────
  const [sel, setSel] = useState<Selection | null>(null)
  const selRef = useRef(sel)
  selRef.current = sel
  /** Bumped by every selection: async openers (nav, search) drop their result if the user moved on */
  const selGenRef = useRef(0)
  const nonceRef = useRef(0)
  const [showInfo, setShowInfo] = useState(false)
  const [showSearch, setShowSearch] = useState(false)
  const [showNewChat, setShowNewChat] = useState(false)
  const [pendingNav, setPendingNav] = useState<{ chatId: number; msgId: number | null } | null>(null)
  const selectedId = sel?.chat.telegram_id ?? null
  const topic = useChatUiSelect(selectedId, (u) => u.topic)

  const patchChat = useCallback((chatId: number, patch: Partial<Chat>) => {
    list.setChats((prev) => prev.map((c) => (c.telegram_id === chatId ? { ...c, ...patch } : c)))
    setSel((s) => (s && s.chat.telegram_id === chatId ? { ...s, chat: { ...s.chat, ...patch } } : s))
  }, [list.setChats]) // eslint-disable-line react-hooks/exhaustive-deps

  const markRead = useCallback((chat: Chat) => {
    const current = chatsRef.current.find((c) => c.telegram_id === chat.telegram_id) ?? chat
    if ((current.unread_count ?? 0) === 0 && !current.is_marked_unread) return
    patchChat(chat.telegram_id, { unread_count: 0, is_marked_unread: false })
    markChatRead(chat.telegram_id).catch((e) => reportError(e, 'Couldn’t mark chat as read'))
    if (current.is_marked_unread) {
      setMarkedUnread(chat.telegram_id, false).catch((e) => reportError(e, 'Couldn’t clear unread mark'))
    }
  }, [patchChat])

  const selectChat = useCallback((chat: Chat, opts: SelectOptions = {}) => {
    selGenRef.current++
    setPendingNav(null)
    const prev = selRef.current
    const sameChat = prev?.chat.telegram_id === chat.telegram_id
    if (sameChat && opts.anchorMsgId == null && opts.topic === undefined) {
      markRead(chat)
      return
    }
    const topicNext = opts.topic !== undefined ? opts.topic : sameChat ? getChatUi(chat.telegram_id).topic : null
    updateChatUi(chat.telegram_id, { topic: topicNext })
    const current = chatsRef.current.find((c) => c.telegram_id === chat.telegram_id)
    const unread = sameChat ? 0 : ((current ?? chat).unread_count ?? 0)
    setSel({ chat: current ?? chat, viaKeyboard: !!opts.viaKeyboard, anchorTgId: opts.anchorMsgId ?? null, unreadCount: unread, nonce: ++nonceRef.current })
    if (!sameChat) {
      setShowInfo(false)
      setShowSearch(false)
    }
    markRead(chat)
  }, [markRead])

  const deselect = useCallback((chatId: number) => {
    setSel((s) => (s && s.chat.telegram_id === chatId ? null : s))
  }, [])

  // Switching workspace closes the open chat (only an explicit switch — list
  // reloads never close it, even if the chat isn't on the first page)
  const prevWorkspaceRef = useRef(activeWorkspaceId)
  useEffect(() => {
    if (prevWorkspaceRef.current !== activeWorkspaceId) setSel(null)
    prevWorkspaceRef.current = activeWorkspaceId
  }, [activeWorkspaceId])

  // The freshest copy of the selected chat (list deltas update badges, flags, previews)
  const selectedChat = useMemo(() => {
    if (!sel) return null
    return list.chats.find((c) => c.telegram_id === sel.chat.telegram_id) ?? sel.chat
  }, [sel, list.chats])
  const selection = useMemo(
    () => (sel ? { anchorTgId: sel.anchorTgId, unreadCount: sel.unreadCount, nonce: sel.nonce, viaKeyboard: sel.viaKeyboard } : null),
    [sel],
  )

  // ── ?chatId=&msgId= navigation (mentions, palette, notifications) ─────────
  useEffect(() => {
    const nav = parseNav(location.search)
    if (!nav) return
    // Consume once: strip the params so list reloads can never re-arm it
    navigate(location.pathname, { replace: true })
    setPendingNav(nav)
  }, [location.search, location.pathname, navigate])

  useEffect(() => {
    if (!pendingNav) return
    const nav = pendingNav
    const found = chatsRef.current.find((c) => c.telegram_id === nav.chatId)
      ?? (selRef.current?.chat.telegram_id === nav.chatId ? selRef.current.chat : undefined)
    if (found) {
      selectChat(found, { anchorMsgId: nav.msgId })
      return
    }
    if (list.loading) return // wait for the first page before asking the server
    const gen = selGenRef.current
    let cancelled = false
    ensureChat(nav.chatId)
      .then((chat) => {
        if (cancelled || gen !== selGenRef.current) return
        list.setChats((prev) => (prev.some((c) => c.telegram_id === chat.telegram_id) ? prev : [chat, ...prev]))
        selectChat(chat, { anchorMsgId: nav.msgId })
      })
      .catch((e) => {
        if (cancelled || gen !== selGenRef.current) return
        setPendingNav(null)
        reportError(e, 'Couldn’t open this chat from Telegram')
      })
    return () => { cancelled = true }
  }, [pendingNav, list.loading]) // eslint-disable-line react-hooks/exhaustive-deps

  /** Open a chat that may not be in the loaded list yet (search results, avatar menu) */
  const openRemote = useCallback(async (telegramId: number, opts: SelectOptions = {}) => {
    const gen = selGenRef.current
    try {
      const chat = chatsRef.current.find((c) => c.telegram_id === telegramId) ?? await ensureChat(telegramId)
      if (gen !== selGenRef.current) return
      list.setChats((prev) => (prev.some((c) => c.telegram_id === chat.telegram_id) ? prev : [chat, ...prev]))
      selectChat(chat, opts)
    } catch (e) {
      if (gen === selGenRef.current) reportError(e, 'Couldn’t open this chat from Telegram')
    }
  }, [selectChat]) // eslint-disable-line react-hooks/exhaustive-deps

  // ── Read acknowledgement while the chat is open and the window focused ────
  const pendingAckRef = useRef<number | null>(null)
  const ackTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const scheduleAck = useCallback((chatId: number) => {
    pendingAckRef.current = chatId
    if (!isWindowFocused()) return // acknowledged when the window regains focus
    if (ackTimerRef.current) clearTimeout(ackTimerRef.current)
    ackTimerRef.current = setTimeout(() => {
      ackTimerRef.current = null
      const id = pendingAckRef.current
      if (id == null || selRef.current?.chat.telegram_id !== id || !isWindowFocused()) return
      pendingAckRef.current = null
      patchChat(id, { unread_count: 0 })
      markChatRead(id).catch((e) => reportError(e, 'Couldn’t mark chat as read'))
    }, READ_ACK_MS)
  }, [patchChat])
  useEffect(() => {
    const onFocus = () => {
      const id = pendingAckRef.current
      if (id != null && selRef.current?.chat.telegram_id === id) scheduleAck(id)
    }
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onFocus)
    return () => {
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onFocus)
      if (ackTimerRef.current) clearTimeout(ackTimerRef.current)
    }
  }, [scheduleAck])

  // ── Live list updates ──────────────────────────────────────────────────────
  const reloadSoon = useCoalescedRefetch(list.reload, 2000)
  useWsEvent('new_message', (e) => {
    const viewing = selRef.current?.chat.telegram_id === e.chat_id
    const known = chatsRef.current.some((c) => c.telegram_id === e.chat_id)
    if (!known) {
      // A chat outside the loaded pages (or brand new): one coalesced refetch
      reloadSoon()
      return
    }
    const msg = e.message
    const date = msg?.date ?? new Date().toISOString()
    const incoming = !e.is_outgoing
    const countAsUnread = incoming && !(viewing && isWindowFocused())
    list.setChats((prev) => {
      const cur = prev.find((c) => c.telegram_id === e.chat_id)
      return bumpChat(prev, e.chat_id, {
        last_message: { text: e.text ?? msg?.text ?? '', sender_name: e.sender_name ?? null, date },
        last_activity_at: date,
        unread_count: countAsUnread ? (cur?.unread_count ?? 0) + 1 : cur?.unread_count,
      })
    })
    if (viewing && incoming) scheduleAck(e.chat_id)
  })
  useWsEvent('chats_synced', reloadSoon)
  useWsEvent('chat_synced', reloadSoon)
  useWsEvent('bulk_sync_complete', reloadSoon)
  useWsEvent('resync', () => {
    reloadSoon()
    void folders.refetch()
    if (shield) void flaggedChats.refetch()
  })

  const onChatActivity = useCallback((chatId: number, preview: { text: string; date: string }) => {
    list.setChats((prev) => bumpChat(prev, chatId, {
      last_message: { text: preview.text, sender_name: null, date: preview.date },
      last_activity_at: preview.date,
    }))
  }, [list.setChats]) // eslint-disable-line react-hooks/exhaustive-deps

  // ── Chat actions (context menu / row buttons) ─────────────────────────────
  const act = useCallback(async (label: string, fn: () => Promise<unknown>): Promise<boolean> => {
    try {
      await fn()
      return true
    } catch (e) {
      reportError(e, label)
      return false
    }
  }, [])
  const actions: ChatActions = useMemo(() => ({
    toggleTrack: (chat) => act('Couldn’t change tracking', async () => {
      const r = await setTracked(chat.telegram_id, !chat.is_tracked)
      patchChat(chat.telegram_id, { is_tracked: r.is_tracked })
    }),
    hide: (chat) => act('Couldn’t hide chat', async () => {
      await setHidden(chat.telegram_id, true)
      list.setChats((prev) => prev.filter((c) => c.telegram_id !== chat.telegram_id))
      deselect(chat.telegram_id)
    }),
    unhide: (chat) => act('Couldn’t unhide chat', async () => {
      await setHidden(chat.telegram_id, false)
      void list.reload()
    }),
    briefing: (chat) => act('Couldn’t add to briefing', async () => {
      await setBriefing(chat.telegram_id, true)
      list.setChats((prev) => prev.filter((c) => c.telegram_id !== chat.telegram_id))
      deselect(chat.telegram_id)
    }),
    pin: (chat) => act(chat.is_pinned ? 'Couldn’t unpin chat' : 'Couldn’t pin chat', async () => {
      const r = await setPinned(chat.telegram_id, !chat.is_pinned)
      patchChat(chat.telegram_id, { is_pinned: r.is_pinned })
    }),
    archive: (chat) => act(chat.is_archived ? 'Couldn’t unarchive chat' : 'Couldn’t archive chat', async () => {
      const archived = !chat.is_archived
      await setArchived(chat.telegram_id, archived)
      if (archived) {
        list.setChats((prev) => prev.filter((c) => c.telegram_id !== chat.telegram_id))
        deselect(chat.telegram_id)
      } else {
        void list.reload()
      }
    }),
    mute: (chat, duration) => act('Couldn’t change mute', async () => {
      const muted = !chat.is_muted || !!duration
      const r = await setMuted(chat.telegram_id, muted, duration)
      patchChat(chat.telegram_id, { is_muted: r.is_muted, muted_until: r.muted_until })
    }),
    markUnread: (chat) => act('Couldn’t change unread mark', async () => {
      const r = await setMarkedUnread(chat.telegram_id, !chat.is_marked_unread)
      patchChat(chat.telegram_id, { is_marked_unread: r.is_marked_unread })
    }),
  }), [act, patchChat, deselect, list.setChats, list.reload]) // eslint-disable-line react-hooks/exhaustive-deps

  // ── Visible list ───────────────────────────────────────────────────────────
  const visible = useMemo(() => {
    let pool = list.chats
    if (folder) {
      // Union loaded chats with separately fetched folder members (prefer the
      // list copy: it carries fresher unread/last-message data)
      const loadedIds = new Set(pool.map((c) => c.telegram_id))
      pool = [...pool, ...(folderMembers.data ?? []).filter((m) => !loadedIds.has(m.telegram_id))]
        .filter((c) => folder.chat_ids.includes(c.telegram_id)
          || (folder.include_groups && c.type === 'group')
          || (folder.include_channels && c.type === 'channel')
          || (folder.include_contacts && c.type === 'private'))
    }
    const q = search.trim().toLowerCase()
    if (q) pool = pool.filter((c) => c.name.toLowerCase().includes(q) || (c.username ?? '').toLowerCase().includes(q))
    return [...pool].sort((a, b) => {
      if (a.is_pinned !== b.is_pinned) return a.is_pinned ? -1 : 1
      const da = a.last_activity_at ?? ''
      const db = b.last_activity_at ?? ''
      if (sortBy === 'unread') {
        const d = (b.unread_count ?? 0) - (a.unread_count ?? 0)
        return d !== 0 ? d : db.localeCompare(da)
      }
      if (sortBy === 'alpha') return a.name.localeCompare(b.name)
      return da !== db ? db.localeCompare(da) : a.name.localeCompare(b.name)
    })
  }, [list.chats, folder, folderMembers.data, search, sortBy])
  const visibleRef = useRef(visible)
  visibleRef.current = visible

  // ── Search: Telegram directory + message content ──────────────────────────
  const tgSearch = useDebouncedQuery(search, (q, signal) => searchChatsTelegram(q, signal), { delayMs: 300 })
  const telegramResults = useMemo(() => {
    const shown = new Set(visible.map((c) => c.telegram_id))
    return (tgSearch.data ?? []).filter((c) => !shown.has(c.telegram_id))
  }, [tgSearch.data, visible])
  // Latest-only: a new query aborts the one in flight (never silently ignored)
  const [messageResults, setMessageResults] = useState<{ q: string; results: MessageSearchResult[] } | null>(null)
  const [messageSearchPending, setMessageSearchPending] = useState<string | null>(null)
  const messageSearchCtl = useRef<AbortController | null>(null)
  const runMessageSearch = useCallback(async (q: string) => {
    messageSearchCtl.current?.abort()
    const ctl = new AbortController()
    messageSearchCtl.current = ctl
    setMessageSearchPending(q)
    try {
      const results = await searchMessagesGlobal(q, ctl.signal)
      if (messageSearchCtl.current === ctl) setMessageResults({ q, results })
    } catch (e) {
      if (messageSearchCtl.current === ctl) reportError(e, 'Message search failed')
    } finally {
      if (messageSearchCtl.current === ctl) {
        messageSearchCtl.current = null
        setMessageSearchPending(null)
      }
    }
  }, [])
  useEffect(() => () => messageSearchCtl.current?.abort(), [])
  const currentMessageResults = messageResults && messageResults.q === search.trim() ? messageResults.results : null

  // ── Keyboard ───────────────────────────────────────────────────────────────
  const chatListRef = useRef<HTMLDivElement>(null)
  const chatListVirtuosoRef = useRef<VirtuosoHandle>(null)
  const searchInputRef = useRef<HTMLInputElement>(null)
  const moveSelection = (delta: 1 | -1) => {
    const items = visibleRef.current
    const cur = selRef.current?.chat.telegram_id
    const idx = cur == null ? -1 : items.findIndex((c) => c.telegram_id === cur)
    const next = idx === -1 ? (delta > 0 ? items[0] : undefined) : items[idx + delta]
    if (!next) return
    // Focus follows the selection in the list, so the next j/k/arrow keeps
    // navigating. The list is virtualized: bring the row into view first.
    chatListVirtuosoRef.current?.scrollIntoView({ index: items.indexOf(next) })
    const findRow = () => chatListRef.current?.querySelector<HTMLElement>(`[data-chat-id="${next.telegram_id}"] [data-chat-row]`)
    const row = findRow()
    if (row) row.focus()
    else requestAnimationFrame(() => findRow()?.focus())
    selectChat(next, { viaKeyboard: true })
  }
  const listHasFocus = () => !!chatListRef.current && chatListRef.current.contains(document.activeElement)
  useKeyboardShortcuts({
    j: () => moveSelection(1),
    k: () => moveSelection(-1),
    '/': () => searchInputRef.current?.focus(),
    i: () => { if (!selRef.current) return false; setShowInfo((v) => !v) },
    'mod+f': () => { if (!selRef.current) return false; setShowSearch((v) => !v) },
    n: () => setShowNewChat(true),
  })
  // Arrow keys move through the list only while the list has focus; elsewhere they scroll
  useKeyboardShortcuts({
    arrowdown: () => { if (!listHasFocus()) return false; moveSelection(1) },
    arrowup: () => { if (!listHasFocus()) return false; moveSelection(-1) },
  }, { yieldScrollKeys: false })

  // ── Resizable list ─────────────────────────────────────────────────────────
  const [sidebarWidth, setSidebarWidth] = useState(288)
  const onResizeStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    const startX = e.clientX
    const startWidth = sidebarWidth
    const onMove = (ev: MouseEvent) => setSidebarWidth(Math.min(480, Math.max(200, startWidth + ev.clientX - startX)))
    const onUp = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }, [sidebarWidth])

  const onSelectTopic = useCallback((t: { id: number; title: string } | null) => {
    const cur = selRef.current
    if (cur) selectChat(cur.chat, { topic: t })
  }, [selectChat])
  const onToggleInfo = useCallback(() => setShowInfo((v) => !v), [])
  const onSearchMessages = useCallback(() => { const q = search.trim(); if (q) void runMessageSearch(q) }, [search, runMessageSearch])
  const onOpenSearchResult = useCallback((chat: Chat) => {
    setSearch('')
    void openRemote(chat.telegram_id)
  }, [openRemote])
  const onOpenMessageResult = useCallback((r: MessageSearchResult) => {
    void openRemote(r.telegram_id, { anchorMsgId: r.messages[0]?.tg_message_id ?? null })
  }, [openRemote])
  const handleChatCreated = useCallback((chat: Chat) => {
    setShowNewChat(false)
    list.setChats((prev) => (prev.some((c) => c.telegram_id === chat.telegram_id) ? prev : [chat, ...prev]))
    selectChat(chat)
  }, [selectChat]) // eslint-disable-line react-hooks/exhaustive-deps

  const navigating = !sel && (pendingNav != null || parseNav(location.search) != null)

  return (
    <div className="flex flex-1 overflow-hidden">
      <ChatListPane
        width={sidebarWidth}
        onResizeStart={onResizeStart}
        listRef={chatListRef}
        virtuosoRef={chatListVirtuosoRef}
        searchInputRef={searchInputRef}
        folders={folders.data ?? []}
        selectedFolder={selectedFolder}
        onSelectFolder={setSelectedFolder}
        search={search}
        onSearchChange={setSearch}
        telegramResults={telegramResults}
        searchingTelegram={tgSearch.pending}
        messageResults={currentMessageResults}
        searchingMessages={messageSearchPending != null && messageSearchPending === search.trim()}
        onSearchMessages={onSearchMessages}
        onOpenSearchResult={onOpenSearchResult}
        onOpenMessageResult={onOpenMessageResult}
        sortBy={sortBy}
        onSortBy={setSortBy}
        selectedTag={selectedTag}
        onSelectTag={setSelectedTag}
        boardButton={activeWorkspace?.view_mode === 'kanban' ? { title: `${activeWorkspace.name} board`, onClick: () => navigate('/board') } : null}
        onNewChat={() => setShowNewChat(true)}
        chats={visible}
        loading={list.loading}
        loadingMore={list.loadingMore}
        onLoadMore={list.loadMore}
        selectedId={selectedId}
        flaggedChatIds={(shield && flaggedChats.data) || EMPTY_SET}
        onSelect={selectChat}
        actions={actions}
      />

      <div className="flex flex-1 overflow-hidden">
        {selectedChat && selection ? (
          <ChatPane
            key={selectedChat.telegram_id}
            chat={selectedChat}
            topic={topic}
            selection={selection}
            showInfo={showInfo}
            onToggleInfo={onToggleInfo}
            showSearch={showSearch}
            onShowSearch={setShowSearch}
            onSelectTopic={onSelectTopic}
            onOpenChat={selectChat}
            onChatActivity={onChatActivity}
          />
        ) : (
          <div className="flex flex-1 flex-col items-center justify-center bg-slate-900 text-slate-500">
            {navigating ? (
              <>
                <Loader2 size={20} className="animate-spin text-slate-400 mb-2" />
                <p className="text-sm text-slate-400">Loading chat…</p>
              </>
            ) : (
              <p className="text-sm">Select a chat to start</p>
            )}
          </div>
        )}

        {showInfo && selectedChat && (
          <ChatInfoPanel key={selectedChat.telegram_id} chat={selectedChat} onClose={onToggleInfo} />
        )}
      </div>

      <NewChatDialog open={showNewChat} onClose={() => setShowNewChat(false)} onChatCreated={handleChatCreated} />
    </div>
  )
}

const EMPTY_SET: Set<number> = new Set()
