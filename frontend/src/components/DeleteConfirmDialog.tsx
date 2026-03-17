import { useState } from 'react'
import { AlertTriangle } from 'lucide-react'
import type { Message } from '../api/types'

interface Props {
  message: Message | null
  onClose: () => void
  onConfirm: (revoke: boolean) => void
}

export default function DeleteConfirmDialog({ message, onClose, onConfirm }: Props) {
  const [revoke, setRevoke] = useState(false)

  if (!message) return null

  const preview = message.text?.slice(0, 80) ?? (message.media_type ? `[${message.media_type}]` : 'this message')

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="w-full max-w-sm rounded-xl border border-slate-600 bg-slate-800 shadow-2xl">
        <div className="px-5 py-4">
          <div className="flex items-start gap-3">
            <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-red-500/20">
              <AlertTriangle size={16} className="text-red-400" />
            </div>
            <div>
              <h3 className="text-sm font-semibold text-gray-100">Delete message?</h3>
              <p className="mt-1 text-xs text-slate-400 line-clamp-2">
                {preview}
              </p>
            </div>
          </div>

          {/* Revoke toggle */}
          <label className="mt-4 flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={revoke}
              onChange={(e) => setRevoke(e.target.checked)}
              className="h-3.5 w-3.5 rounded border-slate-500 bg-slate-700 text-indigo-500 focus:ring-indigo-500/30"
            />
            <span className="text-xs text-slate-300">Delete for everyone</span>
          </label>
        </div>

        <div className="flex justify-end gap-2 border-t border-slate-700 px-5 py-3">
          <button
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-xs text-slate-400 hover:bg-slate-700 transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={() => onConfirm(revoke)}
            className="rounded-lg bg-red-500/20 px-3 py-1.5 text-xs font-medium text-red-400 hover:bg-red-500/30 transition-colors"
          >
            Delete
          </button>
        </div>
      </div>
    </div>
  )
}
