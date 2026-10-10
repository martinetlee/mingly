import { useState, useRef, useEffect, useCallback, KeyboardEvent, DragEvent, ClipboardEvent } from 'react'
import { Send, X, CornerUpLeft, Pencil, Paperclip, FileIcon, Image, Loader2, AlertCircle } from 'lucide-react'
import type { Message } from '../api/types'
import { errorMessage, toast } from '../lib/toast'
import type { AttachedFile, ComposeDraft } from '../features/chat/chatUiStore'

export type { AttachedFile, ComposeDraft }

/** Per-file result of a multi-file send: the files that didn't go out (undefined = all sent) */
export type FilesSendOutcome = { failed: { file: File; error: string }[] } | void

interface Props {
  /** Rejecting keeps the text and shows the error inline */
  onSend: (text: string) => Promise<void>
  /** Resolve with `{failed}` when only some files went out: those stay attached for a retry */
  onSendFiles?: (files: File[], text: string) => Promise<FilesSendOutcome>
  disabled?: boolean
  /** A send for this conversation is still in flight (e.g. started before a chat switch) */
  busy?: boolean
  /** Focus the text box when it mounts (default true; false keeps keyboard focus where it is) */
  autoFocus?: boolean
  replyTo?: Message | null
  onCancelReply?: () => void
  editingMessage?: Message | null
  onSaveEdit?: (text: string) => Promise<void>
  onCancelEdit?: () => void
  onTyping?: () => void
  uploadProgress?: number | null // 0-1 fraction, null when not uploading
  /** When set, this text is appended to the compose field and the value is cleared via onInsertConsumed */
  insertText?: string | null
  onInsertConsumed?: () => void
  /** Restore a previously saved draft (used with a per-chat `key` to keep drafts per chat) */
  initialDraft?: ComposeDraft | null
  /** Reports every draft change so the parent can persist it across chat switches (never the edit buffer) */
  onDraftChange?: (draft: ComposeDraft) => void
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
  busy,
  autoFocus = true,
  replyTo,
  onCancelReply,
  editingMessage,
  onSaveEdit,
  onCancelEdit,
  onTyping,
  uploadProgress,
  insertText,
  onInsertConsumed,
  initialDraft,
  onDraftChange,
}: Props) {
  // The draft and the edit buffer are separate: editing a message never
  // overwrites (or persists over) what you were typing, and cancelling the
  // edit brings the draft back.
  const [text, setText] = useState(initialDraft?.text ?? '')
  const [editText, setEditText] = useState(editingMessage?.text ?? '')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [fileErrors, setFileErrors] = useState<{ name: string; error: string }[] | null>(null)
  const [fileTotal, setFileTotal] = useState(0)
  const [attachments, setAttachments] = useState<AttachedFile[]>(initialDraft?.attachments ?? [])
  const [dragOver, setDragOver] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const lastTypingSentRef = useRef(0)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const isUploading = uploadProgress != null
  const isEditing = !!editingMessage && !!onSaveEdit
  const value = isEditing ? editText : text

  // Focus on mount (unless told not to), after a send/upload finishes, and
  // when a reply starts. Busy→idle is tracked by value, not by "first run",
  // so StrictMode's second effect run can't take focus (autoFocus=false keeps
  // keyboard focus in the chat list).
  useEffect(() => {
    if (autoFocus) textareaRef.current?.focus()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const busyRef = useRef(false)
  useEffect(() => {
    const wasBusy = busyRef.current
    busyRef.current = sending || isUploading
    if (wasBusy && !busyRef.current) textareaRef.current?.focus()
  }, [sending, isUploading])

  useEffect(() => {
    if (replyTo) textareaRef.current?.focus()
  }, [replyTo])

  // Entering edit mode (or switching the edited message) loads its text into the edit buffer
  const editingId = editingMessage?.id ?? null
  useEffect(() => {
    if (editingId == null) return
    setEditText(editingMessage?.text ?? '')
    setError(null)
    textareaRef.current?.focus()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editingId])

  // Insert text from external trigger (e.g. mention from avatar menu)
  useEffect(() => {
    if (!insertText) return
    const add = (prev: string) => (prev ? prev + ' ' : '') + insertText
    if (isEditing) setEditText(add)
    else setText(add)
    textareaRef.current?.focus()
    onInsertConsumed?.()
  }, [insertText, onInsertConsumed, isEditing])

  // Report draft changes so the parent can persist them across chat switches
  const onDraftChangeRef = useRef(onDraftChange)
  onDraftChangeRef.current = onDraftChange
  const firstReportRef = useRef(true)
  useEffect(() => {
    if (firstReportRef.current) { firstReportRef.current = false; return }
    onDraftChangeRef.current?.({ text, attachments })
  }, [text, attachments])

  // Revoke preview URLs on unmount — unless a parent persists drafts (the
  // previews must survive remount); via a ref so the CURRENT ones are revoked.
  const attachmentsRef = useRef(attachments)
  attachmentsRef.current = attachments
  useEffect(() => {
    return () => {
      if (onDraftChangeRef.current) return
      for (const att of attachmentsRef.current) {
        if (att.preview) URL.revokeObjectURL(att.preview)
      }
    }
  }, [])

  const addFiles = useCallback((fileList: FileList | File[]) => {
    const newFiles: AttachedFile[] = []
    const tooBig: string[] = []
    for (const file of Array.from(fileList)) {
      if (file.size > MAX_FILE_SIZE) { tooBig.push(file.name); continue }
      newFiles.push({ file, preview: isImageFile(file) ? URL.createObjectURL(file) : undefined })
    }
    if (tooBig.length) toast.error(`Too large to send (max 50 MB): ${tooBig.join(', ')}`)
    if (newFiles.length) setAttachments((prev) => [...prev, ...newFiles])
  }, [])

  const removeAttachment = useCallback((index: number) => {
    setFileErrors(null)
    setAttachments((prev) => {
      const removed = prev[index]
      if (removed?.preview) URL.revokeObjectURL(removed.preview)
      return prev.filter((_, i) => i !== index)
    })
  }, [])

  const resetHeight = () => {
    if (textareaRef.current) textareaRef.current.style.height = 'auto'
  }

  const handleSend = async () => {
    const trimmed = value.trim()
    const sentAttachments = attachments
    const hasAttachments = !isEditing && sentAttachments.length > 0
    if ((!trimmed && !hasAttachments) || sending || busy || isUploading || disabled) return

    setSending(true)
    setError(null)
    setFileErrors(null)
    try {
      if (isEditing) {
        await onSaveEdit!(trimmed)
        setEditText('')
      } else if (hasAttachments && onSendFiles) {
        const outcome = await onSendFiles(sentAttachments.map((a) => a.file), trimmed)
        const failed = outcome ? outcome.failed : []
        const failedFiles = new Set(failed.map((f) => f.file))
        // Sent files leave the tray; failed ones stay attached so "Retry" resends only them
        setAttachments((prev) => prev.filter((a) => {
          const sent = sentAttachments.includes(a) && !failedFiles.has(a.file)
          if (sent && a.preview) URL.revokeObjectURL(a.preview)
          return !sent
        }))
        // The caption went out with the first sent file (kept if nothing was sent)
        if (failed.length < sentAttachments.length) setText((cur) => (cur.trim() === trimmed ? '' : cur))
        if (failed.length) {
          setFileTotal(sentAttachments.length)
          setFileErrors(failed.map((f) => ({ name: f.file.name, error: f.error })))
        }
      } else {
        await onSend(trimmed)
        // Keep anything typed while the send was in flight
        setText((cur) => (cur.trim() === trimmed ? '' : cur))
      }
      resetHeight()
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setSending(false)
    }
  }

  const handleCancelEdit = () => {
    setEditText('')
    setError(null)
    onCancelEdit?.()
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.nativeEvent.isComposing) return
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void handleSend()
    }
    if (e.key === 'Escape') {
      if (isEditing) handleCancelEdit()
      else if (replyTo) onCancelReply?.()
    }
  }

  const handleChange = (v: string) => {
    if (error) setError(null)
    if (isEditing) setEditText(v)
    else setText(v)
  }

  const handleInput = () => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 120)}px`
    // Throttle typing indicator to at most once every 3 seconds
    const now = Date.now()
    if (onTyping && !isEditing && now - lastTypingSentRef.current > 3000) {
      lastTypingSentRef.current = now
      onTyping()
    }
  }

  // --- Drag and drop ---
  const handleDragOver = (e: DragEvent) => {
    if (!onSendFiles || isEditing) return
    e.preventDefault()
    e.stopPropagation()
    setDragOver(true)
  }

  const handleDragLeave = (e: DragEvent) => {
    if (!onSendFiles) return
    e.preventDefault()
    e.stopPropagation()
    // Ignore leaving into a child element
    if (e.currentTarget.contains(e.relatedTarget as Node | null)) return
    setDragOver(false)
  }

  const handleDrop = (e: DragEvent) => {
    if (!onSendFiles || isEditing) return
    e.preventDefault()
    e.stopPropagation()
    setDragOver(false)
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) addFiles(e.dataTransfer.files)
  }

  // --- Paste ---
  const handlePaste = (e: ClipboardEvent) => {
    if (!onSendFiles || isEditing) return
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
    ? replyTo.text?.slice(0, 100) ?? (replyTo.attachments?.[0] ? `[${replyTo.attachments[0].kind}]` : '')
    : null

  const inFlight = sending || !!busy || isUploading
  const canSend = (value.trim() || (!isEditing && attachments.length > 0)) && !inFlight && !disabled

  return (
    <div
      className={`relative border-t border-slate-700/50 bg-slate-900 px-4 py-3 ${dragOver ? 'ring-2 ring-inset ring-indigo-500/60' : ''}`}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {isEditing && (
        <div className="mb-2 flex items-start gap-2 rounded-lg border-l-2 border-amber-500 bg-slate-800/80 px-3 py-1.5">
          <Pencil size={13} className="mt-0.5 flex-shrink-0 text-amber-400" />
          <div className="min-w-0 flex-1">
            <p className="text-xs font-medium text-amber-400">Editing message</p>
            <p className="truncate text-xs text-slate-400">{editingMessage!.text?.slice(0, 100) ?? ''}</p>
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
      {replyTo && !isEditing && (
        <div className="mb-2 flex items-start gap-2 rounded-lg border-l-2 border-indigo-500 bg-slate-800/80 px-3 py-1.5">
          <CornerUpLeft size={13} className="mt-0.5 flex-shrink-0 text-indigo-400" />
          <div className="min-w-0 flex-1">
            <p className="text-xs font-medium text-indigo-400">{replyTo.sender?.name ?? 'You'}</p>
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

      {!isEditing && attachments.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-2">
          {attachments.map((att, i) => (
            <div key={i} className="group relative flex items-center gap-1.5 rounded-lg border border-slate-600 bg-slate-800 px-2 py-1.5">
              {att.preview ? (
                <img src={att.preview} alt={att.file.name} className="h-10 w-10 rounded object-cover" />
              ) : (
                <FileIcon size={16} className="flex-shrink-0 text-slate-400" />
              )}
              <div className="min-w-0 max-w-[120px]">
                <p className="truncate text-xs text-slate-300">{att.file.name}</p>
                <p className="text-[10px] text-slate-500">{formatFileSize(att.file.size)}</p>
              </div>
              <button
                onClick={() => removeAttachment(i)}
                disabled={inFlight}
                className="ml-1 flex-shrink-0 rounded p-0.5 text-slate-500 opacity-0 transition-opacity hover:bg-slate-700 hover:text-slate-300 group-hover:opacity-100 focus:opacity-100"
                title="Remove"
              >
                <X size={12} />
              </button>
            </div>
          ))}
        </div>
      )}

      {isUploading && (
        <div className="mb-2">
          <div className="flex items-center gap-2 text-xs text-slate-400">
            <Loader2 size={12} className="animate-spin" />
            <span>Uploading… {Math.round((uploadProgress ?? 0) * 100)}%</span>
          </div>
          <div className="mt-1 h-1 overflow-hidden rounded-full bg-slate-700">
            <div className="h-full rounded-full bg-indigo-500 transition-all duration-200" style={{ width: `${(uploadProgress ?? 0) * 100}%` }} />
          </div>
        </div>
      )}

      {fileErrors && (
        <div role="alert" className="mb-2 rounded-lg bg-red-500/10 px-3 py-1.5 text-xs text-red-400">
          <div className="flex items-center gap-1.5">
            <AlertCircle size={13} className="flex-shrink-0" />
            <span className="flex-1">
              {fileErrors.length === fileTotal ? 'No files were sent' : `${fileErrors.length} of ${fileTotal} files weren’t sent`}
            </span>
            <button onClick={() => void handleSend()} disabled={inFlight} className="underline hover:text-red-300">
              Retry {fileErrors.length === 1 ? 'it' : `${fileErrors.length} files`}
            </button>
            <button onClick={() => setFileErrors(null)} aria-label="Dismiss" className="hover:text-red-300"><X size={12} /></button>
          </div>
          <ul className="mt-1 ml-5 list-disc">
            {fileErrors.map((f, i) => <li key={i}><span className="text-red-300">{f.name}</span>: {f.error}</li>)}
          </ul>
        </div>
      )}

      {error && (
        <div role="alert" className="mb-2 flex items-center gap-1.5 rounded-lg bg-red-500/10 px-3 py-1.5 text-xs text-red-400">
          <AlertCircle size={13} className="flex-shrink-0" />
          <span className="flex-1">{isEditing ? 'Edit failed' : 'Not sent'}: {error}</span>
          <button onClick={() => void handleSend()} disabled={inFlight} className="underline hover:text-red-300">Retry</button>
        </div>
      )}

      <div className="flex items-end gap-2 rounded-xl border border-slate-600 bg-slate-800 px-3 py-2">
        {onSendFiles && !isEditing && (
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={isUploading || disabled}
            className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg text-slate-400 transition-colors hover:bg-slate-700 hover:text-slate-200 disabled:opacity-40"
            title="Attach files"
          >
            {attachments.length > 0 ? <Image size={16} /> : <Paperclip size={16} />}
          </button>
        )}

        <textarea
          ref={textareaRef}
          rows={1}
          value={value}
          onChange={(e) => handleChange(e.target.value)}
          onInput={handleInput}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          placeholder={
            isEditing
              ? 'Edit message…'
              : attachments.length > 0
                ? 'Add a caption… (Enter to send)'
                : replyTo
                  ? 'Write a reply…'
                  : 'Message… (Enter to send)'
          }
          disabled={disabled || isUploading}
          className="flex-1 resize-none bg-transparent text-sm text-gray-100 outline-none placeholder:text-slate-500 disabled:opacity-50"
          style={{ maxHeight: 120 }}
        />

        <button
          onClick={() => void handleSend()}
          disabled={!canSend}
          className={`flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg text-white transition-colors disabled:opacity-40 ${
            isEditing ? 'bg-amber-500 hover:bg-amber-400' : 'bg-indigo-500 hover:bg-indigo-400'
          }`}
          title={isEditing ? 'Save edit' : 'Send'}
        >
          {inFlight && !isUploading ? <Loader2 size={15} className="animate-spin" /> : isEditing ? <Pencil size={15} /> : <Send size={15} />}
        </button>
      </div>

      <input
        ref={fileInputRef}
        type="file"
        multiple
        onChange={(e) => {
          if (e.target.files && e.target.files.length > 0) addFiles(e.target.files)
          e.target.value = '' // so the same file can be picked again
        }}
        className="hidden"
      />

      {dragOver && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-lg bg-indigo-500/10">
          <p className="text-sm font-medium text-indigo-300">Drop files to attach</p>
        </div>
      )}
    </div>
  )
}
