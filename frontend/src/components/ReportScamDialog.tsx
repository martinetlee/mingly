import { useState } from 'react'
import { ShieldAlert, Check, Loader2 } from 'lucide-react'
import { getReportContext, getScamPipeline, createScamReport } from '../api/client'
import { Modal, modalButton } from './ui'
import { useLatestQuery } from '../hooks/useLatestQuery'
import { useAsyncAction } from '../hooks/useAsyncAction'
import { errorMessage, toast } from '../lib/toast'

interface Props {
  /** Captured when the dialog opened — never the "currently selected" conversation */
  conversationId: number
  targetMessageId: number
  /** Identity of the reported sender */
  targetIdentityId: number
  onClose: () => void
}

export default function ReportScamDialog({ conversationId, targetMessageId, targetIdentityId, onClose }: Props) {
  const ctx = useLatestQuery(['report-ctx', conversationId, targetMessageId], async () => {
    const [c, pipeline] = await Promise.all([getReportContext(conversationId, targetMessageId), getScamPipeline()])
    return { messages: c.messages, categories: pipeline.categories }
  })
  // Messages the user unticked (all are included by default)
  const [unchecked, setUnchecked] = useState<Set<number>>(new Set())
  const [category, setCategory] = useState('')
  const [customCategory, setCustomCategory] = useState('')
  const [submitted, setSubmitted] = useState(false)

  const messages = ctx.data?.messages ?? []
  const checkedIds = messages.map((m) => m.message_id).filter((id) => !unchecked.has(id))

  const submit = useAsyncAction(async () => {
    await createScamReport({
      reported_identity_id: targetIdentityId,
      reported_in_conversation_id: conversationId,
      reporter_verdict: 'scam',
      scam_category: category || undefined,
      custom_category: category === 'other' ? customCategory || undefined : undefined,
      included_message_ids: checkedIds,
    })
    setSubmitted(true)
    toast.success('Report submitted')
    setTimeout(onClose, 1200)
  })

  const toggle = (id: number) => setUnchecked((prev) => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    return next
  })

  return (
    <Modal
      open
      onClose={submit.pending ? () => {} : onClose}
      size="lg"
      title="Report Scam"
      icon={<ShieldAlert size={16} className="text-red-400" />}
      footer={!ctx.loading && !submitted && !ctx.error ? (
        <>
          <button onClick={onClose} disabled={submit.pending} className={modalButton.secondary}>Cancel</button>
          <button
            disabled={submit.pending || checkedIds.length === 0}
            onClick={() => void submit.run()}
            className="inline-flex items-center gap-1.5 rounded-lg bg-red-600 px-4 py-1.5 text-xs font-medium text-white hover:bg-red-500 disabled:opacity-40 transition-colors"
          >
            {submit.pending && <Loader2 size={12} className="animate-spin" />}
            Submit Report ({checkedIds.length} messages)
          </button>
        </>
      ) : undefined}
    >
      <div className="p-4 space-y-4">
        {ctx.loading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 size={20} className="animate-spin text-slate-500" />
          </div>
        ) : ctx.error ? (
          <p className="py-6 text-center text-xs text-red-400">{errorMessage(ctx.error)}</p>
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
            <div className="space-y-1 rounded-lg border border-slate-700 bg-slate-900 p-2 max-h-64 overflow-y-auto">
              {messages.map((msg) => {
                const isReported = msg.sender_identity_id === targetIdentityId
                const isChecked = !unchecked.has(msg.message_id)
                return (
                  <label
                    key={msg.message_id}
                    className={`flex items-start gap-2 rounded-lg px-2 py-1.5 cursor-pointer transition-colors ${isChecked ? 'bg-slate-800' : 'opacity-50'}`}
                  >
                    <input
                      type="checkbox"
                      checked={isChecked}
                      onChange={() => toggle(msg.message_id)}
                      className="mt-0.5 rounded border-slate-600 bg-slate-700 text-red-500 focus:ring-0 h-3.5 w-3.5"
                    />
                    <div className="flex-1 min-w-0">
                      <span className={`text-[10px] font-medium ${isReported ? 'text-red-400' : msg.is_outgoing ? 'text-indigo-400' : 'text-purple-400'}`}>
                        {isReported ? (msg.sender_name || 'Reported sender') : msg.is_outgoing ? 'You' : (msg.sender_name || 'Unknown')}
                      </span>
                      <p className={`text-xs ${isChecked ? 'text-slate-300' : 'line-through text-slate-500'}`}>
                        {msg.text ? (msg.text.length > 150 ? msg.text.slice(0, 150) + '…' : msg.text) : '(media)'}
                      </p>
                    </div>
                  </label>
                )
              })}
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-400 mb-1">Scam category</label>
              <select
                value={category}
                onChange={(e) => setCategory(e.target.value)}
                className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-xs text-slate-200 focus:border-red-500 focus:outline-none"
              >
                <option value="">Select category…</option>
                {(ctx.data?.categories ?? []).map((c) => (
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
            {submit.error != null && <p role="alert" className="text-xs text-red-400">{errorMessage(submit.error)}</p>}
          </>
        )}
      </div>
    </Modal>
  )
}
