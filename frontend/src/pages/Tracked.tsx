import { useState, useCallback, useRef } from 'react'
import { RefreshCw, Bookmark, X, Info } from 'lucide-react'
import type { Chat } from '../api/types'
import { getTrackedChats, setTracked, syncChat, sendMessage } from '../api/client'
import { useWsEvent } from '../hooks/useWebSocket'
import { useLatestQuery } from '../hooks/useLatestQuery'
import { useCoalescedRefetch } from '../hooks/useCoalescedRefetch'
import { useChatMessages } from '../hooks/useChatMessages'
import { reportError, errorMessage } from '../lib/toast'
import { fmtDate } from '../utils/time'
import ChatMessagesView from '../components/ChatMessagesView'
import ComposeBar from '../components/ComposeBar'
import ChatInfoPanel from '../components/ChatInfoPanel'


export default function Tracked() {
  const { data: chatsData, error: chatsError, loading: chatsLoading, refetch: refetchChats, setData: setChats } =
    useLatestQuery(['tracked-chats'], () => getTrackedChats())
  const chats = chatsData ?? []
  const [selectedChat, setSelectedChat] = useState<Chat | null>(null)
  const [syncing, setSyncing] = useState<number | null>(null)
  const [showInfo, setShowInfo] = useState(false)
  const [search, setSearch] = useState('')
  const [sidebarWidth, setSidebarWidth] = useState(288)
  const dragging = useRef(false)

  // Messages are bound to the selected chat: a late response for a previous
  // chat can never render here (or be replied to via Compose)
  const selectedId = selectedChat?.telegram_id ?? null
  // Same message-window engine as the Chats pane (paging, backfill, live WS updates)
  const pane = useChatMessages({ chatId: selectedId, topicId: null, nonce: 0 })
  const selectedRef = useRef(selectedChat)
  selectedRef.current = selectedChat

  // The list refreshes at most every 3s however many messages arrive
  const refreshList = useCoalescedRefetch(refetchChats, 3000)
  // The open chat's messages update themselves (useChatMessages); only the list is refreshed here
  useWsEvent('new_message', refreshList)
  useWsEvent('chats_synced', refreshList)
  useWsEvent('resync', refreshList)

  const handleSelectChat = (chat: Chat) => {
    if (selectedRef.current?.telegram_id === chat.telegram_id) return
    setSelectedChat(chat)
    setShowInfo(false)
  }

  const handleSync = async () => {
    const chat = selectedRef.current
    if (!chat || syncing === chat.telegram_id) return
    setSyncing(chat.telegram_id)
    try {
      await syncChat(chat.telegram_id)
      if (selectedRef.current?.telegram_id === chat.telegram_id) await pane.refresh()
    } catch (e) {
      reportError(e, 'Sync failed')
    } finally {
      setSyncing((v) => (v === chat.telegram_id ? null : v))
    }
  }

  const handleUntrack = async (chat: Chat) => {
    try {
      await setTracked(chat.telegram_id, false)
    } catch (e) {
      reportError(e, 'Untrack failed')
      return
    }
    setChats((prev) => prev?.filter((c) => c.telegram_id !== chat.telegram_id))
    setSelectedChat((cur) => (cur?.telegram_id === chat.telegram_id ? null : cur))
  }

  const handleSend = async (text: string) => {
    const chat = selectedRef.current
    if (!chat) return
    const res = await sendMessage(chat.telegram_id, text)
    if (res?.message) pane.applyMessage(res.message)
    else if (selectedRef.current?.telegram_id === chat.telegram_id) await pane.refresh()
  }


  const onDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    dragging.current = true
    const startX = e.clientX
    const startWidth = sidebarWidth
    const onMove = (e: MouseEvent) => {
      if (!dragging.current) return
      setSidebarWidth(Math.min(480, Math.max(200, startWidth + e.clientX - startX)))
    }
    const onUp = () => {
      dragging.current = false
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }, [sidebarWidth])

  const filtered = chats.filter((c) =>
    c.name.toLowerCase().includes(search.toLowerCase())
  )

  return (
    <div className="flex flex-1 overflow-hidden">
      {/* Chat list */}
      <div className="flex flex-shrink-0 flex-col border-r border-slate-700/50 bg-slate-800 relative" style={{ width: sidebarWidth }}>
        <div
          onMouseDown={onDragStart}
          className="absolute right-0 top-0 h-full w-1 cursor-col-resize hover:bg-indigo-500 transition-colors z-10"
        />
        <div className="flex items-center gap-2 border-b border-slate-700/50 px-3 py-3">
          <Bookmark size={16} className="text-amber-500 flex-shrink-0" fill="currentColor" />
          <span className="text-sm font-semibold text-gray-200">Tracked</span>
        </div>
        <div className="border-b border-slate-700/50 px-3 py-2">
          <input
            type="text"
            placeholder="Search…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-sm text-gray-200 placeholder:text-slate-500 outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500/30"
          />
        </div>
        <div className="flex-1 overflow-y-auto">
          {chatsLoading ? (
            <p className="px-4 py-6 text-center text-sm text-slate-500">Loading…</p>
          ) : chatsError != null && chats.length === 0 ? (
            <div className="px-4 py-6 text-center">
              <p className="text-sm text-rose-400">Couldn't load tracked chats: {errorMessage(chatsError)}</p>
              <button onClick={() => refetchChats()} className="mt-2 text-xs text-indigo-400 hover:text-indigo-300">Retry</button>
            </div>
          ) : filtered.length === 0 ? (
            <p className="px-4 py-6 text-center text-sm text-slate-500">No tracked chats</p>
          ) : (
            filtered.map((chat) => (
              <div
                key={chat.telegram_id}
                onClick={() => handleSelectChat(chat)}
                className={`group flex cursor-pointer items-center gap-3 px-3 py-2.5 transition-colors ${
                  selectedChat?.telegram_id === chat.telegram_id ? 'bg-amber-500/15' : 'hover:bg-slate-700/50'
                }`}
              >
                <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full bg-amber-500/20 text-sm font-medium text-amber-400">
                  {chat.name.charAt(0).toUpperCase()}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <p title={chat.name} className={`truncate text-sm font-medium ${selectedChat?.telegram_id === chat.telegram_id ? 'text-amber-300' : 'text-gray-100'}`}>
                      {chat.name}
                    </p>
                    {chat.last_message?.date && (
                      <span className="ml-auto flex-shrink-0 text-[10px] text-slate-500">
                        {fmtDate(chat.last_message.date)}
                      </span>
                    )}
                  </div>
                  {chat.last_message ? (
                    <p className="truncate text-xs text-slate-500 mt-0.5">
                      {chat.last_message.sender_name && (
                        <span className="text-slate-400">{chat.last_message.sender_name}: </span>
                      )}
                      {chat.last_message.text || '[media]'}
                    </p>
                  ) : (
                    <span className="text-[10px] text-slate-500">{chat.type}</span>
                  )}
                </div>
                {(chat.unread_count ?? 0) > 0 && (
                  <span className="min-w-[18px] rounded-full bg-indigo-500 px-1.5 py-0.5 text-center text-[10px] font-semibold leading-tight text-white group-hover:hidden">
                    {(chat.unread_count ?? 0) > 99 ? '99+' : chat.unread_count}
                  </span>
                )}
                <button
                  onClick={(e) => { e.stopPropagation(); handleUntrack(chat) }}
                  title="Untrack"
                  className="hidden group-hover:flex rounded p-1 text-slate-500 hover:text-slate-300 transition-colors"
                >
                  <X size={14} />
                </button>
              </div>
            ))
          )}
        </div>
      </div>

      {/* Message area */}
      <div className="flex flex-1 flex-col overflow-hidden bg-slate-900">
        {selectedChat ? (
          <>
            <div className="flex items-center justify-between border-b border-slate-700/50 bg-slate-800 px-4 py-3">
              <div>
                <h2 className="text-sm font-semibold text-gray-100">{selectedChat.name}</h2>
                <p className="text-xs text-slate-500">{selectedChat.type}</p>
              </div>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => setShowInfo((v) => !v)}
                  title="Chat info & tags"
                  className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs transition-colors ${
                    showInfo ? 'bg-indigo-500/20 text-indigo-400' : 'text-slate-400 hover:bg-slate-700'
                  }`}
                >
                  <Info size={13} />
                  Info
                </button>
                <button
                  onClick={handleSync}
                  disabled={syncing === selectedChat.telegram_id}
                  className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs text-slate-400 hover:bg-slate-700 disabled:opacity-50"
                >
                  <RefreshCw size={13} className={syncing === selectedChat.telegram_id ? 'animate-spin' : ''} />
                  Sync
                </button>
              </div>
            </div>

            <ChatMessagesView key={selectedChat.telegram_id} msgs={pane} chatType={selectedChat.type} />
            <ComposeBar key={selectedChat.telegram_id} onSend={handleSend} />
          </>
        ) : (
          <div className="flex flex-1 flex-col items-center justify-center text-slate-400">
            <Bookmark size={32} className="mb-2 text-amber-200" fill="currentColor" />
            <p className="text-sm">Select a tracked chat to view</p>
          </div>
        )}
      </div>

      {showInfo && selectedChat && (
        <ChatInfoPanel chat={selectedChat} onClose={() => setShowInfo(false)} />
      )}
    </div>
  )
}
