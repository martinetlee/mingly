import type { Conversation } from '../../../api/types'
import { conversationTitle, kindColor, kindLabel } from '../../../lib/conversation'
import PersonCard from '../../people/PersonCard'
import ChannelIcon from '../../channels/ChannelIcon'
import ConversationAvatar from '../ConversationAvatar'

/** PersonCard for a direct conversation's peer, simple display otherwise */
export default function ConversationIdentity({ chat }: { chat: Conversation }) {
  const typeColor = kindColor(chat.kind)
  const peerPersonId = chat.kind === 'direct' ? chat.peer?.person_id ?? null : null
  const title = conversationTitle(chat)

  if (peerPersonId != null) {
    return (
      <div className="px-4 py-5 border-b border-slate-700/50">
        <PersonCard
          personId={peerPersonId}
          name={title}
          username={chat.username}
          mode="full"
          showStatus
        />
      </div>
    )
  }
  return (
    <div className="flex flex-col items-center gap-2 px-4 py-5 border-b border-slate-700/50">
      <ConversationAvatar conversation={chat} size={56} />
      <p className="text-sm font-semibold text-gray-100 text-center leading-snug">{title}</p>
      <span className="flex items-center gap-1.5">
        <ChannelIcon channel={chat.channel} />
        <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${typeColor}`}>{kindLabel(chat.kind)}</span>
      </span>
    </div>
  )
}
