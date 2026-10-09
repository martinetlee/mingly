/**
 * Left pane of the Chats page (messengers only — email lives in Mail): channel
 * chips, folders, search (local + Telegram directory +
 * message content), tag filter, sort, the chat list, and the archived/hidden panels.
 * Presentational: every action is a callback into Chats, which owns the one
 * selectChat() entry point.
 */
import { memo, useEffect, useRef, useState } from 'react'
import type { ReactNode, Ref, RefObject } from 'react'
import { Virtuoso } from 'react-virtuoso'
import type { VirtuosoHandle } from 'react-virtuoso'
import { Radio, EyeOff, Eye, FileText, Search, MessageSquare, Pin, Archive, BellOff, Bell, MailOpen, MoreVertical, Plus, ShieldAlert, Tag as TagIcon, Columns3, X, Loader2 } from 'lucide-react'
import type { ChannelName, Conversation, Folder, Tag, TelegramDirectoryResult } from '../../api/types'
import type { MessageSearchResult } from '../../api/client'
import { getArchivedConversations, getHiddenConversations, getTags, MESSENGERS_ONLY } from '../../api/client'
import { useLatestQuery } from '../../hooks/useLatestQuery'
import { errorMessage } from '../../lib/toast'
import { fmtDate } from '../../utils/time'
import ChannelIcon, { channelLabel } from '../../components/ChannelIcon'
import SenderAvatar from '../../components/SenderAvatar'
import ConversationAvatar from '../../components/ConversationAvatar'
import { conversationTitle, isDirectoryHit, kindLabel } from '../../lib/conversation'

// Same tag color maps as ChatInfoPanel
const TAG_COLORS: Record<string, string> = {
  indigo: 'bg-indigo-500/20 text-indigo-300 border border-indigo-500/30',
  emerald: 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30',
  rose: 'bg-rose-500/20 text-rose-300 border border-rose-500/30',
  amber: 'bg-amber-500/20 text-amber-300 border border-amber-500/30',
  violet: 'bg-violet-500/20 text-violet-300 border border-violet-500/30',
  sky: 'bg-sky-500/20 text-sky-300 border border-sky-500/30',
  slate: 'bg-slate-500/20 text-slate-300 border border-slate-500/30',
}
const COLOR_DOTS: Record<string, string> = {
  indigo: 'bg-indigo-400', emerald: 'bg-emerald-400', rose: 'bg-rose-400',
  amber: 'bg-amber-400', violet: 'bg-violet-400', sky: 'bg-sky-400', slate: 'bg-slate-400',
}
const KIND_COLOR: Record<string, string> = {
  direct: 'bg-indigo-500/20 text-indigo-400',
  group: 'bg-purple-500/20 text-purple-400',
  channel: 'bg-amber-500/20 text-amber-400',
  email_thread: 'bg-sky-500/20 text-sky-400',
}

export type SortBy = 'recent' | 'unread' | 'alpha'

/** Conversation-level mutations; each resolves true on success (failures are toasted) */
export interface ChatActions {
  toggleTrack: (chat: Conversation) => Promise<boolean>
  hide: (chat: Conversation) => Promise<boolean>
  unhide: (chat: Conversation) => Promise<boolean>
  briefing: (chat: Conversation) => Promise<boolean>
  pin: (chat: Conversation) => Promise<boolean>
  archive: (chat: Conversation) => Promise<boolean>
  mute: (chat: Conversation, duration?: string) => Promise<boolean>
  markUnread: (chat: Conversation) => Promise<boolean>
}

