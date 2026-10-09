import type { Conversation, Message } from '../api/types'
import type { ChatMessages } from '../hooks/useChatMessages'
import { can } from '../lib/capabilities'
import { isMultiParty } from '../lib/conversation'
import MessageList from './MessageList'
import { HistoryHeader, JumpingStatus, PaneJumpNotice } from './MessageWindowStatus'

/**
 * The message list of a `useChatMessages` pane for pages that embed a
 * conversation (Contacts, Tracked). Same paging/jump/backfill behaviour as the
 * Chats pane: scroll near the top loads older (then from the channel), "Jump
 * to latest" when the window isn't at the newest message.
 */
export default function ChatMessagesView({
  msgs,
  conversation,
  onReply,
  onReact,
}: {
  msgs: ChatMessages
  conversation: Pick<Conversation, 'kind' | 'channel' | 'capabilities'>
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
        <p className="text-xs text-slate-600 mt-1">Send a message or sync to load the history</p>
      </div>
    )
  }

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      {/* Same jump feedback as the Chats pane: a jump that can't land keeps the window and says why */}
      <JumpingStatus msgs={msgs} />
      <PaneJumpNotice msgs={msgs} />
      <MessageList
        messages={msgs.messages}
        multiParty={isMultiParty(conversation)}
        channel={conversation.channel}
        conversationKind={conversation.kind}
        highlightMessageId={msgs.highlightMessageId}
        scrollIntent={msgs.scrollIntent}
        firstUnreadMessageId={msgs.firstUnreadMessageId}
        header={<HistoryHeader msgs={msgs} canBackfill={can(conversation, 'history_backfill')} />}
        loadingOlder={msgs.loadingOlder}
        loadingNewer={msgs.loadingNewer}
        hasNewer={msgs.hasNewer}
        onScrollNearTop={msgs.loadOlder}
        onScrollNearBottom={msgs.loadNewer}
        onJumpToLatest={msgs.jumpToLatest}
        onJumpToMessage={msgs.jumpTo}
        onJumpToExternal={msgs.jumpToExternal}
        onReply={onReply}
        onReact={onReact}
      />
    </div>
  )
}
