/**
 * One dispatch for every message media type. Single messages and album items
 * both render through here, so each type behaves the same everywhere
 * (download pipeline, retry, lightbox).
 */
import { memo, useState } from 'react'
import { Image as ImageIcon, Loader2, RotateCcw } from 'lucide-react'
import type { Message } from '../api/types'
import { authenticatedMediaUrl } from '../api/session'
import { useMediaDownload } from '../hooks/useMediaDownload'
import ImageLightbox from './ImageLightbox'
import VideoPlayer from './VideoPlayer'
import AudioPlayer from './AudioPlayer'
import DocumentCard from './DocumentCard'
import GifPlayer from './GifPlayer'

type MediaMessage = Pick<Message, 'chat_id' | 'tg_message_id' | 'media_type' | 'media_url' | 'media_filename' | 'media_size'>

interface Props {
  message: MediaMessage
  isOut: boolean
  /** Album cell: fill the grid cell */
  compact?: boolean
  /** Album: open the shared lightbox (receives the authenticated URL) instead of a per-item one */
  onOpen?: (src: string) => void
}

/** Media types MediaView renders (anything else is shown as a "[type]" label) */
export const RENDERED_MEDIA = new Set(['photo', 'video', 'video_note', 'voice', 'audio', 'gif', 'document'])

export function PhotoView({ message, isOut, compact, onOpen }: Props) {
  const media = useMediaDownload(message.chat_id, message.tg_message_id, { auto: true, initialUrl: message.media_url })
  const [lightbox, setLightbox] = useState(false)
  const tone = isOut ? 'text-indigo-300' : 'text-slate-400'

  if (!media.url) {
    const failed = media.status === 'error'
    return (
      <button
        type="button"
        onClick={failed ? media.retry : undefined}
        disabled={!failed}
        className={`flex items-center justify-center gap-2 px-3 py-4 text-xs ${tone} ${compact ? 'h-full w-full bg-black/20' : ''}`}
        style={compact ? { minHeight: 80 } : { minWidth: 160 }}
        title={failed ? media.error ?? 'Photo failed to load' : undefined}
      >
        {failed ? <RotateCcw size={14} /> : media.status === 'idle' ? <ImageIcon size={14} /> : <Loader2 size={14} className="animate-spin" />}
        {!compact && (failed ? 'Photo unavailable — retry' : 'Loading photo…')}
      </button>
    )
  }

  const src = authenticatedMediaUrl(media.url)
  return (
    <>
      <img
        src={src}
        alt="photo"
        className={compact
          ? 'block h-full w-full cursor-pointer object-cover hover:opacity-90 transition-opacity'
          : `block cursor-pointer object-cover ${isOut ? 'ml-auto' : 'mr-auto'} hover:opacity-90 transition-opacity`}
        style={compact ? { minHeight: 80 } : { maxWidth: 320, maxHeight: 320 }}
        onClick={() => (onOpen ? onOpen(src) : setLightbox(true))}
      />
      {lightbox && <ImageLightbox src={src} onClose={() => setLightbox(false)} />}
    </>
  )
}

function MediaView({ message, isOut, compact, onOpen }: Props) {
  const common = { chatTelegramId: message.chat_id, msgTelegramId: message.tg_message_id, mediaUrl: message.media_url ?? null, isOut }
  switch (message.media_type) {
    case 'photo':
      return <PhotoView message={message} isOut={isOut} compact={compact} onOpen={onOpen} />
    case 'video':
    case 'video_note':
      return <VideoPlayer {...common} isVideoNote={message.media_type === 'video_note' && !compact} compact={compact} />
    case 'voice':
    case 'audio':
      return <AudioPlayer {...common} mediaType={message.media_type} />
    case 'gif':
      return <GifPlayer {...common} />
    case 'document':
      return <DocumentCard {...common} filename={message.media_filename ?? null} fileSize={message.media_size ?? null} />
    default:
      return null
  }
}

export default memo(MediaView)
