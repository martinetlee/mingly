/**
 * Right pane: header, in-chat search, forum topics, pinned banner, message
 * window and compose box for ONE conversation. Chats mounts it with
 * `key={chat.id}`, so nothing here can outlive a conversation switch;
 * per-chat state that must survive (draft, reply, edit, upload progress)
 * lives in chatUiStore, and every async action writes to the chat it was
 * started for (captured ids — never "the currently selected chat").
 *
 * Features render only when the conversation's capabilities include them
 * (lib/capabilities): reply, react, edit, delete, forward, pin, send,
 * media_send, typing, forum_topics, admin_moderation, history_backfill.
 */
import { memo, useCallback, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { RefreshCw, Info, X, Search, Star, Loader2, MessageSquare, Pin, AlertTriangle } from 'lucide-react'
import type { Conversation, Message, MessageSender, Reaction, SendFileResult, SendFilesResponse } from '../../api/types'
import {
  ApiError,
  deleteMessages, editMessage, forwardMessages, getPerson, getPinnedMessage, pinMessage,
  reactToMessage, restrictMember, searchConversationMessages, sendFiles, sendMessage, sendTypingAction,
  setPersonImportant, syncConversation,
} from '../../api/client'
import { authenticatedMediaUrl } from '../../api/session'
import { useChatMessages } from '../../hooks/useChatMessages'
import { useLatestQuery } from '../../hooks/useLatestQuery'
import { useDebouncedQuery } from '../../hooks/useDebouncedQuery'
import { useAsyncAction } from '../../hooks/useAsyncAction'
import { useWsEvent } from '../../hooks/useWebSocket'
import { useFlaggedSenders } from '../../hooks/useFlaggedSenders'
import { useFeatures } from '../../context/FeaturesContext'
import { useConfirm } from '../../components/ui'
import { errorMessage, reportError, toast } from '../../lib/toast'
import { fmtShortDate } from '../../utils/time'
import { can } from '../../lib/capabilities'
import { conversationTitle, isMultiParty } from '../../lib/conversation'
import { personUrl } from '../../lib/routes'
import { NoConversationError, openDirectConversation } from '../../lib/directConversation'
import ChannelIcon from '../../components/ChannelIcon'
import { HistoryHeader, JumpingStatus, PaneJumpNotice } from '../../components/MessageWindowStatus'
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
import { submitNotScamVerdict } from './scamChecks'

export interface ChatPaneProps {
  chat: Conversation
  topic: { id: number; title: string } | null
  /** Bumped by selectChat; `anchorMessageId` opens the conversation centred on a message */
  selection: { anchorMessageId: number | null; anchorExternalId?: string | null; unreadCount: number; nonce: number; viaKeyboard?: boolean }
  showInfo: boolean
  onToggleInfo: () => void
  showSearch: boolean
  onShowSearch: (open: boolean) => void
  onSelectTopic: (topic: { id: number; title: string } | null) => void
  /** Open another conversation through the page's single selectChat entry point */
  onOpenChat: (chat: Conversation) => void
  /** Own send landed: update the conversation list preview */
  onChatActivity: (conversationId: number, preview: { text: string; date: string }) => void
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
  const chatId = chat.id
  const navigate = useNavigate()
  const confirm = useConfirm()
  // Only the sync flag here: the compose slot changes per keystroke and is read by ComposeArea
  const syncing = useChatUiSelect(chatId, (u) => u.syncing)
  const flagged = useFlaggedSenders()
  const { shield } = useFeatures()

  const canBackfill = can(chat, 'history_backfill')
  const msgs = useChatMessages({
    conversationId: chatId,
    topicId: topic?.id ?? null,
    anchorMessageId: selection.anchorMessageId,
    anchorExternalId: selection.anchorExternalId ?? null,
    unreadCount: selection.unreadCount,
    nonce: selection.nonce,
    canBackfill,
  })
  const msgsRef = useRef(msgs)
  msgsRef.current = msgs

  // ── Header data (keyed by conversation; aborted on switch) ─────────────────
  const canPin = can(chat, 'pin')
  const pinned = useLatestQuery(['pinned', chatId], (signal) => getPinnedMessage(chatId, signal), { enabled: canPin })
  const refetchPinned = useRef(pinned.refetch)
  refetchPinned.current = pinned.refetch
  useWsEvent('messages_pinned', (e) => { if (e.conversation_id === chatId) void pinned.refetch() }, canPin)
  // A direct conversation's peer is a person: the star marks them important
  const peerPersonId = chat.kind === 'direct' ? chat.peer?.person_id ?? null : null
  const important = useLatestQuery(
    ['important', peerPersonId],
    (signal) => getPerson(peerPersonId as number, signal).then((p) => p.is_important).catch(() => false),
    { enabled: peerPersonId != null },
  )
  const toggleImportant = useAsyncAction(async () => {
    if (peerPersonId == null) return
    const updated = await setPersonImportant(peerPersonId, !important.data)
    important.setData(updated.is_important)
  }, { errorContext: 'Couldn’t update important person' })

  // ── In-chat search ─────────────────────────────────────────────────────────
  const [chatSearch, setChatSearch] = useState('')
  const searchResults = useDebouncedQuery(chatSearch, (q, signal) => searchConversationMessages(chatId, q, 50, signal), { delayMs: 300, enabled: showSearch })

  // ── Manual sync ────────────────────────────────────────────────────────────
  const handleSync = useCallback(async () => {
    if (getChatUi(chatId).syncing) return
    updateChatUi(chatId, { syncing: true })
    try {
      const res = await syncConversation(chatId)
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
    const key = sendKey(text, [], replyTo?.id, topicId)
    const nonce = before.failedSend?.key === key ? before.failedSend.nonce : newNonce()
    updateChatUi(chatId, { sending: true })
    try {
      const res = await sendMessage(chatId, text, replyTo?.id, { topicId, clientNonce: nonce })
      updateChatUi(chatId, (p) => ({
        sending: false,
        failedSend: null,
        replyTo: p.replyTo?.id === replyTo?.id ? null : p.replyTo,
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
    const key = sendKey(text, files, replyTo?.id, topicId)
    // Reuse the nonce only after a transport failure (the server may have sent it).
    // When the server answered with per-file failures, a retry needs a NEW nonce
    // (the same nonce would just replay that response).
    const nonce = before.failedSend?.key === key ? before.failedSend.nonce : newNonce()
    updateChatUi(chatId, { sending: true, uploadProgress: 0 })
    let res: SendFilesResponse
    try {
      const { promise } = sendFiles(chatId, files, text || undefined, replyTo?.id,
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
      replyTo: p.replyTo?.id === replyTo?.id ? null : p.replyTo,
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
    const res = await editMessage(target.id, text)
    updateChatUi(target.conversation_id, (p) => (p.editing?.id === target.id ? { editing: null } : {}))
    msgsRef.current.applyMessage(res.message ?? { ...target, text, text_html: null, is_edited: true })
  }, [chatId])

  const handleTyping = useCallback(() => {
    sendTypingAction(chatId).catch((e) => console.debug('typing action failed', e))
  }, [chatId])

  const [mentionInsert, setMentionInsert] = useState<string | null>(null)
  const clearMention = useCallback(() => setMentionInsert(null), [])

  // ── Message actions (targets come from the message itself) ─────────────────
  const onReply = useCallback((msg: Message) => updateChatUi(msg.conversation_id, { replyTo: msg, editing: null }), [])
  const onEdit = useCallback((msg: Message) => updateChatUi(msg.conversation_id, { editing: msg, replyTo: null }), [])

  const onReact = useCallback(async (msg: Message, emoji: string | null) => {
    const m = msgsRef.current
    m.patchMessage(msg.id, (cur) => applyReaction(cur, emoji))
    try {
      await reactToMessage(msg.id, emoji)
    } catch (e) {
      m.patchMessage(msg.id, (cur) => applyReaction(cur, msg.my_reaction ?? null))
      reportError(e, 'Reaction failed')
    }
  }, [])

  const onPin = useCallback(async (msg: Message) => {
    const next = !msg.is_pinned
    try {
      await pinMessage(msg.id, next)
      msgsRef.current.patchMessage(msg.id, { is_pinned: next })
      void refetchPinned.current()
      toast.success(next ? 'Message pinned' : 'Message unpinned')
    } catch (e) {
      reportError(e, next ? 'Pin failed' : 'Unpin failed')
    }
  }, [])

  const onDelete = useCallback(async (msg: Message, album?: Message[]) => {
    // Target captured at open: {conversation, ids} — confirm can't act on another conversation
    const targetChat = msg.conversation_id
    const ids = (album ?? [msg]).map((m) => m.id)
    const preview = msg.text?.slice(0, 80) ?? (msg.attachments?.[0] ? `[${msg.attachments[0].kind}]` : 'this message')
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
    setForwardTarget({ fromChatId: msg.conversation_id, ids: (album ?? [msg]).map((m) => m.id) })
  }, [])
  const forward = useAsyncAction(async (target: { fromChatId: number; ids: number[] }, to: Conversation) => {
    await forwardMessages(target.fromChatId, target.ids, to.id)
    toast.success(`Forwarded to ${conversationTitle(to)}`)
    setForwardTarget(null)
  }, { errorContext: 'Forward failed' })

  const [reportTarget, setReportTarget] = useState<{ conversationId: number; messageId: number; identityId: number } | null>(null)
  const onReportScam = useCallback((msg: Message) => {
    const identityId = msg.sender?.identity_id
    if (identityId == null) { toast.info('This message has no reportable sender'); return }
    setReportTarget({ conversationId: msg.conversation_id, messageId: msg.id, identityId })
  }, [])

  const onScamVerdict = useCallback(async (msg: Message, scope: ScamVerdictScope) => {
    const states = await submitNotScamVerdict(msg, scope)
    if (!states) return
    for (const st of states) {
      if (st.conversation_id === msg.conversation_id) msgsRef.current.patchMessage(st.message_id, (m) => ({ ...m, ...st }))
    }
  }, [])

  // ── Avatar menu ────────────────────────────────────────────────────────────
  const onOpenProfile = useCallback((sender: MessageSender) => {
    if (sender.person_id != null) navigate(personUrl(sender.person_id))
  }, [navigate])
  // Their existing direct conversation on any channel, else a new Telegram one
  // (a sender in a Telegram conversation has a Telegram identity). From a
  // messenger chat it stays in messengers: never their email thread.
  const canStartFromHere = chat.channel === 'telegram'
  const messengersOnly = chat.kind !== 'email_thread'
  const onSendMessage = useCallback(async (sender: MessageSender) => {
    if (sender.person_id == null) return
    try {
      onOpenChat(await openDirectConversation(sender.person_id, { canStart: canStartFromHere, messengersOnly }))
    } catch (e) {
      // Same as Home's "Message" button: not an error, just nothing to open yet
      if (e instanceof NoConversationError) toast.info(`No conversation with ${sender.name || 'this person'} yet`)
      else reportError(e, 'Couldn’t open a chat with this user')
    }
  }, [onOpenChat, canStartFromHere, messengersOnly])
  const onMention = useCallback((sender: MessageSender) => {
    if (sender.name) setMentionInsert(`@${sender.name.replace(/\s+/g, '_')} `)
  }, [])
  const chatName = conversationTitle(chat)
  const onRestrict = useCallback((sender: MessageSender, inConversationId: number) => {
    const identityId = sender.identity_id
    if (identityId == null) return
    void confirm({
      title: 'Restrict this user?',
      message: `They won't be able to send messages in ${chatName}.`,
      destructive: true,
      confirmLabel: 'Restrict',
      onConfirm: () => restrictMember(inConversationId, identityId, NO_RESTRICTIONS),
    }).then((ok) => { if (ok) toast.success('User restricted') })
  }, [confirm, chatName])

  // ── Render ─────────────────────────────────────────────────────────────────
  const history = <HistoryHeader msgs={msgs} canBackfill={canBackfill} />
  const canSend = can(chat, 'send')
  const canForwardTo = useCallback((to: Conversation) => to.channel === chat.channel && can(to, 'send'), [chat.channel])

  const pinnedMsg = pinned.data
  const pinnedPhoto = pinnedMsg?.attachments?.find((a) => a.kind === 'photo')
  const empty = msgs.messages.length === 0
  return (
    <div className="flex flex-1 flex-col overflow-hidden bg-slate-900">
      <div className="flex items-center justify-between border-b border-slate-700/50 bg-slate-800 px-4 py-3">
        <div className="flex items-center gap-2 min-w-0">
          <div className="min-w-0">
            <h2 className="flex items-center gap-1.5 text-sm font-semibold text-gray-100">
              <span className="truncate">{chatName}</span>
              <ChannelIcon channel={chat.channel} size={11} />
            </h2>
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
          {peerPersonId != null && (
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
          {/* Pulling from the channel on demand: only channels that can fetch history */}
          {canBackfill && (
            <button
              onClick={() => void handleSync()}
              disabled={syncing}
              title="Sync messages"
              className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs text-slate-400 hover:bg-slate-700 disabled:opacity-50"
            >
              <RefreshCw size={13} className={syncing ? 'animate-spin' : ''} />
              Sync
            </button>
          )}
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
                  key={msg.id}
                  onClick={() => void msgs.jumpTo(msg.id)}
                  className="flex items-center gap-2 rounded px-2 py-1 text-left text-xs hover:bg-slate-700/50 transition-colors"
                >
                  <span className="text-slate-400 flex-shrink-0">{msg.sender?.name || 'Unknown'}</span>
                  <span className="truncate text-slate-500">{msg.text || '[media]'}</span>
                  {msg.date && <span className="ml-auto flex-shrink-0 text-[10px] text-slate-600">{fmtShortDate(msg.date)}</span>}
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {chat.is_forum && can(chat, 'forum_topics') && (
        <ForumTopicsPanel conversationId={chatId} onSelectTopic={onSelectTopic} selectedTopicId={topic?.id ?? null} />
      )}

      {canPin && pinnedMsg && (
        <div
          role="button"
          tabIndex={0}
          className="flex items-center gap-2 border-b border-slate-700/50 bg-slate-800/50 px-4 py-2 cursor-pointer hover:bg-slate-700/50 transition-colors"
          onClick={() => void msgs.jumpTo(pinnedMsg.id)}
          onKeyDown={(e) => { if (e.key === 'Enter') void msgs.jumpTo(pinnedMsg.id) }}
        >
          <Pin size={14} className="text-indigo-400 flex-shrink-0" />
          {pinnedPhoto?.url && (
            <img src={authenticatedMediaUrl(pinnedPhoto.url)} alt="" className="h-8 w-8 rounded object-cover flex-shrink-0" />
          )}
          <div className="min-w-0 flex-1">
            <p className="text-[11px] font-medium text-indigo-400">Pinned message</p>
            <p className="text-xs text-slate-300 truncate">
              {pinnedMsg.text ? pinnedMsg.text.slice(0, 120) : pinnedMsg.attachments?.[0] ? `[${pinnedMsg.attachments[0].kind}]` : ''}
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
            Couldn’t sync this chat{msgs.syncFailed.error ? `: ${msgs.syncFailed.error}` : ''}
          </span>
          <button onClick={() => void handleSync()} disabled={syncing} className="underline hover:text-orange-100">Retry</button>
        </div>
      )}

      <JumpingStatus msgs={msgs} />
      <PaneJumpNotice msgs={msgs} />

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
          Syncing message history — first-time sync can take a while…
        </div>
      ) : (
        <MessageList
          messages={msgs.messages}
          multiParty={isMultiParty(chat)}
          channel={chat.channel}
          conversationKind={chat.kind}
          highlightMessageId={msgs.highlightMessageId}
          scrollIntent={msgs.scrollIntent}
          firstUnreadMessageId={msgs.firstUnreadMessageId}
          header={history}
          loadingOlder={msgs.loadingOlder}
          loadingNewer={msgs.loadingNewer}
          hasNewer={msgs.hasNewer}
          onScrollNearTop={msgs.loadOlder}
          onScrollNearBottom={msgs.loadNewer}
          onJumpToLatest={msgs.jumpToLatest}
          onJumpToMessage={msgs.jumpTo}
          onJumpToExternal={msgs.jumpToExternal}
          onReply={can(chat, 'reply') ? onReply : undefined}
          onReact={can(chat, 'react') ? onReact : undefined}
          onEdit={can(chat, 'edit') ? onEdit : undefined}
          onDelete={can(chat, 'delete') ? onDelete : undefined}
          onForward={can(chat, 'forward') ? onForward : undefined}
          onPin={canPin ? onPin : undefined}
          flaggedSenderIds={flagged.ids}
          onReportScam={shield ? onReportScam : undefined}
          onScamVerdict={shield ? onScamVerdict : undefined}
          onOpenProfile={onOpenProfile}
          onSendMessage={onSendMessage}
          onMention={canSend ? onMention : undefined}
          onRestrict={chat.kind === 'group' && can(chat, 'admin_moderation') ? onRestrict : undefined}
        />
      )}

      <TypingBar chat={chat} />

      {canSend && (
        <ComposeArea
          chatId={chatId}
          autoFocus={!selection.viaKeyboard}
          onSend={handleSend}
          onSendFiles={can(chat, 'media_send') ? handleSendFiles : undefined}
          onSaveEdit={handleSaveEdit}
          onTyping={can(chat, 'typing') ? handleTyping : undefined}
          insertText={mentionInsert}
          onInsertConsumed={clearMention}
        />
      )}

      <ChatPickerDialog
        open={!!forwardTarget}
        busy={forward.pending}
        filter={canForwardTo}
        onClose={() => setForwardTarget(null)}
        onSelect={(to) => { if (forwardTarget) void forward.run(forwardTarget, to) }}
      />

      {shield && reportTarget && (
        <ReportScamDialog
          conversationId={reportTarget.conversationId}
          targetMessageId={reportTarget.messageId}
          targetIdentityId={reportTarget.identityId}
          onClose={() => setReportTarget(null)}
        />
      )}
    </div>
  )
}

/** Compose box bound to the conversation's slot (draft, reply, edit target, upload progress). */
function ComposeArea({ chatId, autoFocus, onSend, onSendFiles, onSaveEdit, onTyping, insertText, onInsertConsumed }: {
  chatId: number
  autoFocus: boolean
  onSend: (text: string) => Promise<void>
  onSendFiles?: (files: File[], text: string) => Promise<FilesSendOutcome>
  onSaveEdit: (text: string) => Promise<void>
  onTyping?: () => void
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