function ChatContextMenu({ chat, position, onClose, actions }: {
  chat: Conversation
  position: { x: number; y: number }
  onClose: () => void
  actions: ChatActions
}) {
  const [showMuteOptions, setShowMuteOptions] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('mousedown', handler)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', handler)
      document.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  const run = (fn: () => Promise<boolean>) => { onClose(); void fn() }
  const itemClass = 'flex w-full items-center gap-2 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-600 transition-colors text-left'

  return (
    <div
      ref={menuRef}
      role="menu"
      style={{ position: 'fixed', left: position.x, top: position.y, zIndex: 50 }}
      className="min-w-[180px] rounded-lg border border-slate-600 bg-slate-700 py-1 shadow-xl"
    >
      <button onClick={() => run(() => actions.pin(chat))} className={itemClass}>
        <Pin size={13} className={chat.is_pinned ? 'text-indigo-400' : ''} />
        {chat.is_pinned ? 'Unpin' : 'Pin to top'}
      </button>
      <button onClick={() => run(() => actions.archive(chat))} className={itemClass}>
        <Archive size={13} />
        {chat.is_archived ? 'Unarchive' : 'Archive'}
      </button>
      {!showMuteOptions ? (
        <button onClick={() => (chat.is_muted ? run(() => actions.mute(chat)) : setShowMuteOptions(true))} className={itemClass}>
          {chat.is_muted ? <Bell size={13} /> : <BellOff size={13} />}
          {chat.is_muted ? 'Unmute' : 'Mute…'}
        </button>
      ) : (
        <div className="border-t border-b border-slate-600 py-0.5">
          <p className="px-3 py-1 text-[10px] text-slate-500 font-medium">Mute for:</p>
          {[
            { label: '1 hour', value: '1h' },
            { label: '8 hours', value: '8h' },
            { label: '1 day', value: '1d' },
            { label: 'Forever', value: 'forever' },
          ].map((opt) => (
            <button key={opt.value} onClick={() => run(() => actions.mute(chat, opt.value))} className={itemClass}>
              <BellOff size={13} />
              {opt.label}
            </button>
          ))}
        </div>
      )}
      <button onClick={() => run(() => actions.markUnread(chat))} className={itemClass}>
        <MailOpen size={13} />
        {chat.is_marked_unread ? 'Mark as read' : 'Mark as unread'}
      </button>
      <div className="my-0.5 border-t border-slate-600" />
      <button onClick={() => run(() => actions.toggleTrack(chat))} className={itemClass}>
        <Radio size={13} className={chat.is_tracked ? 'text-indigo-400' : ''} />
        {chat.is_tracked ? 'Stop tracking' : 'Track'}
      </button>
      <button onClick={() => run(() => actions.briefing(chat))} className={itemClass}>
        <FileText size={13} />
        Add to briefing
      </button>
      <button onClick={() => run(() => actions.hide(chat))} className={itemClass}>
        <EyeOff size={13} />
        Hide
      </button>
    </div>
  )
}

interface ChatItemProps {
  chat: Conversation
  selected: boolean
  hasScamFlag?: boolean
  onSelect: (chat: Conversation) => void
  onContextMenu?: (chat: Conversation, x: number, y: number) => void
  onToggleTrack?: (chat: Conversation) => void
}

