import { useEffect, useState } from 'react'
import { FileText, Download, Loader2, RotateCcw } from 'lucide-react'
import { authenticatedMediaUrl } from '../../api/session'
import { useMediaDownload } from '../../hooks/useMediaDownload'

interface Props {
  attachmentId: number
  filename: string | null
  fileSize: number | null
  mediaUrl: string | null
  isOut: boolean
}

function formatFileSize(bytes: number | null): string {
  if (bytes == null) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`
}

function fileExtension(filename: string | null): string {
  if (!filename) return ''
  const parts = filename.split('.')
  return parts.length > 1 ? parts[parts.length - 1].toUpperCase() : ''
}

export default function DocumentCard({ attachmentId, filename, fileSize, mediaUrl, isOut }: Props) {
  // Click-to-download; opens once the file is available
  const media = useMediaDownload(attachmentId, { initialUrl: mediaUrl })
  const [wantOpen, setWantOpen] = useState(false)
  const ext = fileExtension(filename)
  const sizeStr = formatFileSize(fileSize)
  const loading = media.status === 'loading' || media.status === 'queued'
  const failed = media.status === 'error'

  useEffect(() => {
    if (wantOpen && media.url) {
      setWantOpen(false)
      window.open(authenticatedMediaUrl(media.url), '_blank')
    }
  }, [wantOpen, media.url])

  const handleClick = () => {
    if (media.url) {
      window.open(authenticatedMediaUrl(media.url), '_blank')
      return
    }
    setWantOpen(true)
    media.start()
  }

  const sub = loading ? 'Downloading…' : failed ? `${media.error ?? 'Download failed'} — click to retry` : sizeStr
  const iconCls = `flex-shrink-0 ${isOut ? 'text-indigo-300' : 'text-slate-400'}`

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={loading}
      className={`flex w-[calc(100%-0.5rem)] items-center gap-3 rounded-lg px-3 py-2.5 mx-1 my-1 text-left transition-colors ${
        isOut ? 'bg-indigo-700/40 hover:bg-indigo-700/60' : 'bg-slate-600/40 hover:bg-slate-600/60'
      }`}
    >
      <div
        className={`flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-lg ${
          isOut ? 'bg-indigo-500/30 text-indigo-200' : 'bg-slate-500/30 text-slate-200'
        }`}
      >
        {ext ? <span className="text-[10px] font-bold leading-none">{ext}</span> : <FileText size={18} />}
      </div>
      <div className="min-w-0 flex-1">
        <p className={`truncate text-sm font-medium ${isOut ? 'text-white' : 'text-gray-100'}`}>{filename || 'Document'}</p>
        {sub && <p className={`text-xs ${failed ? 'text-orange-300' : isOut ? 'text-indigo-300' : 'text-slate-400'}`}>{sub}</p>}
      </div>
      {loading ? <Loader2 size={16} className={`${iconCls} animate-spin`} />
        : failed ? <RotateCcw size={16} className={iconCls} />
        : <Download size={16} className={iconCls} />}
    </button>
  )
}
