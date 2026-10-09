/**
 * /mention/:channel/:externalId — resolves a channel handle (Telegram user id
 * or @username) to the stored person or conversation and redirects there.
 */
import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { isAbortError } from '../api/client'
import { resolveMention } from '../lib/mentions'
import { errorMessage } from '../lib/toast'

export default function Mention() {
  const { channel = '', externalId = '' } = useParams<{ channel: string; externalId: string }>()
  const navigate = useNavigate()
  const [failure, setFailure] = useState<string | null>(null)

  useEffect(() => {
    const ac = new AbortController()
    setFailure(null)
    resolveMention(channel, externalId, ac.signal).then(
      (to) => {
        if (ac.signal.aborted) return
        if (to) navigate(to, { replace: true })
        else setFailure(externalId.startsWith('@') ? `${externalId} isn't known yet` : 'This person isn’t known yet')
      },
      (e) => { if (!isAbortError(e) && !ac.signal.aborted) setFailure(errorMessage(e)) },
    )
    return () => ac.abort()
  }, [channel, externalId, navigate])

  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 text-sm text-slate-500">
      {failure ? (
        <>
          <p>{failure}</p>
          <button onClick={() => navigate(-1)} className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors">Go back</button>
        </>
      ) : (
        <span className="flex items-center gap-2"><Loader2 size={14} className="animate-spin" /> Opening…</span>
      )}
    </div>
  )
}