const ChatItem = memo(function ChatItem({ chat, selected, hasScamFlag, onSelect, onContextMenu, onToggleTrack }: ChatItemProps) {
  const unread = chat.unread_count ?? 0
  const title = conversationTitle(chat)
  return (
    <div
      data-chat-id={chat.id}
      onContextMenu={onContextMenu ? (e) => { e.preventDefault(); onContextMenu(chat, e.clientX, e.clientY) } : undefined}
      className={`group flex items-center gap-3 px-3 py-2.5 transition-colors ${selected ? 'bg-indigo-500/15' : 'hover:bg-slate-700/50'}`}
    >
      <button
        type="button"
        onClick={() => onSelect(chat)}
        aria-current={selected ? 'true' : undefined}
        data-chat-row=""
        className="flex min-w-0 flex-1 cursor-pointer items-center gap-3 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 focus-visible:rounded"
      >
        <ConversationAvatar conversation={chat} size={36} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            {chat.is_pinned && <Pin size={11} className="flex-shrink-0 text-indigo-400" />}
            <p title={title} className={`truncate text-sm font-medium ${selected ? 'text-indigo-300' : 'text-gray-200'}`}>{title}</p>
            <ChannelIcon channel={chat.channel} />
            <span className={`flex-shrink-0 inline-block rounded px-1 text-[10px] font-medium ${KIND_COLOR[chat.kind] ?? 'bg-slate-600 text-slate-400'}`}>{kindLabel(chat.kind)}</span>
            {chat.is_forum && (
              <span className="flex-shrink-0 inline-block rounded px-1 text-[10px] font-medium bg-purple-500/20 text-purple-400">
                <MessageSquare size={9} className="inline" />
              </span>
            )}
            {hasScamFlag && <span title="Contains flagged scammer"><ShieldAlert size={11} className="flex-shrink-0 text-red-400" /></span>}
            {chat.is_muted && <BellOff size={11} className="flex-shrink-0 text-slate-500" />}
            {chat.last_message?.date && (
              <span className="ml-auto flex-shrink-0 text-[10px] text-slate-500">{fmtDate(chat.last_message.date)}</span>
            )}
          </div>
          {chat.last_message && (
            <p className="truncate text-xs text-slate-500 mt-0.5">
              {chat.last_message.sender_name && <span className="text-slate-400">{chat.last_message.sender_name}: </span>}
              {chat.last_message.text || '[media]'}
            </p>
          )}
        </div>
      </button>

      <div className="flex flex-shrink-0 items-center gap-0.5">
        {(unread > 0 || chat.is_marked_unread) && (
          <span
            className={`min-w-[18px] rounded-full px-1.5 py-0.5 text-center text-[10px] font-semibold leading-tight text-white ${chat.is_muted ? 'bg-slate-500' : 'bg-indigo-500'}`}
            aria-label={`${unread} unread`}
          >
            {unread > 0 ? (unread > 99 ? '99+' : unread) : '•'}
          </span>
        )}
        {onContextMenu && (
          <button
            onClick={(e) => { e.stopPropagation(); onContextMenu(chat, e.clientX, e.clientY) }}
            title="More actions"
            aria-label="More actions"
            className="rounded p-1 text-slate-500 opacity-0 transition-opacity group-hover:opacity-100 focus:opacity-100 hover:text-slate-300 focus-visible:ring-2 focus-visible:ring-indigo-500 focus:outline-none"
          >
            <MoreVertical size={14} />
          </button>
        )}
        {onToggleTrack && (
          <button
            onClick={() => onToggleTrack(chat)}
            title={chat.is_tracked ? 'Stop tracking' : 'Track this chat'}
            aria-label={chat.is_tracked ? 'Stop tracking' : 'Track this chat'}
            className={`rounded p-1 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 ${
              chat.is_tracked ? 'text-indigo-400 hover:text-indigo-300' : 'text-slate-600 hover:text-slate-400'
            }`}
          >
            <Radio size={14} />
          </button>
        )}
      </div>
    </div>
  )
})

/** A Telegram search hit: a stored conversation, or a directory hit that opening ensures */
const DirectoryItem = memo(function DirectoryItem({ result, selected, onSelect }: {
  result: TelegramDirectoryResult
  selected: boolean
  onSelect: (r: TelegramDirectoryResult) => void
}) {
  const hit = isDirectoryHit(result)
  const name = result.title || kindLabel(result.kind)
  return (
    <button
      type="button"
      onClick={() => onSelect(result)}
      aria-current={selected ? 'true' : undefined}
      className={`flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 ${selected ? 'bg-indigo-500/15' : 'hover:bg-slate-700/50'}`}
    >
      <SenderAvatar avatarUrl={hit ? null : result.avatar_url} name={name} colorSeed={hit ? result.peer_id : result.id} size={36} link={false} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <p title={name} className="truncate text-sm font-medium text-gray-200">{name}</p>
          <ChannelIcon channel="telegram" />
          <span className={`flex-shrink-0 inline-block rounded px-1 text-[10px] font-medium ${KIND_COLOR[result.kind] ?? 'bg-slate-600 text-slate-400'}`}>{kindLabel(result.kind)}</span>
        </div>
        {result.username && <p className="truncate text-xs text-slate-500 mt-0.5">@{result.username}</p>}
      </div>
    </button>
  )
})

