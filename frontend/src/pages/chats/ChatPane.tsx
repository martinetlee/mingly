/**
 * Right pane: header, in-chat search, forum topics, pinned banner, message
 * window and compose box for ONE chat. Chats mounts it with
 * `key={chat.telegram_id}`, so nothing here can outlive a chat switch;
 * per-chat state that must survive (draft, reply, edit, upload progress)
 * lives in chatUiStore, and every async action writes to the chat it was
 * started for (captured ids — never "the currently selected chat").
 */
import { memo, useCallback, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { RefreshCw, Info, X, Search, Star, Loader2, MessageSquare, Pin, AlertTriangle } from 'lucide-react'
import type { Chat, Message, Reaction, SendFileResult, SendFilesResponse } from '../../api/types'
import {
  ApiError,
  deleteMessages, editMessage, ensureChat, forwardMessages, getContactByTelegram, getPinnedMessage, pinMessage,
  reactToMessage, restrictMember, searchChatMessages, sendFiles, sendMessage, sendTypingAction,
  setContactImportantByTelegram, setMessageVerdict, setSenderVerdict, syncChat,
} from '../../api/client'
import { authenticatedMediaUrl } from '../../api/session'
import { useChatMessages } from '../../hooks/useChatMessages'
import type { JumpNotice } from '../../hooks/useChatMessages'
import { useLatestQuery } from '../../hooks/useLatestQuery'
import { useDebouncedQuery } from '../../hooks/useDebouncedQuery'
import { useAsyncAction } from '../../hooks/useAsyncAction'
import { useWsEvent } from '../../hooks/useWebSocket'
import { applyFlagChanges, useFlaggedSenders } from '../../hooks/useFlaggedSenders'
import { useFeatures } from '../../context/FeaturesContext'
import { useConfirm } from '../../components/ui'
import { errorMessage, reportError, toast } from '../../lib/toast'
import { fmtShortDate } from '../../utils/time'
import MessageList from '../../components/MessageList'
import type { ScamVerdictScope } from '../../components/MessageBubble'
import ComposeBar from '../../components/ComposeBar'
import type { FilesSendOutcome } from '../../components/ComposeBar'
import ForumTopicsPanel from '../../components/ForumTopicsPanel'
import ChatPickerDialog from '../../components/ChatPickerDialog'
import ReportScamDialog from '../../components/ReportScamDialog'
import { ChatStatusLine, TypingBar } from './PresenceTyping'
import { getChatUi, newNonce, sendKey, updateChatUi, useChatUi, useChatUiSelect } from './chatUiStore'
import type { ComposeDraft } from './chatUiStore'
import { useScamCheckEvents } from './scamChecks'

export interface ChatPaneProps {
  chat: Chat
  topic: { id: number; title: string } | null
  /** Bumped by selectChat; `anchorTgId` opens the chat centred on a message */
  selection: { anchorTgId: number | null; unreadCount: number; nonce: number; viaKeyboard?: boolean }
  showInfo: boolean
  onToggleInfo: () => void
  showSearch: boolean
  onShowSearch: (open: boolean) => void
  onSelectTopic: (topic: { id: number; title: string } | null) => void
  /** Open another chat through the page's single selectChat entry point */
  onOpenChat: (chat: Chat) => void
  /** Own send landed: update the chat list preview */
  onChatActivity: (chatId: number, preview: { text: string; date: string }) => void
}

/** Optimistic reaction toggle that keeps the aggregate counts consistent. */
export function applyReaction(m: Message, emoji: string | null): Message {
  const prev = m.my_reaction ?? null
  let reactions: Reaction[] = [...(m.reactions ?? [])]
  if (prev) {
    reactions = reactions
      .map((r) => (r.emoji === prev ? { ...r, count: r.count - 1 } : r))
      .filter((r) => r.count > 0)
  }
  if (emoji) {
    const idx = reactions.findIndex((r) => r.emoji === emoji)
    if (idx >= 0) reactions[idx] = { ...reactions[idx], count: reactions[idx].count + 1 }
    else reactions.push({ emoji, count: 1 })
  }
  return { ...m, my_reaction: emoji, reactions }
}

/** `<input type=date>` value (local calendar day) → ISO instant at local midnight */
function localDayToIso(day: string): string | null {
  const [y, m, d] = day.split('-').map(Number)
  if (!y || !m || !d) return null
  return new Date(y, m - 1, d).toISOString()
}

const NO_RESTRICTIONS = {
  send_messages: false, send_media: false, send_stickers: false, send_gifs: false,
  send_games: false, send_inline: false, embed_links: false, send_polls: false,
}

function ChatPane({ chat, topic, selection, showInfo, onToggleInfo, showSearch, onShowSearch, onSelectTopic, onOpenChat, onChatActivity }: ChatPaneProps) {
  const chatId = chat.telegram_id
  const navigate = useNavigate()
  const confirm = useConfirm()
  // Only the sync flag here: the compose slot changes per keystroke and is read by ComposeArea
  const syncing = useChatUiSelect(chatId, (u) => u.syncing)
  const flagged = useFlaggedSenders()
  const { shield } = useFeatures()
  useScamCheckEvents()

  const msgs = useChatMessages({
    chatId,
    topicId: topic?.id ?? null,
    anchorTgId: selection.anchorTgId,
    unreadCount: selection.unreadCount,
    nonce: selection.nonce,
  })
  const msgsRef = useRef(msgs)
  msgsRef.current = msgs

  // ── Header data (keyed by chat; aborted on switch) ─────────────────────────
  const pinned = useLatestQuery(['pinned', chatId], (signal) => getPinnedMessage(chatId, signal))
  const refetchPinned = useRef(pinned.refetch)
  refetchPinned.current = pinned.refetch
  useWsEvent('messages_pinned', (e) => { if (e.chat_id === chatId) void pinned.refetch() })
  const important = useLatestQuery(
    ['important', chatId],
    (signal) => getContactByTelegram(chatId, signal).then((c) => c.is_important).catch(() => false),
    { enabled: chat.type === 'private' },
  )
  const toggleImportant = useAsyncAction(async () => {
    const updated = await setContactImportantByTelegram(chatId, !important.data)
    important.setData(updated.is_important)
  }, { errorContext: 'Couldn’t update important contact' })

  // ── In-chat search ─────────────────────────────────────────────────────────
  const [chatSearch, setChatSearch] = useState('')
  const searchResults = useDebouncedQuery(chatSearch, (q, signal) => searchChatMessages(chatId, q, 50, signal), { delayMs: 300, enabled: showSearch })

  // ── Manual sync ────────────────────────────────────────────────────────────
  const handleSync = useCallback(async () => {
    if (getChatUi(chatId).syncing) return
    updateChatUi(chatId, { syncing: true })
    try {
      const res = await syncChat(chatId)
      if (res?.status === 'changed') void msgsRef.current.refresh()
      else if (res?.error) toast.error(`Sync: ${res.error}`)
    } catch (e) {
      reportError(e, 'Sync failed')
    } finally {
      updateChatUi(chatId, { syncing: false })
    }
  }, [chatId])

  // ── Compose (all writes go to the captured chat's slot) ───────────────────
  const handleSend = useCallback(async (text: string) => {
    const before = getChatUi(chatId)
    const replyTo = before.replyTo
    const topicId = topic?.id ?? null
    const key = sendKey(text, [], replyTo?.tg_message_id, topicId)
    const nonce = before.failedSend?.key === key ? before.failedSend.nonce : newNonce()
    updateChatUi(chatId, { sending: true })
    try {
      const res = await sendMessage(chatId, text, replyTo?.tg_message_id, { topicId, clientNonce: nonce })
      updateChatUi(chatId, (p) => ({
        sending: false,
        failedSend: null,
        replyTo: p.replyTo?.tg_message_id === replyTo?.tg_message_id ? null : p.replyTo,
        draft: p.draft.text.trim() === text ? { ...p.draft, text: '' } : p.draft,
      }))
      const m = msgsRef.current
      if (res.message) {
        m.applyMessage(res.message)
        void m.jumpToLatest() // your own message: show it (reloads the window if it was scrolled back)
      } else {
        void m.refresh()
      }
      onChatActivity(chatId, { text, date: res.message?.date ?? new Date().toISOString() })
    } catch (e) {
      updateChatUi(chatId, { sending: false, failedSend: { nonce, key, error: errorMessage(e) } })
      throw e // ComposeBar shows it inline and keeps the text
    }
  }, [chatId, topic?.id, onChatActivity])

  const handleSendFiles = useCallback(async (files: File[], text: string): Promise<FilesSendOutcome> => {
    const before = getChatUi(chatId)
    const replyTo = before.replyTo
    const topicId = topic?.id ?? null
    const key = sendKey(text, files, replyTo?.tg_message_id, topicId)
    // Reuse the nonce only after a transport failure (the server may have sent it).
    // When the server answered with per-file failures, a retry needs a NEW nonce
    // (the same nonce would just replay that response).
    const nonce = before.failedSend?.key === key ? before.failedSend.nonce : newNonce()
    updateChatUi(chatId, { sending: true, uploadProgress: 0 })
    let res: SendFilesResponse
    try {
      const { promise } = sendFiles(chatId, files, text || undefined, replyTo?.tg_message_id,
        (fraction) => updateChatUi(chatId, { uploadProgress: fraction }),
        { topicId, clientNonce: nonce })
      res = await promise
    } catch (e) {
      const answered = e instanceof ApiError && e.status !== 0
      updateChatUi(chatId, {
        sending: false, uploadProgress: null,
        failedSend: answered ? null : { nonce, key, error: errorMessage(e) },
      })
      const perFile = e instanceof ApiError ? (e.body?.files as SendFileResult[] | undefined) : undefined
      if (perFile?.length) {
        // None sent (400): every file stays attached with its own error
        return { failed: files.map((f, i) => ({ file: f, error: perFile[i]?.error ?? errorMessage(e) })) }
      }
      throw e
    }
    // Per-file outcome (same order as uploaded); a partial send keeps only the failed files
    const failed = (res.files ?? [])
      .map((r, i) => ({ r, file: files[i] }))
      .filter(({ r, file }) => r.status === 'failed' && file)
      .map(({ r, file }) => ({ file, error: r.error ?? 'Failed to send' }))
    const failedSet = new Set(failed.map((f) => f.file))
    updateChatUi(chatId, (p) => ({
      sending: false,
      uploadProgress: null,
      failedSend: null,
      replyTo: p.replyTo?.tg_message_id === replyTo?.tg_message_id ? null : p.replyTo,
      // The caption went out with the first sent item
      draft: {
        text: p.draft.text.trim() === text ? '' : p.draft.text,
        attachments: p.draft.attachments.filter((a) => !files.includes(a.file) || failedSet.has(a.file)),
      },
    }))
    const m = msgsRef.current
    if (res.messages?.length) {
      for (const msg of res.messages) m.applyMessage(msg)
      void m.jumpToLatest()
    } else {
      void m.refresh()
    }
    if (failed.length < files.length) {
      const preview = text || `[${files[0]?.type?.startsWith('image/') ? 'photo' : 'file'}]`
      onChatActivity(chatId, { text: preview, date: new Date().toISOString() })
    }
    return failed.length ? { failed } : undefined
  }, [chatId, topic?.id, onChatActivity])

  const handleSaveEdit = useCallback(async (text: string) => {
    // The edit target was captured when Edit was clicked (and lives in its chat's slot)
    const target = getChatUi(chatId).editing
    if (!target) return
    const res = await editMessage(target.chat_id, target.tg_message_id, text)
    updateChatUi(target.chat_id, (p) => (p.editing?.tg_message_id === target.tg_message_id ? { editing: null } : {}))
    msgsRef.current.applyMessage(res.message ?? { ...target, text, text_html: null, is_edited: true })
  }, [chatId])

  const handleTyping = useCallback(() => {
    sendTypingAction(chatId).catch((e) => console.debug('typing action failed', e))
  }, [chatId])

  const [mentionInsert, setMentionInsert] = useState<string | null>(null)
  const clearMention = useCallback(() => setMentionInsert(null), [])

  // ── Message actions (targets come from the message itself) ─────────────────
  const onReply = useCallback((msg: Message) => updateChatUi(msg.chat_id, { replyTo: msg, editing: null }), [])
  const onEdit = useCallback((msg: Message) => updateChatUi(msg.chat_id, { editing: msg, replyTo: null }), [])

  const onReact = useCallback(async (msg: Message, emoji: string | null) => {
    const m = msgsRef.current
    m.patchMessage(msg.tg_message_id, (cur) => applyReaction(cur, emoji))
    try {
      await reactToMessage(msg.chat_id, msg.tg_message_id, emoji)
    } catch (e) {
      m.patchMessage(msg.tg_message_id, (cur) => applyReaction(cur, msg.my_reaction ?? null))
      reportError(e, 'Reaction failed')
    }
  }, [])

  const onPin = useCallback(async (msg: Message) => {
    const next = !msg.is_pinned
    try {
      await pinMessage(msg.chat_id, msg.tg_message_id, next)
      msgsRef.current.patchMessage(msg.tg_message_id, { is_pinned: next })
      void refetchPinned.current()
      toast.success(next ? 'Message pinned' : 'Message unpinned')
    } catch (e) {
      reportError(e, next ? 'Pin failed' : 'Unpin failed')
    }
  }, [])

  const onDelete = useCallback(async (msg: Message, album?: Message[]) => {
    // Target captured at open: {chatId, ids} — confirm can't act on another chat
    const targetChat = msg.chat_id
    const ids = (album ?? [msg]).map((m) => m.tg_message_id)
    const preview = msg.text?.slice(0, 80) ?? (msg.media_type ? `[${msg.media_type}]` : 'this message')
    const res = await confirm({
      title: ids.length > 1 ? `Delete ${ids.length} messages?` : 'Delete message?',
      message: <span className="line-clamp-2">{preview}</span>,
      destructive: true,
      checkbox: { label: 'Delete for everyone' },
      onConfirm: (revoke) => deleteMessages(targetChat, ids, revoke),
    })
    if (res.confirmed && targetChat === chatId) msgsRef.current.removeMessages(ids)
  }, [chatId, confirm])

  const [forwardTarget, setForwardTarget] = useState<{ fromChatId: number; ids: number[] } | null>(null)
  const onForward = useCallback((msg: Message, album?: Message[]) => {
    setForwardTarget({ fromChatId: msg.chat_id, ids: (album ?? [msg]).map((m) => m.tg_message_id) })
  }, [])
  const forward = useAsyncAction(async (target: { fromChatId: number; ids: number[] }, to: Chat) => {
    await forwardMessages(target.fromChatId, target.ids, to.telegram_id)
    toast.success(`Forwarded to ${to.name}`)
    setForwardTarget(null)
  }, { errorContext: 'Forward failed' })

  const [reportTarget, setReportTarget] = useState<{ chatId: number; tgId: number; senderId: number } | null>(null)
  const onReportScam = useCallback((msg: Message) => {
    setReportTarget({ chatId: msg.chat_id, tgId: msg.tg_message_id, senderId: msg.sender_id ?? 0 })
  }, [])

  const onScamVerdict = useCallback(async (msg: Message, scope: ScamVerdictScope) => {
    try {
      const res = scope === 'sender' && msg.sender_id != null
        ? await setSenderVerdict(msg.sender_id, 'not_scam')
        : await setMessageVerdict([{ chat_id: msg.chat_id, tg_message_id: msg.tg_message_id }], 'not_scam')
      for (const st of res.messages ?? []) {
        if (st.chat_id === msg.chat_id) msgsRef.current.patchMessage(st.tg_message_id, (m) => ({ ...m, ...st }))
      }
      applyFlagChanges(res.flags)
      toast.success(scope === 'sender' ? 'Sender dismissed' : 'Marked as not a scam')
    } catch (e) {
      reportError(e, 'Couldn’t update scam verdict')
    }
  }, [])

  // ── Avatar menu ────────────────────────────────────────────────────────────
  const onOpenProfile = useCallback((senderId: number) => navigate(`/profile/${senderId}`), [navigate])
  const onSendMessage = useCallback(async (senderId: number) => {
    try {
      onOpenChat(await ensureChat(senderId))
    } catch (e) {
      reportError(e, 'Couldn’t open a chat with this user')
    }
  }, [onOpenChat])
  const onMention = useCallback((_senderId: number, senderName: string) => {
    setMentionInsert(`@${senderName.replace(/\s+/g, '_')} `)
  }, [])
  const chatName = chat.name
  const onRestrict = useCallback((senderId: number, inChatId: number) => {
    void confirm({
      title: 'Restrict this user?',
      message: `They won't be able to send messages in ${chatName}.`,
      destructive: true,
      confirmLabel: 'Restrict',
      onConfirm: () => restrictMember(inChatId, senderId, NO_RESTRICTIONS),
    }).then((ok) => { if (ok) toast.success('User restricted') })
  }, [confirm, chatName])

  // ── Render ─────────────────────────────────────────────────────────────────
  const history = msgs.messages.length > 0 && !msgs.hasOlder ? (
    <div className="flex justify-center py-2 text-[11px] text-slate-500">
      {msgs.backfill === 'loading' ? (
        <span className="flex items-center gap-1.5"><Loader2 size={12} className="animate-spin" /> Loading older messages from Telegram…</span>
      ) : msgs.backfill === 'exhausted' ? (
        <span>Beginning of conversation</span>
      ) : (
        <span className="flex items-center gap-2">
          {msgs.backfillError && <span className="text-orange-300">{msgs.backfillError}</span>}
          <button onClick={() => void msgs.fetchOlderFromTelegram()} className="rounded border border-slate-600 px-2 py-0.5 hover:bg-slate-700 hover:text-slate-300">
            Load older messages from Telegram
          </button>
        </span>
      )}
    </div>
  ) : null

  const pinnedMsg = pinned.data
  const empty = msgs.messages.length === 0
  return (
    <div className="flex flex-1 flex-col overflow-hidden bg-slate-900">
      <div className="flex items-center justify-between border-b border-slate-700/50 bg-slate-800 px-4 py-3">
        <div className="flex items-center gap-2 min-w-0">
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold text-gray-100">{chat.name}</h2>
            <div className="flex items-center gap-1.5">
              <p className="text-xs text-slate-400"><ChatStatusLine chat={chat} /></p>
              {chat.is_forum && <span className="rounded px-1 text-[10px] font-medium bg-purple-500/20 text-purple-400">forum</span>}
              {topic && (
                <span className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium bg-indigo-500/20 text-indigo-400">
                  <MessageSquare size={9} />
                  {topic.title}
                </span>
              )}
            </div>
          </div>
          {chat.type === 'private' && (
            <button
              onClick={() => void toggleImportant.run()}
              disabled={toggleImportant.pending || important.loading}
              title={important.data ? 'Remove from important' : 'Mark as important'}
              className={`rounded-full p-1 transition-colors ${important.data ? 'text-amber-400 hover:text-amber-300' : 'text-slate-600 hover:text-amber-400'}`}
            >
              <Star size={16} className={important.data ? 'fill-amber-400' : ''} />
            </button>
          )}
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={() => onShowSearch(!showSearch)}
            title="Search in chat (⌘F)"
            className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs transition-colors ${showSearch ? 'bg-indigo-500/20 text-indigo-400' : 'text-slate-400 hover:bg-slate-700'}`}
          >
            <Search size={13} />
            Search
          </button>
          <button
            onClick={onToggleInfo}
            title="Chat info & tags (i)"
            className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs transition-colors ${showInfo ? 'bg-indigo-500/20 text-indigo-400' : 'text-slate-400 hover:bg-slate-700'}`}
          >
            <Info size={13} />
            Info
          </button>
          <button
            onClick={() => void handleSync()}
            disabled={syncing}
            title="Sync messages"
            className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs text-slate-400 hover:bg-slate-700 disabled:opacity-50"
          >
            <RefreshCw size={13} className={syncing ? 'animate-spin' : ''} />
            Sync
          </button>
        </div>
      </div>

      {showSearch && (
        <div className="border-b border-slate-700/50 bg-slate-800/50 px-4 py-2 flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <Search size={14} className="text-slate-500 flex-shrink-0" />
            <input
              autoFocus
              type="text"
              placeholder="Search messages…"
              value={chatSearch}
              onChange={(e) => setChatSearch(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Escape') { e.stopPropagation(); onShowSearch(false) } }}
              className="flex-1 bg-transparent text-sm text-gray-200 placeholder:text-slate-500 outline-none"
            />
            <input
              type="date"
              onChange={(e) => { const iso = localDayToIso(e.target.value); if (iso) void msgs.jumpToDate(iso) }}
              title="Jump to date"
              className="rounded border border-slate-600 bg-slate-700 px-2 py-0.5 text-[11px] text-slate-300 outline-none"
            />
            <button onClick={() => onShowSearch(false)} className="text-slate-500 hover:text-slate-300" aria-label="Close search">
              <X size={14} />
            </button>
          </div>
          {chatSearch.trim() && (
            <div className="text-[11px] text-slate-500">
              {searchResults.pending ? 'Searching…'
                : searchResults.error ? <span className="text-red-400">Search failed: {errorMessage(searchResults.error)}</span>
                : `${searchResults.data?.length ?? 0} result${searchResults.data?.length !== 1 ? 's' : ''}`}
            </div>
          )}
          {chatSearch.trim() && (searchResults.data?.length ?? 0) > 0 && (
            <div className="max-h-40 overflow-y-auto flex flex-col gap-0.5">
              {searchResults.data!.map((msg) => (
                <button
                  key={msg.tg_message_id}
                  onClick={() => void msgs.jumpTo(msg.tg_message_id)}
                  className="flex items-center gap-2 rounded px-2 py-1 text-left text-xs hover:bg-slate-700/50 transition-colors"
                >
                  <span className="text-slate-400 flex-shrink-0">{msg.sender_name || 'Unknown'}</span>
                  <span className="truncate text-slate-500">{msg.text || '[media]'}</span>
                  {msg.date && <span className="ml-auto flex-shrink-0 text-[10px] text-slate-600">{fmtShortDate(msg.date)}</span>}
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {chat.is_forum && (
        <ForumTopicsPanel telegramId={chatId} onSelectTopic={onSelectTopic} selectedTopicId={topic?.id ?? null} />
      )}

      {pinnedMsg && (
        <div
          role="button"
          tabIndex={0}
          className="flex items-center gap-2 border-b border-slate-700/50 bg-slate-800/50 px-4 py-2 cursor-pointer hover:bg-slate-700/50 transition-colors"
          onClick={() => void msgs.jumpTo(pinnedMsg.tg_message_id)}
          onKeyDown={(e) => { if (e.key === 'Enter') void msgs.jumpTo(pinnedMsg.tg_message_id) }}
        >
          <Pin size={14} className="text-indigo-400 flex-shrink-0" />
          {pinnedMsg.media_url && pinnedMsg.media_type === 'photo' && (
            <img src={authenticatedMediaUrl(pinnedMsg.media_url)} alt="" className="h-8 w-8 rounded object-cover flex-shrink-0" />
          )}
          <div className="min-w-0 flex-1">
            <p className="text-[11px] font-medium text-indigo-400">Pinned message</p>
            <p className="text-xs text-slate-300 truncate">
              {pinnedMsg.text ? pinnedMsg.text.slice(0, 120) : pinnedMsg.media_type ? `[${pinnedMsg.media_type}]` : ''}
            </p>
          </div>
          <button
            onClick={(e) => { e.stopPropagation(); pinned.setData(null) }}
            className="flex-shrink-0 text-slate-500 hover:text-slate-300 transition-colors"
            title="Dismiss pinned message"
          >
            <X size={14} />
          </button>
        </div>
      )}

      {msgs.syncFailed && (
        <div className="flex items-center gap-2 border-b border-orange-500/20 bg-orange-500/10 px-4 py-1.5 text-xs text-orange-300">
          <AlertTriangle size={13} className="flex-shrink-0" />
          <span className="flex-1">
            Couldn’t sync this chat from Telegram{msgs.syncFailed.error ? `: ${msgs.syncFailed.error}` : ''}
          </span>
          <button onClick={() => void handleSync()} disabled={syncing} className="underline hover:text-orange-100">Retry</button>
        </div>
      )}

      {msgs.replacing && msgs.jumpingTo && (
        <div role="status" className="flex items-center gap-2 border-b border-slate-700/50 bg-slate-800/60 px-4 py-1.5 text-xs text-slate-400">
          <Loader2 size={12} className="animate-spin text-indigo-400" />
          {msgs.jumpingTo.kind === 'message' ? 'Loading the message (fetching from Telegram if needed)…' : 'Loading messages from that date…'}
        </div>
      )}
      {msgs.jumpNotice && (
        <JumpNoticeBar
          notice={msgs.jumpNotice}
          onRetry={() => {
            const t = msgs.jumpNotice!.target
            void (t.kind === 'message' ? msgs.jumpTo(t.tgId) : msgs.jumpToDate(t.date))
          }}
          onDismiss={msgs.dismissJumpNotice}
        />
      )}

      {msgs.loading ? (
        <div className="flex flex-1 items-center justify-center text-sm text-slate-500">Loading…</div>
      ) : msgs.error && empty ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 px-8 text-center text-sm text-slate-500">
          <p className="text-red-400">Couldn’t load messages: {msgs.error}</p>
          <button onClick={() => void msgs.refresh()} className="rounded border border-slate-600 px-3 py-1 text-xs hover:bg-slate-700">Retry</button>
        </div>
      ) : empty && msgs.syncEmpty ? (
        <div className="flex flex-1 items-center justify-center px-8 text-center text-sm text-slate-500">
          No message history available — this group may have been migrated to a newer
          group or its history isn't accessible to this account.
        </div>
      ) : empty && !msgs.syncFailed && (msgs.syncing || chat.last_synced_at == null) ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 px-8 text-center text-sm text-slate-500">
          <Loader2 size={18} className="animate-spin text-indigo-400" />
          Syncing message history from Telegram — first-time sync can take a while…
        </div>
      ) : (
        <MessageList
          messages={msgs.messages}
          chatType={chat.type}
          highlightTelegramId={msgs.highlightTgId}
          scrollIntent={msgs.scrollIntent}
          firstUnreadTgId={msgs.firstUnreadTgId}
          header={history}
          loadingOlder={msgs.loadingOlder}
          loadingNewer={msgs.loadingNewer}
          hasNewer={msgs.hasNewer}
          onScrollNearTop={msgs.loadOlder}
          onScrollNearBottom={msgs.loadNewer}
          onJumpToLatest={msgs.jumpToLatest}
          onJumpToMessage={msgs.jumpTo}
          onReply={onReply}
          onReact={onReact}
          onEdit={onEdit}
          onDelete={onDelete}
          onForward={onForward}
          onPin={onPin}
          flaggedSenderIds={flagged.ids}
          onReportScam={shield ? onReportScam : undefined}
          onScamVerdict={shield ? onScamVerdict : undefined}
          onOpenProfile={onOpenProfile}
          onSendMessage={onSendMessage}
          onMention={onMention}
          onRestrict={chat.type === 'group' ? onRestrict : undefined}
        />
      )}

      <TypingBar chatId={chatId} />

      <ComposeArea
        chatId={chatId}
        autoFocus={!selection.viaKeyboard}
        onSend={handleSend}
        onSendFiles={handleSendFiles}
        onSaveEdit={handleSaveEdit}
        onTyping={handleTyping}
        insertText={mentionInsert}
        onInsertConsumed={clearMention}
      />

      <ChatPickerDialog
        open={!!forwardTarget}
        busy={forward.pending}
        onClose={() => setForwardTarget(null)}
        onSelect={(to) => { if (forwardTarget) void forward.run(forwardTarget, to) }}
      />

      {shield && reportTarget && (
        <ReportScamDialog
          chatId={reportTarget.chatId}
          targetMessageId={reportTarget.tgId}
          targetSenderId={reportTarget.senderId}
          onClose={() => setReportTarget(null)}
        />
      )}
    </div>
  )
}

/** Why a jump (search hit, reply quote, pinned banner, mention, date) didn't land */
export function jumpNoticeText(n: JumpNotice): string {
  const what = n.target.kind === 'message' ? 'that message' : 'messages at that date'
  switch (n.reason) {
    case 'not_found':
      return n.target.kind === 'message'
        ? 'That message isn’t available — it may have been deleted.'
        : 'No messages found at that date.'
    case 'unavailable': return `Telegram is offline — couldn’t load ${what}.`
    case 'rate_limited': return `Telegram rate limit — couldn’t load ${what} yet. Try again in a moment.`
    default: return `Couldn’t load ${what}${n.detail ? `: ${n.detail}` : '.'}`
  }
}

export function JumpNoticeBar({ notice, onRetry, onDismiss }: { notice: JumpNotice; onRetry: () => void; onDismiss: () => void }) {
  return (
    <div role="alert" className="flex items-center gap-2 border-b border-amber-500/20 bg-amber-500/10 px-4 py-1.5 text-xs text-amber-200">
      <AlertTriangle size={13} className="flex-shrink-0" />
      <span className="flex-1">{jumpNoticeText(notice)}</span>
      {notice.reason !== 'not_found' && (
        <button onClick={onRetry} className="underline hover:text-amber-50">Retry</button>
      )}
      <button onClick={onDismiss} aria-label="Dismiss" className="hover:text-amber-50"><X size={12} /></button>
    </div>
  )
}

/** Compose box bound to the chat's slot (draft, reply, edit target, upload progress). */
function ComposeArea({ chatId, autoFocus, onSend, onSendFiles, onSaveEdit, onTyping, insertText, onInsertConsumed }: {
  chatId: number
  autoFocus: boolean
  onSend: (text: string) => Promise<void>
  onSendFiles: (files: File[], text: string) => Promise<FilesSendOutcome>
  onSaveEdit: (text: string) => Promise<void>
  onTyping: () => void
  insertText: string | null
  onInsertConsumed: () => void
}) {
  const ui = useChatUi(chatId)
  const onDraftChange = useCallback((draft: ComposeDraft) => updateChatUi(chatId, { draft }), [chatId])
  return (
    <ComposeBar
      autoFocus={autoFocus}
      onSend={onSend}
      onSendFiles={onSendFiles}
      busy={ui.sending}
      replyTo={ui.replyTo}
      onCancelReply={() => updateChatUi(chatId, { replyTo: null })}
      editingMessage={ui.editing}
      onSaveEdit={onSaveEdit}
      onCancelEdit={() => updateChatUi(chatId, { editing: null })}
      onTyping={onTyping}
      uploadProgress={ui.uploadProgress}
      insertText={insertText}
      onInsertConsumed={onInsertConsumed}
      initialDraft={ui.draft}
      onDraftChange={onDraftChange}
    />
  )
}

export default memo(ChatPane)
