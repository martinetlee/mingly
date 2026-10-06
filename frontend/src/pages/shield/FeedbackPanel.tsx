import { useState } from 'react'
import { Send } from 'lucide-react'
import { submitFeedback } from '../../api/client'
import { useAsyncAction } from '../../hooks/useAsyncAction'
import { errorMessage } from '../../lib/toast'

type Result = { kind: 'sent' | 'saved' | 'error'; text: string }

export default function FeedbackPanel() {
  const [type, setType] = useState('general')
  const [msg, setMsg] = useState('')
  const [result, setResult] = useState<Result | null>(null)

  const send = useAsyncAction(async () => {
    setResult(null)
    try {
      const res = await submitFeedback({ feedback_type: type, message: msg })
      setResult(
        res.status === 'synced' ? { kind: 'sent', text: 'Sent — thank you!' }
          : res.sync_error ? { kind: 'saved', text: 'Saved locally — cloud sync failed' }
            : { kind: 'saved', text: 'Saved locally — will sync later' },
      )
      setMsg('')
      setType('general')
    } catch (e) {
      setResult({ kind: 'error', text: errorMessage(e) })
    }
  })

  return (
    <div className="border-b border-slate-700 bg-slate-800/50 px-6 py-4">
      <div className="max-w-xl space-y-3">
        <h3 className="text-xs font-medium text-slate-400">Send Feedback</h3>
        <div className="flex items-center gap-3">
          <select
            value={type}
            onChange={(e) => setType(e.target.value)}
            className="rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-xs text-slate-200 focus:border-emerald-500 focus:outline-none"
          >
            <option value="general">General</option>
            <option value="false_positive">False Positive</option>
            <option value="missed_scam">Missed Scam</option>
            <option value="feature_request">Feature Request</option>
            <option value="bug">Bug Report</option>
          </select>
        </div>
        <textarea
          value={msg}
          onChange={(e) => { setMsg(e.target.value); setResult(null) }}
          placeholder="Describe your feedback — e.g. a scam that was missed, a false positive, or how we can improve detection…"
          rows={3}
          className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-xs text-slate-200 placeholder-slate-500 focus:border-emerald-500 focus:outline-none resize-none"
        />
        <div className="flex items-center gap-3">
          <button
            disabled={send.pending || !msg.trim()}
            onClick={() => { void send.run() }}
            className="flex items-center gap-1.5 rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-500 disabled:opacity-40"
          >
            <Send size={12} />
            {send.pending ? 'Sending…' : 'Send Feedback'}
          </button>
          {result && (
            <span className={`text-xs ${result.kind === 'sent' ? 'text-emerald-400' : result.kind === 'saved' ? 'text-amber-400' : 'text-red-400'}`}>{result.text}</span>
          )}
        </div>
      </div>
    </div>
  )
}