function TagFilter({ selectedTag, onSelectTag }: { selectedTag: Tag | null; onSelectTag: (tag: Tag | null) => void }) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const ref = useRef<HTMLDivElement>(null)
  const tags = useLatestQuery(['tags'], () => getTags(), { enabled: open })

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const q = query.trim().toLowerCase()
  const all = tags.data ?? []
  const visible = all
    .filter((t) => t.category !== 'stage' && t.category !== 'stream') // board tags live on boards, not here
    .filter((t) => (q ? t.name.toLowerCase().includes(q) : (t.conversation_count ?? 0) > 0))

  return (
    <div ref={ref} className="relative flex-shrink-0">
      <button
        onClick={() => { setOpen((v) => !v); setQuery('') }}
        title="Filter by tag"
        className={`rounded-lg border px-2 py-1.5 transition-colors ${
          selectedTag || open
            ? 'border-indigo-500/30 bg-indigo-500/20 text-indigo-400'
            : 'border-slate-600 bg-slate-700 text-slate-400 hover:bg-indigo-500/20 hover:text-indigo-400 hover:border-indigo-500/30'
        }`}
      >
        <TagIcon size={16} />
      </button>
      {open && (
        <div className="absolute right-0 top-full mt-1 z-30 w-60 rounded-lg border border-slate-600 bg-slate-800 shadow-xl overflow-hidden">
          <div className="p-2 border-b border-slate-700/50">
            <input
              type="text"
              autoFocus
              placeholder="Filter tags…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="w-full rounded-md border border-slate-600 bg-slate-700 px-2 py-1 text-xs text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500"
            />
          </div>
          <div className="max-h-64 overflow-y-auto py-1">
            {tags.loading ? (
              <p className="px-3 py-2 text-xs text-slate-500">Loading…</p>
            ) : tags.error ? (
              <p className="px-3 py-2 text-xs text-red-400">Couldn’t load tags</p>
            ) : visible.length === 0 ? (
              <p className="px-3 py-2 text-xs text-slate-500">{all.length === 0 ? 'No tags yet' : 'No matching tags'}</p>
            ) : visible.map((tag) => (
              <button
                key={tag.id}
                onClick={() => { onSelectTag(selectedTag?.id === tag.id ? null : tag); setOpen(false) }}
                className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors hover:bg-slate-700/50 ${
                  selectedTag?.id === tag.id ? 'text-indigo-300' : 'text-slate-300'
                }`}
              >
                <span className={`h-2 w-2 flex-shrink-0 rounded-full ${COLOR_DOTS[tag.color] ?? 'bg-slate-400'}`} />
                <span className="min-w-0 flex-1 truncate">{tag.name}</span>
                <span className="text-slate-500">{tag.conversation_count ?? 0}</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

/** "All" + one chip per connected messenger channel */
function ChannelChips({ channels, selected, onSelect }: {
  channels: ChannelName[]; selected: string | null; onSelect: (channel: string | null) => void
}) {
  return (
    <div role="group" aria-label="Filter by channel" className="flex flex-wrap gap-1 border-b border-slate-700/50 px-3 pt-2 pb-1.5">
      {[null, ...channels].map((c) => (
        <button
          key={c ?? 'all'}
          onClick={() => onSelect(c)}
          aria-pressed={selected === c}
          className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-[11px] font-medium transition-colors ${
            selected === c ? 'bg-indigo-500/20 text-indigo-400' : 'text-slate-400 hover:bg-slate-700'
          }`}
        >
          {c && <span aria-hidden><ChannelIcon channel={c} className="text-current" /></span>}
          {c ? channelLabel(c) : 'All'}
        </button>
      ))}
    </div>
  )
}

/** Archived / hidden chats drawer (fetched when opened, refetched after a restore).
 * Messengers only, narrowed to the selected channel like the list. */
