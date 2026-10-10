import type { Conversation } from '../../api/types'
import SenderAvatar from './SenderAvatar'
import { conversationTitle } from '../../lib/conversation'

/** A conversation's avatar (its own, or a direct conversation's peer) — never a link */
export default function ConversationAvatar({
  conversation,
  size = 36,
}: {
  conversation: Pick<Conversation, 'id' | 'title' | 'avatar_url' | 'kind'>
  size?: number
}) {
  return (
    <SenderAvatar
      avatarUrl={conversation.avatar_url}
      name={conversationTitle(conversation)}
      colorSeed={conversation.id}
      size={size}
      link={false}
    />
  )
}
