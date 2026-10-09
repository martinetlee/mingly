/**
 * Presence + typing indicators. Each component subscribes to its own WS
 * events and holds its own state, so a `user_typing` event (any conversation,
 * many per second in busy groups) re-renders only these few nodes — not the Chats
 * page, the conversation list or the message list. Both render only when the
 * conversation has the 'typing' / 'presence' capability.
 */
import { memo, useEffect, useState } from 'react'
import type { Conversation, UserPresence } from '../../api/types'
import { getPeerPresence } from '../../api/client'
import { useLatestQuery } from '../../hooks/useLatestQuery'
import { useWsEvent } from '../../hooks/useWebSocket'
import { can } from '../../lib/capabilities'
import { kindLabel } from '../../lib/conversation'
import { fmtDate } from '../../utils/time'

const TYPING_MS = 5000

/** Name of whoever is typing in `conversationId`, cleared TYPING_MS after the last event. */
function useTyping(conversationId: number, enabled: boolean): string | null {
  const [typing, setTyping] = useState<{ name: string; until: number } | null>(null)
  useEffect(() => { setTyping(null) }, [conversationId])
  useWsEvent('user_typing', (e) => {
    if (e.conversation_id === conversationId) setTyping({ name: e.name, until: Date.now() + TYPING_MS })
  }, enabled)
  useEffect(() => {
    if (!typing) return
    const t = setTimeout(() => setTyping(null), Math.max(0, typing.until - Date.now()))
    return () => clearTimeout(t)
  }, [typing])
  return enabled ? typing?.name ?? null : null
}

function presenceLabel(p: UserPresence | null | undefined): { text: string; online?: boolean } | null {
  if (!p) return null
  switch (p.status) {
    case 'online': return { text: 'online', online: true }
    case 'offline': return p.last_seen ? { text: `last seen ${fmtDate(p.last_seen)}` } : null
    case 'recently': return { text: 'last seen recently' }
    case 'last_week': return { text: 'last seen within a week' }
    case 'last_month': return { text: 'last seen within a month' }
    default: return null
  }
}

type StatusConversation = Pick<Conversation, 'id' | 'kind' | 'peer' | 'capabilities'>

/** Header sub-line: "typing…", the peer's presence for direct conversations, else the kind. */
export const ChatStatusLine = memo(function ChatStatusLine({ chat }: { chat: StatusConversation }) {
  const typingName = useTyping(chat.id, can(chat, 'typing'))
  const peerIdentityId = chat.peer?.identity_id ?? null
  const showPresence = chat.kind === 'direct' && peerIdentityId != null && can(chat, 'presence')
  const { data, setData } = useLatestQuery<UserPresence | null>(
    ['presence', chat.id],
    (signal) => getPeerPresence(chat.id, signal).catch(() => null),
    { enabled: showPresence },
  )
  useWsEvent('user_status', (e) => {
    if (showPresence && e.identity_id === peerIdentityId) {
      setData({ status: e.status as UserPresence['status'], last_seen: e.last_seen })
    }
  })
  if (typingName) return <span className="text-indigo-400">typing…</span>
  const label = showPresence ? presenceLabel(data) : null
  if (label) return <span className={label.online ? 'text-green-400' : undefined}>{label.text}</span>
  return <>{kindLabel(chat.kind)}</>
})

/** "<name> is typing…" bar above the compose box. */
export const TypingBar = memo(function TypingBar({ chat }: { chat: Pick<Conversation, 'id' | 'capabilities'> }) {
  const name = useTyping(chat.id, can(chat, 'typing'))
  if (!name) return null
  return <div className="px-4 py-1 text-xs text-indigo-400 animate-pulse">{name} is typing…</div>
})
