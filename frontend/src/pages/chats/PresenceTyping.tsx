/**
 * Presence + typing indicators. Each component subscribes to its own WS
 * events and holds its own state, so a `user_typing` event (any chat, many
 * per second in busy groups) re-renders only these few nodes — not the Chats
 * page, the chat list or the message list.
 */
import { memo, useEffect, useState } from 'react'
import type { Chat, UserPresence } from '../../api/types'
import { getUserPresence } from '../../api/client'
import { useLatestQuery } from '../../hooks/useLatestQuery'
import { useWsEvent } from '../../hooks/useWebSocket'
import { fmtDate } from '../../utils/time'

const TYPING_MS = 5000

/** Name of whoever is typing in `chatId`, cleared TYPING_MS after the last event. */
function useTyping(chatId: number): string | null {
  const [typing, setTyping] = useState<{ name: string; until: number } | null>(null)
  useEffect(() => { setTyping(null) }, [chatId])
  useWsEvent('user_typing', (e) => {
    if (e.chat_id === chatId) setTyping({ name: e.user_name, until: Date.now() + TYPING_MS })
  })
  useEffect(() => {
    if (!typing) return
    const t = setTimeout(() => setTyping(null), Math.max(0, typing.until - Date.now()))
    return () => clearTimeout(t)
  }, [typing])
  return typing?.name ?? null
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

/** Header sub-line: "typing…", presence for 1:1 chats, else the chat type. */
export const ChatStatusLine = memo(function ChatStatusLine({ chat }: { chat: Chat }) {
  const typingName = useTyping(chat.telegram_id)
  const isPrivate = chat.type === 'private'
  const { data, setData } = useLatestQuery<UserPresence | null>(
    ['presence', chat.telegram_id],
    (signal) => getUserPresence(chat.telegram_id, signal).catch(() => null),
    { enabled: isPrivate },
  )
  useWsEvent('user_status', (e) => {
    if (isPrivate && e.user_id === chat.telegram_id) {
      setData({ status: e.status as UserPresence['status'], last_seen: e.last_seen })
    }
  })
  if (typingName) return <span className="text-indigo-400">typing…</span>
  const label = isPrivate ? presenceLabel(data) : null
  if (label) return <span className={label.online ? 'text-green-400' : undefined}>{label.text}</span>
  return <>{chat.type}</>
})

/** "<name> is typing…" bar above the compose box. */
export const TypingBar = memo(function TypingBar({ chatId }: { chatId: number }) {
  const name = useTyping(chatId)
  if (!name) return null
  return <div className="px-4 py-1 text-xs text-indigo-400 animate-pulse">{name} is typing…</div>
})