function SideDrawer({ kind, channel, onRestore }: {
  kind: 'archived' | 'hidden'; channel: string | null; onRestore: (chat: Conversation) => Promise<boolean>
}) {
  const [open, setOpen] = useState(false)
  const query = useLatestQuery([kind], () => (kind === 'archived' ? getArchivedConversations() : getHiddenConversations()), { enabled: open })
  const list = {
    ...query,
    data: query.data?.filter((c) => c.kind !== MESSENGERS_ONLY.excludeKind && (channel == null || c.channel === channel)),
  }
  const Icon = kind === 'archived' ? Archive : Eye
  const label = kind === 'archived' ? 'archived' : 'hidden'
  return (
    <div className="border-t border-slate-700/50">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 px-3 py-2 text-xs text-slate-500 hover:text-slate-300 transition-colors"
        aria-expanded={open}
      >
        <Icon size={12} />
        {open ? `Hide ${label} chats` : `Show ${label} chats`}
      </button>
      {open && (
        <div className="max-h-48 overflow-y-auto border-t border-slate-700/50 bg-slate-900/50">
          {list.loading ? (
            <p className="px-4 py-3 text-center text-xs text-slate-500">Loading…</p>
          ) : list.error ? (
            <p className="px-4 py-3 text-center text-xs text-red-400">{errorMessage(list.error)}</p>
          ) : (list.data ?? []).length === 0 ? (
            <p className="px-4 py-3 text-center text-xs text-slate-500">No {label} chats</p>
          ) : (list.data ?? []).map((chat) => (
            <div key={chat.id} className="flex items-center gap-2 px-3 py-2">
              <ConversationAvatar conversation={chat} size={28} />
              <p title={conversationTitle(chat)} className="min-w-0 flex-1 truncate text-xs text-slate-400">{conversationTitle(chat)}</p>
              <ChannelIcon channel={chat.channel} />
              <button
                onClick={() => { void onRestore(chat).then((ok) => { if (ok) void list.refetch() }) }}
                title={kind === 'archived' ? 'Unarchive' : 'Unhide'}
                aria-label={kind === 'archived' ? `Unarchive ${conversationTitle(chat)}` : `Unhide ${conversationTitle(chat)}`}
                className="flex-shrink-0 rounded p-1 text-slate-500 hover:text-slate-300 transition-colors"
              >
                <Icon size={12} />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

const ListFooter = ({ context }: { context?: { footer: ReactNode } }) => <>{context?.footer}</>
const LIST_COMPONENTS = { Footer: ListFooter }

export interface ChatListPaneProps {
  /** Scroll a row into view (keyboard navigation over the virtualized list) */
  virtuosoRef?: Ref<VirtuosoHandle>
  width: number
  onResizeStart: (e: React.MouseEvent) => void
  listRef: RefObject<HTMLDivElement>
  searchInputRef: RefObject<HTMLInputElement>
  /** Connected messenger channels; chips show when there are two or more */
  channels: ChannelName[]
  selectedChannel: string | null
  onSelectChannel: (channel: string | null) => void
  folders: Folder[]
  selectedFolder: number | null
  onSelectFolder: (id: number | null) => void
  search: string
  onSearchChange: (q: string) => void
  /** Telegram directory matches not already in the list (Telegram-only) */
  telegramResults: TelegramDirectoryResult[]
  searchingTelegram: boolean
  messageResults: MessageSearchResult[] | null
  searchingMessages: boolean
  onSearchMessages: () => void
  onOpenSearchResult: (r: TelegramDirectoryResult) => void
  onOpenMessageResult: (r: MessageSearchResult) => void
  sortBy: SortBy
  onSortBy: (s: SortBy) => void
  selectedTag: Tag | null
  onSelectTag: (t: Tag | null) => void
  boardButton?: { title: string; onClick: () => void } | null
  onNewChat: () => void
  chats: Conversation[]
  loading: boolean
  loadingMore: boolean
  onLoadMore: () => void
  selectedId: number | null
  flaggedChatIds: Set<number>
  onSelect: (chat: Conversation) => void
  actions: ChatActions
}

export default function ChatListPane(p: ChatListPaneProps) {
  const [contextMenu, setContextMenu] = useState<{ chat: Conversation; x: number; y: number } | null>(null)
  const actionsRef = useRef(p.actions)
  actionsRef.current = p.actions
  // Stable row callbacks so memoised rows don't re-render on unrelated updates
  const openMenu = useRef((chat: Conversation, x: number, y: number) => setContextMenu({ chat, x, y })).current
  const toggleTrack = useRef((chat: Conversation) => { void actionsRef.current.toggleTrack(chat) }).current
  const searching = p.search.trim().length > 0
  const searchExtras = (
    <>
      {searching && (
        <div className="border-t border-slate-700/50">
          <p className="px-3 py-1.5 text-[10px] font-medium text-slate-500 uppercase tracking-wider">
            {p.searchingTelegram ? 'Searching Telegram…' : p.telegramResults.length > 0 ? 'On Telegram' : 'Nothing else found on Telegram'}
          </p>
          {p.telegramResults.map((r) => (
            <DirectoryItem
              key={isDirectoryHit(r) ? `tg-${r.peer_id}` : `c-${r.id}`}
              result={r}
              selected={!isDirectoryHit(r) && p.selectedId === r.id}
              onSelect={p.onOpenSearchResult}
            />
          ))}
        </div>
      )}

      {searching && (
        <div className="border-t border-slate-700/50 px-3 py-2">
          {p.searchingMessages ? (
            <p className="flex items-center justify-center gap-1.5 text-xs text-slate-500"><Loader2 size={12} className="animate-spin" /> Searching messages…</p>
          ) : p.messageResults ? (
            p.messageResults.length === 0 ? (
              <p className="text-center text-xs text-slate-500">No messages found</p>
            ) : (
              <div>
                <p className="text-[10px] font-medium text-slate-500 uppercase tracking-wider mb-1">Messages</p>
                {p.messageResults.map((r) => (
                  <button
                    key={r.conversation_id}
                    onClick={() => p.onOpenMessageResult(r)}
                    className="w-full text-left px-2 py-1.5 rounded hover:bg-slate-700/50 transition-colors"
                  >
                    <p className="text-xs font-medium text-slate-300 truncate">{r.conversation_title || kindLabel(r.conversation_kind)}</p>
                    {r.messages[0] && (
                      <p className="text-[11px] text-slate-500 truncate">
                        {r.messages[0].sender_name && <span className="text-slate-400">{r.messages[0].sender_name}: </span>}
                        {r.messages[0].text}
                      </p>
                    )}
                  </button>
                ))}
              </div>
            )
          ) : (
            <button
              onClick={p.onSearchMessages}
              className="w-full rounded-lg border border-slate-600 bg-slate-700/50 px-3 py-2 text-xs text-slate-400 hover:text-slate-200 hover:bg-slate-700 transition-colors flex items-center justify-center gap-1.5"
            >
              <Search size={12} />
              Search message content
            </button>
          )}
        </div>
      )}
    </>
  )

  return (
    <div className="flex flex-shrink-0 flex-col border-r border-slate-700/50 bg-slate-800 relative" style={{ width: p.width }}>
      <div
        onMouseDown={p.onResizeStart}
        className="absolute right-0 top-0 h-full w-1 cursor-col-resize hover:bg-indigo-500 transition-colors z-10"
        aria-hidden="true"
      />
      {p.channels.length > 1 && (
        <ChannelChips channels={p.channels} selected={p.selectedChannel} onSelect={p.onSelectChannel} />
      )}
      {p.folders.length > 0 && (
        <div className="folder-scrollbar flex overflow-x-auto border-b border-slate-700/50 px-2 pt-2 pb-1 gap-1">
          {[{ id: null as number | null, title: 'All' }, ...p.folders].map((f) => (
            <button
              key={f.id ?? 'all'}
              onClick={() => p.onSelectFolder(f.id)}
              className={`flex-shrink-0 rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                p.selectedFolder === f.id ? 'bg-indigo-500/20 text-indigo-400' : 'text-slate-400 hover:bg-slate-700'
              }`}
            >
              {f.title}
            </button>
          ))}
        </div>
      )}
      <div className="border-b border-slate-700/50 px-3 py-3 flex flex-col gap-2">
        <div className="flex gap-2">
          <input
            ref={p.searchInputRef}
            type="text"
            placeholder="Search chats…"
            value={p.search}
            onChange={(e) => p.onSearchChange(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Escape' && p.search) { e.stopPropagation(); p.onSearchChange('') } }}
            className="min-w-0 flex-1 rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-sm text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500/30"
          />
          <button
            onClick={p.onNewChat}
            title="New conversation"
            className="flex-shrink-0 rounded-lg border border-slate-600 bg-slate-700 px-2 py-1.5 text-slate-400 hover:bg-indigo-500/20 hover:text-indigo-400 hover:border-indigo-500/30 transition-colors"
          >
            <Plus size={16} />
          </button>
          {p.boardButton && (
            <button
              onClick={p.boardButton.onClick}
              title={p.boardButton.title}
              className="flex-shrink-0 rounded-lg border border-slate-600 bg-slate-700 px-2 py-1.5 text-slate-400 hover:bg-indigo-500/20 hover:text-indigo-400 hover:border-indigo-500/30 transition-colors"
            >
              <Columns3 size={16} />
            </button>
          )}
          <TagFilter selectedTag={p.selectedTag} onSelectTag={p.onSelectTag} />
        </div>
        <div className="flex rounded-lg border border-slate-700 overflow-hidden text-[11px]">
          {(['recent', 'unread', 'alpha'] as const).map((s) => (
            <button
              key={s}
              onClick={() => p.onSortBy(s)}
              className={`flex-1 py-1 transition-colors ${
                p.sortBy === s ? 'bg-indigo-500/20 text-indigo-400 font-medium' : 'text-slate-500 hover:text-slate-300 hover:bg-slate-700/50'
              }`}
            >
              {s === 'recent' ? 'Recent' : s === 'unread' ? 'Unread' : 'A-Z'}
            </button>
          ))}
        </div>
      </div>
      {p.selectedTag && (
        <div className="border-b border-slate-700/50 px-3 py-1.5">
          <button
            onClick={() => p.onSelectTag(null)}
            title="Clear tag filter"
            className={`inline-flex max-w-full items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${TAG_COLORS[p.selectedTag.color] ?? TAG_COLORS.slate}`}
          >
            <span className="truncate">{p.selectedTag.name}</span>
            <X size={12} className="flex-shrink-0" />
          </button>
        </div>
      )}
      <div ref={p.listRef} className="flex min-h-0 flex-1 flex-col" data-chat-list="">
        {p.chats.length === 0 ? (
          <div className="flex-1 overflow-y-auto">
            <p className="px-4 py-6 text-center text-sm text-slate-500">{p.loading ? 'Loading…' : 'No chats found'}</p>
            {searchExtras}
          </div>
        ) : (
          // Virtualized: only the visible rows (plus overscan) are mounted
          <Virtuoso
            ref={p.virtuosoRef}
            className="flex-1"
            data={p.chats}
            computeItemKey={(_i, chat) => chat.id}
            itemContent={(_i, chat) => (
              <ChatItem
                chat={chat}
                selected={p.selectedId === chat.id}
                hasScamFlag={p.flaggedChatIds.has(chat.id)}
                onSelect={p.onSelect}
                onContextMenu={openMenu}
                onToggleTrack={toggleTrack}
              />
            )}
            endReached={() => p.onLoadMore()}
            increaseViewportBy={400}
            components={LIST_COMPONENTS}
            context={{ footer: <>{p.loadingMore && <p className="px-4 py-3 text-center text-xs text-slate-500">Loading…</p>}{searchExtras}</> }}
          />
        )}
      </div>

      <SideDrawer kind="archived" channel={p.selectedChannel} onRestore={p.actions.archive} />
      <SideDrawer kind="hidden" channel={p.selectedChannel} onRestore={p.actions.unhide} />

      {contextMenu && (
        <ChatContextMenu
          chat={contextMenu.chat}
          position={{ x: contextMenu.x, y: contextMenu.y }}
          onClose={() => setContextMenu(null)}
          actions={p.actions}
        />
      )}
    </div>
  )
}
