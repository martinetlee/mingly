import { useState, useEffect } from 'react'
import { ShieldAlert, X, Check, Loader2 } from 'lucide-react'
import type { ScamCategory, ReportContextMessage } from '../api/types'
import { getReportContext, getScamPipeline, createScamReport } from '../api/client'

interface Props {
  chatId: number
  targetMessageId: number
  targetSenderId: number
  onClose: () => void
}

export default function ReportScamDialog({ chatId, targetMessageId, targetSenderId, onClose }: Props) {
  const [messages, setMessages] = useState<ReportContextMessage[]>([])
  const [categories, setCategories] = useState<ScamCategory[]>([])
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [submitted, setSubmitted] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // User selections
  const [checkedIds, setCheckedIds] = useState<Set<number>>(new Set())
  const [category, setCategory] = useState('')
  const [customCategory, setCustomCategory] = useState('')

  useEffect(() => {
    Promise.all([
      getReportContext(chatId, targetMessageId),
      getScamPipeline(),
    ])
      .then(([ctx, pipeline]) => {
        setMessages(ctx.messages)
        setCategories(pipeline.categories)
        // Check all messages by default
        setCheckedIds(new Set(ctx.messages.map((m) => m.tg_message_id)))
      })
      .catch((e) => setError(e instanceof Error ? e.message : 'Failed to load context'))
      .finally(() => setLoading(false))
  }, [chatId, targetMessageId])

  const toggleMessage = (id: number) => {
    setCheckedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const handleSubmit = async () => {
    if (checkedIds.size === 0) return
    setSubmitting(true)
    setError(null)
    try {
      await createScamReport({
        reported_telegram_id: targetSenderId,
        reported_in_chat_id: chatId,
        reporter_verdict: 'scam',
        scam_category: category || undefined,
        custom_category: category === 'other' ? customCategory || undefined : undefined,
        included_message_ids: Array.from(checkedIds),
      })
      setSubmitted(true)
      setTimeout(onClose, 1500)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to submit report')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />
      <div className="relative w-full max-w-lg rounded-xl border border-slate-700/80 bg-slate-800 shadow-2xl overflow-hidden max-h-[80vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-slate-700/50 px-4 py-3">
          <div className="flex items-center gap-2">
            <ShieldAlert size={16} className="text-red-400" />
            <h2 className="text-sm font-semibold text-gray-100">Report Scam</h2>
          </div>
          <button onClick={onClose} className="rounded p-1 text-slate-400 hover:bg-slate-700 hover:text-slate-200">
            <X size={16} />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {loading ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 size={20} className="animate-spin text-slate-500" />
            </div>
          ) : submitted ? (
            <div className="flex flex-col items-center justify-center py-8">
              <Check size={24} className="mb-2 text-emerald-400" />
              <p className="text-sm text-emerald-300">Report submitted</p>
            </div>
          ) : (
            <>
              <p className="text-xs text-slate-400">
                Select which messages to include in your report. Unchecked messages will appear as "(omitted)" to preserve conversation flow.
              </p>

              {/* Message list with checkboxes */}
              <div className="space-y-1 rounded-lg border border-slate-700 bg-slate-900 p-2 max-h-64 overflow-y-auto">
                {messages.map((msg) => {
                  const isReported = msg.sender_id === targetSenderId
                  const isChecked = checkedIds.has(msg.tg_message_id)
                  return (
                    <label
                      key={msg.tg_message_id}
                      className={`flex items-start gap-2 rounded-lg px-2 py-1.5 cursor-pointer transition-colors ${
                        isChecked ? 'bg-slate-800' : 'opacity-50'
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={isChecked}
                        onChange={() => toggleMessage(msg.tg_message_id)}
                        className="mt-0.5 rounded border-slate-600 bg-slate-700 text-red-500 focus:ring-0 h-3.5 w-3.5"
                      />
                      <div className="flex-1 min-w-0">
                        <span className={`text-[10px] font-medium ${
                          isReported ? 'text-red-400' : msg.is_outgoing ? 'text-indigo-400' : 'text-purple-400'
                        }`}>
                          {isReported ? (msg.sender_name || `ID: ${msg.sender_id}`) : msg.is_outgoing ? 'You' : (msg.sender_name || 'Unknown')}
                        </span>
                        <p className={`text-xs text-slate-300 ${isChecked ? '' : 'line-through text-slate-500'}`}>
                          {msg.text ? (msg.text.length > 150 ? msg.text.slice(0, 150) + '...' : msg.text) : '(media)'}
                        </p>
                      </div>
                    </label>
                  )
                })}
              </div>

              {/* Category selector */}
              <div>
                <label className="block text-xs font-medium text-slate-400 mb-1">Scam category</label>
                <select
                  value={category}
                  onChange={(e) => setCategory(e.target.value)}
                  className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-xs text-slate-200 focus:border-red-500 focus:outline-none"
                >
                  <option value="">Select category...</option>
                  {categories.map((c) => (
                    <option key={c.id} value={c.id}>{c.label}</option>
                  ))}
                </select>
              </div>

              {category === 'other' && (
                <div>
                  <label className="block text-xs font-medium text-slate-400 mb-1">Describe the scam type</label>
                  <input
                    type="text"
                    value={customCategory}
                    onChange={(e) => setCustomCategory(e.target.value)}
                    placeholder="e.g., NFT rug pull"
                    className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-xs text-slate-200 placeholder-slate-500 focus:border-red-500 focus:outline-none"
                  />
                </div>
              )}

              {error && (
                <p className="text-xs text-red-400">{error}</p>
              )}
            </>
          )}
        </div>

        {/* Footer */}
        {!loading && !submitted && (
          <div className="flex items-center justify-end gap-2 border-t border-slate-700/50 px-4 py-3">
            <button
              onClick={onClose}
              className="rounded-lg px-3 py-1.5 text-xs text-slate-400 hover:bg-slate-700 hover:text-slate-200 transition-colors"
            >
              Cancel
            </button>
            <button
              disabled={submitting || checkedIds.size === 0}
              onClick={handleSubmit}
              className="rounded-lg bg-red-600 px-4 py-1.5 text-xs font-medium text-white hover:bg-red-500 disabled:opacity-40 transition-colors"
            >
              {submitting ? 'Submitting...' : `Submit Report (${checkedIds.size} messages)`}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
