import { useState, useRef, useEffect, KeyboardEvent } from 'react'
import { Send, X, CornerUpLeft, Pencil } from 'lucide-react'
import type { Message } from '../api/types'

interface Props {
  onSend: (text: string) => Promise<void>
  disabled?: boolean
  replyTo?: Message | null
  onCancelReply?: () => void
  editingMessage?: Message | null
  onSaveEdit?: (text: string) => Promise<void>
  onCancelEdit?: () => void
}

export default function ComposeBar({ onSend, disabled, replyTo, onCancelReply, editingMessage, onSaveEdit, onCancelEdit }: Props) {
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

  // Populate text when entering edit mode
  useEffect(() => {
    if (editingMessage) {
      setText(editingMessage.text ?? '')
      textareaRef.current?.focus()
    }
  }, [editingMessage])

  const handleSend = async () => {
    const trimmed = text.trim()
    if (!trimmed || sending) return
    setSending(true)
    try {
      if (editingMessage && onSaveEdit) {
        await onSaveEdit(trimmed)
      } else {
        await onSend(trimmed)
      }
      setText('')
      if (textareaRef.current) {
        textareaRef.current.style.height = 'auto'
      }
    } finally {
      setSending(false)
    }
  }

  const handleCancelEdit = () => {
    setText('')
    onCancelEdit?.()
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
    if (e.key === 'Escape') {
      if (editingMessage) {
        handleCancelEdit()
      } else if (replyTo) {
        onCancelReply?.()
      }
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
      {/* Edit preview bar */}
      {editingMessage && (
        <div className="mb-2 flex items-start gap-2 rounded-lg border-l-2 border-amber-500 bg-slate-800/80 px-3 py-1.5">
          <Pencil size={13} className="mt-0.5 flex-shrink-0 text-amber-400" />
          <div className="min-w-0 flex-1">
            <p className="text-xs font-medium text-amber-400">
              Editing message
            </p>
            <p className="truncate text-xs text-slate-400">
              {editingMessage.text?.slice(0, 100) ?? ''}
            </p>
          </div>
          <button
            onClick={handleCancelEdit}
            className="flex-shrink-0 rounded p-0.5 text-slate-500 hover:bg-slate-700 hover:text-slate-300"
            title="Cancel edit"
          >
            <X size={13} />
          </button>
        </div>
      )}
      {/* Reply preview bar */}
      {replyTo && !editingMessage && (
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
          placeholder={editingMessage ? 'Edit message…' : replyTo ? 'Write a reply…' : 'Message… (Enter to send)'}
          disabled={disabled}
          className="flex-1 resize-none bg-transparent text-sm text-gray-100 outline-none placeholder:text-slate-500 disabled:opacity-50"
          style={{ maxHeight: 120 }}
        />
        <button
          onClick={handleSend}
          disabled={!text.trim() || sending || disabled}
          className={`flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg text-white transition-colors disabled:opacity-40 ${
            editingMessage ? 'bg-amber-500 hover:bg-amber-400' : 'bg-indigo-500 hover:bg-indigo-400'
          }`}
          title={editingMessage ? 'Save edit' : 'Send'}
        >
          {editingMessage ? <Pencil size={15} /> : <Send size={15} />}
        </button>
      </div>
    </div>
  )
}
