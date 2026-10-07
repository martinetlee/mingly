import { Loader2 } from 'lucide-react'
import type { Message } from '../api/types'
import type { ChatMessages } from '../hooks/useChatMessages'
import MessageList from './MessageList'
import { JumpNoticeBar } from '../pages/chats/ChatPane'

/**
 * The message list of a `useChatMessages` pane for pages that embed a chat
 * (Contacts, Tracked). Same paging/jump/backfill behaviour as the Chats pane:
 * scroll near the top loads older (then from Telegram), "Jump to latest" when
 * the window isn't at the newest message.
 */
export default function ChatMessagesView({
  msgs,
  chatType,
  onReply,
  onReact,
}: {
  msgs: ChatMessages
  chatType: string
  onReply?: (msg: Message) => void
  onReact?: (msg: Message, emoji: string | null) => void
}) {
  if (msgs.loading) {
    return <div className="flex flex-1 items-center justify-center text-sm text-slate-500">Loading…</div>
  }
  if (msgs.error && msgs.messages.length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-2 text-sm text-rose-400">
        Couldn't load messages: {msgs.error}
        <button onClick={() => void msgs.refresh()} className="text-xs text-indigo-400 hover:text-indigo-300">Retry</button>
      </div>
    )
  }
  if (msgs.messages.length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center text-slate-500">
        <p className="text-sm">{msgs.syncing ? 'Syncing messages…' : 'No messages yet'}</p>
        <p className="text-xs text-slate-600 mt-1">Send a message or sync to load chat history</p>
      </div>
    )
  }

  const history = !msgs.hasOlder ? (
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

  const notice = msgs.jumpNotice
  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      {/* Same jump feedback as the Chats pane: a jump that can't land keeps the window and says why */}
      {msgs.replacing && msgs.jumpingTo && (
        <div role="status" className="flex items-center gap-2 border-b border-slate-700/50 bg-slate-800/60 px-4 py-1.5 text-xs text-slate-400">
          <Loader2 size={12} className="animate-spin text-indigo-400" />
          {msgs.jumpingTo.kind === 'message' ? 'Loading the message (fetching from Telegram if needed)…' : 'Loading messages from that date…'}
        </div>
      )}
      {notice && (
        <JumpNoticeBar
          notice={notice}
          onRetry={() => void (notice.target.kind === 'message' ? msgs.jumpTo(notice.target.tgId) : msgs.jumpToDate(notice.target.date))}
          onDismiss={msgs.dismissJumpNotice}
        />
      )}
      <MessageList
        messages={msgs.messages}
        chatType={chatType}
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
      />
    </div>
  )
}
