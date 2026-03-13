import { useState, useRef, useEffect, KeyboardEvent } from 'react'
import { Send, X, CornerUpLeft } from 'lucide-react'
import type { Message } from '../api/types'

interface Props {
  onSend: (text: string) => Promise<void>
  disabled?: boolean
  replyTo?: Message | null
  onCancelReply?: () => void
}

export default function ComposeBar({ onSend, disabled, replyTo, onCancelReply }: Props) {
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    if (!sending) {
      textareaRef.current?.focus()
    }
  }, [sending])

  // Auto-focus when reply target changes
  useEffect(() => {
    textareaRef.current?.focus()
  }, [replyTo])

  const handleSend = async () => {
    const trimmed = text.trim()
    if (!trimmed || sending) return
    setSending(true)
    try {
      await onSend(trimmed)
      setText('')
      if (textareaRef.current) {
        textareaRef.current.style.height = 'auto'
      }
    } finally {
      setSending(false)
    }
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
    if (e.key === 'Escape' && replyTo) {
      onCancelReply?.()
    }
  }

  const handleInput = () => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 120)}px`
  }

  const replyPreview = replyTo
    ? replyTo.text?.slice(0, 100) ?? (replyTo.media_type ? `[${replyTo.media_type}]` : '')
    : null

  return (
    <div className="border-t border-slate-700/50 bg-slate-900 px-4 py-3">
      {/* Reply preview bar */}
      {replyTo && (
        <div className="mb-2 flex items-start gap-2 rounded-lg border-l-2 border-indigo-500 bg-slate-800/80 px-3 py-1.5">
          <CornerUpLeft size={13} className="mt-0.5 flex-shrink-0 text-indigo-400" />
          <div className="min-w-0 flex-1">
            <p className="text-xs font-medium text-indigo-400">
              {replyTo.sender_name ?? 'You'}
            </p>
            <p className="truncate text-xs text-slate-400">{replyPreview}</p>
          </div>
          <button
            onClick={onCancelReply}
            className="flex-shrink-0 rounded p-0.5 text-slate-500 hover:bg-slate-700 hover:text-slate-300"
            title="Cancel reply"
          >
            <X size={13} />
          </button>
        </div>
      )}

      <div className="flex items-end gap-2 rounded-xl border border-slate-600 bg-slate-800 px-3 py-2">
        <textarea
          ref={textareaRef}
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onInput={handleInput}
          onKeyDown={handleKeyDown}
          placeholder={replyTo ? 'Write a reply…' : 'Message… (Enter to send)'}
          disabled={disabled}
          className="flex-1 resize-none bg-transparent text-sm text-gray-100 outline-none placeholder:text-slate-500 disabled:opacity-50"
          style={{ maxHeight: 120 }}
        />
        <button
          onClick={handleSend}
          disabled={!text.trim() || sending || disabled}
          className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg bg-indigo-500 text-white transition-colors hover:bg-indigo-400 disabled:opacity-40"
        >
          <Send size={15} />
        </button>
      </div>
    </div>
  )
}
