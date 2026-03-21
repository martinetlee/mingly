import { useState, useRef, useEffect, useCallback, KeyboardEvent, DragEvent, ClipboardEvent } from 'react'
import { Send, X, CornerUpLeft, Pencil, Paperclip, FileIcon, Image, Loader2 } from 'lucide-react'
import type { Message } from '../api/types'

interface AttachedFile {
  file: File
  preview?: string // data URL for images
}

interface Props {
  onSend: (text: string) => Promise<void>
  onSendFiles?: (files: File[], text: string) => Promise<void>
  disabled?: boolean
  replyTo?: Message | null
  onCancelReply?: () => void
  editingMessage?: Message | null
  onSaveEdit?: (text: string) => Promise<void>
  onCancelEdit?: () => void
  onTyping?: () => void
  uploadProgress?: number | null // 0-1 fraction, null when not uploading
}

const MAX_FILE_SIZE = 50 * 1024 * 1024 // 50 MB per file
// NOTE: No accept filter — pywebview on macOS has a bug where it hardcodes
// the native file dialog to only allow image/jpg when WebKit passes MIME types.
// Removing the filter lets users pick any file type.

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function isImageFile(file: File): boolean {
  return file.type.startsWith('image/')
}

export default function ComposeBar({
  onSend,
  onSendFiles,
  disabled,
  replyTo,
  onCancelReply,
  editingMessage,
  onSaveEdit,
  onCancelEdit,
  onTyping,
  uploadProgress,
}: Props) {
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [attachments, setAttachments] = useState<AttachedFile[]>([])
  const [dragOver, setDragOver] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const lastTypingSentRef = useRef(0)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const isUploading = uploadProgress != null

  useEffect(() => {
    if (!sending && !isUploading) {
      textareaRef.current?.focus()
    }
  }, [sending, isUploading])

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

  // Clean up preview URLs on unmount
  useEffect(() => {
    return () => {
      for (const att of attachments) {
        if (att.preview) URL.revokeObjectURL(att.preview)
      }
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const addFiles = useCallback((fileList: FileList | File[]) => {
    const newFiles: AttachedFile[] = []
    for (const file of Array.from(fileList)) {
      if (file.size > MAX_FILE_SIZE) {
        continue // silently skip oversized files
      }
      const preview = isImageFile(file) ? URL.createObjectURL(file) : undefined
      newFiles.push({ file, preview })
    }
    setAttachments((prev) => [...prev, ...newFiles])
  }, [])

  const removeAttachment = useCallback((index: number) => {
    setAttachments((prev) => {
      const removed = prev[index]
      if (removed?.preview) URL.revokeObjectURL(removed.preview)
      return prev.filter((_, i) => i !== index)
    })
  }, [])

  const clearAttachments = useCallback(() => {
    setAttachments((prev) => {
      for (const att of prev) {
        if (att.preview) URL.revokeObjectURL(att.preview)
      }
      return []
    })
  }, [])

  const handleSend = async () => {
    const trimmed = text.trim()
    const hasAttachments = attachments.length > 0
    if ((!trimmed && !hasAttachments) || sending || isUploading) return

    setSending(true)
    try {
      if (editingMessage && onSaveEdit) {
        await onSaveEdit(trimmed)
      } else if (hasAttachments && onSendFiles) {
        await onSendFiles(attachments.map((a) => a.file), trimmed)
        clearAttachments()
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
    // Throttle typing indicator to at most once every 3 seconds
    const now = Date.now()
    if (onTyping && now - lastTypingSentRef.current > 3000) {
      lastTypingSentRef.current = now
      onTyping()
    }
  }

  const handleFileSelect = () => {
    fileInputRef.current?.click()
  }

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      addFiles(e.target.files)
    }
    // Reset so same file can be selected again
    e.target.value = ''
  }

  // --- Drag and drop ---
  const handleDragOver = (e: DragEvent) => {
    if (!onSendFiles) return
    e.preventDefault()
    e.stopPropagation()
    setDragOver(true)
  }

  const handleDragLeave = (e: DragEvent) => {
    if (!onSendFiles) return
    e.preventDefault()
    e.stopPropagation()
    setDragOver(false)
  }

  const handleDrop = (e: DragEvent) => {
    if (!onSendFiles) return
    e.preventDefault()
    e.stopPropagation()
    setDragOver(false)
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      addFiles(e.dataTransfer.files)
    }
  }

  // --- Paste ---
  const handlePaste = (e: ClipboardEvent) => {
    if (!onSendFiles) return
    const items = e.clipboardData?.items
    if (!items) return
    const pastedFiles: File[] = []
    for (const item of Array.from(items)) {
      if (item.kind === 'file') {
        const file = item.getAsFile()
        if (file) pastedFiles.push(file)
      }
    }
    if (pastedFiles.length > 0) {
      e.preventDefault()
      addFiles(pastedFiles)
    }
  }

  const replyPreview = replyTo
    ? replyTo.text?.slice(0, 100) ?? (replyTo.media_type ? `[${replyTo.media_type}]` : '')
    : null

  const canSend = (text.trim() || attachments.length > 0) && !sending && !isUploading && !disabled

  return (
    <div
      className={`border-t border-slate-700/50 bg-slate-900 px-4 py-3 ${dragOver ? 'ring-2 ring-inset ring-indigo-500/60' : ''}`}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
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

      {/* Attachment previews */}
      {attachments.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-2">
          {attachments.map((att, i) => (
            <div
              key={i}
              className="group relative flex items-center gap-1.5 rounded-lg border border-slate-600 bg-slate-800 px-2 py-1.5"
            >
              {att.preview ? (
                <img
                  src={att.preview}
                  alt={att.file.name}
                  className="h-10 w-10 rounded object-cover"
                />
              ) : (
                <FileIcon size={16} className="flex-shrink-0 text-slate-400" />
              )}
              <div className="min-w-0 max-w-[120px]">
                <p className="truncate text-xs text-slate-300">{att.file.name}</p>
                <p className="text-[10px] text-slate-500">{formatFileSize(att.file.size)}</p>
              </div>
              <button
                onClick={() => removeAttachment(i)}
                className="ml-1 flex-shrink-0 rounded p-0.5 text-slate-500 opacity-0 transition-opacity hover:bg-slate-700 hover:text-slate-300 group-hover:opacity-100"
                title="Remove"
              >
                <X size={12} />
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Upload progress bar */}
      {isUploading && (
        <div className="mb-2">
          <div className="flex items-center gap-2 text-xs text-slate-400">
            <Loader2 size={12} className="animate-spin" />
            <span>Uploading... {Math.round((uploadProgress ?? 0) * 100)}%</span>
          </div>
          <div className="mt-1 h-1 overflow-hidden rounded-full bg-slate-700">
            <div
              className="h-full rounded-full bg-indigo-500 transition-all duration-200"
              style={{ width: `${(uploadProgress ?? 0) * 100}%` }}
            />
          </div>
        </div>
      )}

      <div className="flex items-end gap-2 rounded-xl border border-slate-600 bg-slate-800 px-3 py-2">
        {/* Attachment button */}
        {onSendFiles && (
          <button
            onClick={handleFileSelect}
            disabled={isUploading || disabled}
            className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg text-slate-400 transition-colors hover:bg-slate-700 hover:text-slate-200 disabled:opacity-40"
            title="Attach files"
          >
            {attachments.length > 0 ? (
              <Image size={16} />
            ) : (
              <Paperclip size={16} />
            )}
          </button>
        )}

        <textarea
          ref={textareaRef}
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onInput={handleInput}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          placeholder={
            editingMessage
              ? 'Edit message...'
              : attachments.length > 0
                ? 'Add a caption... (Enter to send)'
                : replyTo
                  ? 'Write a reply...'
                  : 'Message... (Enter to send)'
          }
          disabled={disabled || isUploading}
          className="flex-1 resize-none bg-transparent text-sm text-gray-100 outline-none placeholder:text-slate-500 disabled:opacity-50"
          style={{ maxHeight: 120 }}
        />

        <button
          onClick={handleSend}
          disabled={!canSend}
          className={`flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg text-white transition-colors disabled:opacity-40 ${
            editingMessage ? 'bg-amber-500 hover:bg-amber-400' : 'bg-indigo-500 hover:bg-indigo-400'
          }`}
          title={editingMessage ? 'Save edit' : 'Send'}
        >
          {editingMessage ? <Pencil size={15} /> : <Send size={15} />}
        </button>
      </div>

      {/* Hidden file input */}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        onChange={handleFileChange}
        className="hidden"
      />

      {/* Drag overlay hint */}
      {dragOver && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-lg bg-indigo-500/10">
          <p className="text-sm font-medium text-indigo-300">Drop files to attach</p>
        </div>
      )}
    </div>
  )
}
