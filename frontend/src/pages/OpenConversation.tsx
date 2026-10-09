/**
 * /open/:conversationId[?messageId=] — resolves a conversation id to its
 * space (Mail for email threads, Chats otherwise) and redirects there. Used
 * by links that only carry ids (citations, notifications, search hits).
 */
import { useEffect, useState } from 'react'
import { Navigate, useParams, useSearchParams } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { getConversation } from '../api/client'
import { chatUrl, conversationUrl } from '../lib/routes'

export default function OpenConversation() {
  const { conversationId } = useParams()
  const [params] = useSearchParams()
  const id = Number(conversationId)
  const messageIdRaw = params.get('messageId')
  const messageId = messageIdRaw != null && messageIdRaw !== '' ? Number(messageIdRaw) : null
  const [target, setTarget] = useState<string | null>(null)

  useEffect(() => {
    if (!Number.isFinite(id)) return
    const ctl = new AbortController()
    getConversation(id, ctl.signal)
      .then((conv) => setTarget(conversationUrl(conv, messageId)))
      // Unknown / unreadable: Chats shows its own "not found" handling
      .catch(() => { if (!ctl.signal.aborted) setTarget(chatUrl(id, messageId)) })
    return () => ctl.abort()
  }, [id, messageId])

  if (!Number.isFinite(id)) return <Navigate to="/chats" replace />
  if (target) return <Navigate to={target} replace />
  return (
    <div className="flex flex-1 items-center justify-center text-slate-500">
      <Loader2 size={18} className="animate-spin" />
    </div>
  )
}
