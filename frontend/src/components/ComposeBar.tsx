import { useState, useRef, KeyboardEvent } from 'react'
import { Send } from 'lucide-react'

interface Props {
  onSend: (text: string) => Promise<void>
  disabled?: boolean
}

export default function ComposeBar({ onSend, disabled }: Props) {
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

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
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
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
    <div className="border-t border-gray-200 bg-white px-4 py-3">
      <div className="flex items-end gap-2 rounded-xl border border-gray-200 bg-gray-50 px-3 py-2">
        <textarea
          ref={textareaRef}
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onInput={handleInput}
          onKeyDown={handleKeyDown}
          placeholder="Message… (⌘↵ to send)"
          disabled={disabled || sending}
          className="flex-1 resize-none bg-transparent text-sm text-gray-900 outline-none placeholder:text-gray-400 disabled:opacity-50"
          style={{ maxHeight: 120 }}
        />
        <button
          onClick={handleSend}
          disabled={!text.trim() || sending || disabled}
          className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg bg-blue-500 text-white transition-colors hover:bg-blue-600 disabled:opacity-40"
        >
          <Send size={15} />
        </button>
      </div>
    </div>
  )
}
