import { useEffect, useState } from 'react'
import { Image, FileText, LinkIcon, Download, ExternalLink } from 'lucide-react'
import type { Attachment, Conversation, SharedMediaItem } from '../../../api/types'
import { getSharedMedia, isAbortError } from '../../../api/client'
import { authenticatedMediaUrl } from '../../../api/session'
import { fmtShortDate } from '../../../utils/time'

type SharedMediaTab = 'media' | 'files' | 'links'

function formatFileSize(bytes: number | null): string {
  if (bytes == null) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** The item's shown attachment (shared-media rows are messages with ≥1 attachment) */
function firstAttachment(item: SharedMediaItem): Attachment | null {
  return item.attachments?.[0] ?? null
}

function extractUrls(text: string | null): string[] {
  if (!text) return []
  const matches = text.match(/https?:\/\/[^\s<>"]+/g)
  return matches ?? []
}

/** Media / files / links shared in the conversation, one tab at a time */
export default function SharedMediaSection({ chat }: { chat: Conversation }) {
  const [activeTab, setActiveTab] = useState<SharedMediaTab>('media')
  const [items, setItems] = useState<SharedMediaItem[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    setLoading(true)
    setItems([])
    // Abort on tab/chat switch so a slower earlier response can't populate
    // the currently active tab with the wrong items
    const ac = new AbortController()
    getSharedMedia(chat.id, activeTab, 50, 0, ac.signal)
      .then((data) => {
        setItems(data)
        setLoading(false)
      })
      .catch((e) => {
        if (isAbortError(e)) return
        setItems([])
        setLoading(false)
      })
    return () => ac.abort()
  }, [chat.id, activeTab])

  const tabs: { key: SharedMediaTab; label: string; icon: React.ReactNode }[] = [
    { key: 'media', label: 'Media', icon: <Image size={11} /> },
    { key: 'files', label: 'Files', icon: <FileText size={11} /> },
    { key: 'links', label: 'Links', icon: <LinkIcon size={11} /> },
  ]

  return (
    <div className="px-4 py-4 border-t border-slate-700/50 flex flex-col gap-3">
      {/* Tab bar */}
      <div className="flex gap-1 rounded-lg bg-slate-700/50 p-0.5">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`flex flex-1 items-center justify-center gap-1 rounded-md px-2 py-1.5 text-[11px] font-medium transition-colors ${
              activeTab === tab.key
                ? 'bg-slate-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            {tab.icon}
            {tab.label}
          </button>
        ))}
      </div>

      {/* Content */}
      {loading ? (
        <p className="text-xs text-slate-500">Loading...</p>
      ) : items.length === 0 ? (
        <p className="text-xs text-slate-500">
          No {activeTab === 'media' ? 'media' : activeTab === 'files' ? 'files' : 'links'} shared yet
        </p>
      ) : activeTab === 'media' ? (
        <div className="grid grid-cols-3 gap-1 max-h-64 overflow-y-auto">
          {items.map((item) => ({ item, media: firstAttachment(item) })).map(({ item, media }) => (
            <div key={item.id} className="relative aspect-square overflow-hidden rounded">
              {media?.url && media.kind !== 'video' ? (
                <img
                  src={authenticatedMediaUrl(media.url)}
                  alt=""
                  className="h-full w-full object-cover hover:opacity-80 transition-opacity cursor-pointer"
                />
              ) : (
                <div className="flex h-full w-full items-center justify-center bg-slate-700 text-slate-500">
                  <Image size={16} />
                </div>
              )}
              {media?.kind === 'gif' && (
                <span className="absolute bottom-0.5 left-0.5 rounded bg-black/60 px-1 text-[8px] text-white">GIF</span>
              )}
              {media?.kind === 'video' && (
                <span className="absolute bottom-0.5 left-0.5 rounded bg-black/60 px-1 text-[8px] text-white">VID</span>
              )}
            </div>
          ))}
        </div>
      ) : activeTab === 'files' ? (
        <div className="flex flex-col gap-1.5 max-h-64 overflow-y-auto">
          {items.map((item) => ({ item, media: firstAttachment(item) })).map(({ item, media }) => (
            <a
              key={item.id}
              href={media?.url ? authenticatedMediaUrl(media.url) : '#'}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-2 rounded-lg bg-slate-700/50 px-2.5 py-2 hover:bg-slate-700 transition-colors"
            >
              <FileText size={14} className="flex-shrink-0 text-slate-400" />
              <div className="min-w-0 flex-1">
                <p className="truncate text-xs text-slate-200">
                  {media?.filename || 'Document'}
                </p>
                <p className="text-[10px] text-slate-500">
                  {formatFileSize(media?.size ?? null)}
                  {item.date && ` \u00b7 ${fmtShortDate(item.date)}`}
                </p>
              </div>
              <Download size={12} className="flex-shrink-0 text-slate-500" />
            </a>
          ))}
        </div>
      ) : (
        <div className="flex flex-col gap-1.5 max-h-64 overflow-y-auto">
          {items.map((item) => {
            const urls = extractUrls(item.text)
            return urls.map((url, urlIdx) => (
              <a
                key={`${item.id}-${urlIdx}`}
                href={url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-2 rounded-lg bg-slate-700/50 px-2.5 py-2 hover:bg-slate-700 transition-colors"
              >
                <ExternalLink size={12} className="flex-shrink-0 text-indigo-400" />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-xs text-indigo-300">{url}</p>
                  {item.sender_name && (
                    <p className="text-[10px] text-slate-500">
                      {item.sender_name}
                      {item.date && ` \u00b7 ${fmtShortDate(item.date)}`}
                    </p>
                  )}
                </div>
              </a>
            ))
          })}
        </div>
      )}
    </div>
  )
}
