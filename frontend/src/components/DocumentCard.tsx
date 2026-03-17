import { FileText, Download } from 'lucide-react'
import { authenticatedMediaUrl } from '../api/session'

interface Props {
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

export default function DocumentCard({ filename, fileSize, mediaUrl, isOut }: Props) {
  const ext = fileExtension(filename)
  const displayName = filename || 'Document'
  const sizeStr = formatFileSize(fileSize)

  const handleDownload = () => {
    if (!mediaUrl) return
    const url = authenticatedMediaUrl(mediaUrl)
    window.open(url, '_blank')
  }

  return (
    <div
      className={`flex items-center gap-3 rounded-lg px-3 py-2.5 mx-1 my-1 cursor-pointer transition-colors ${
        isOut
          ? 'bg-indigo-700/40 hover:bg-indigo-700/60'
          : 'bg-slate-600/40 hover:bg-slate-600/60'
      }`}
      onClick={handleDownload}
    >
      <div
        className={`flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-lg ${
          isOut ? 'bg-indigo-500/30 text-indigo-200' : 'bg-slate-500/30 text-slate-200'
        }`}
      >
        {ext ? (
          <span className="text-[10px] font-bold leading-none">{ext}</span>
        ) : (
          <FileText size={18} />
        )}
      </div>
      <div className="min-w-0 flex-1">
        <p
          className={`truncate text-sm font-medium ${
            isOut ? 'text-white' : 'text-gray-100'
          }`}
        >
          {displayName}
        </p>
        {sizeStr && (
          <p className={`text-xs ${isOut ? 'text-indigo-300' : 'text-slate-400'}`}>
            {sizeStr}
          </p>
        )}
      </div>
      {mediaUrl && (
        <Download
          size={16}
          className={`flex-shrink-0 ${
            isOut ? 'text-indigo-300 hover:text-white' : 'text-slate-400 hover:text-white'
          } transition-colors`}
        />
      )}
    </div>
  )
}
