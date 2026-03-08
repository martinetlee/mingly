import { useState, useRef, useEffect, KeyboardEvent } from 'react'
import { Send } from 'lucide-react'

interface Props {
  onSend: (text: string) => Promise<void>
  disabled?: boolean
}

export default function ComposeBar({ onSend, disabled }: Props) {
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    if (!sending) {
      textareaRef.current?.focus()
    }
  }, [sending])

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
  }

  const handleInput = () => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 120)}px`
  }

  return (
    <div className="border-t border-slate-700/50 bg-slate-900 px-4 py-3">
      <div className="flex items-end gap-2 rounded-xl border border-slate-600 bg-slate-800 px-3 py-2">
        <textarea
          ref={textareaRef}
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onInput={handleInput}
          onKeyDown={handleKeyDown}
          placeholder="Message… (Enter to send)"
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
