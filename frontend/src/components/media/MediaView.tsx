/**
 * One dispatch for every attachment kind. Single attachments and album items
 * both render through here, so each kind behaves the same everywhere
 * (download pipeline, retry, lightbox).
 */
import { memo, useState } from 'react'
import { Image as ImageIcon, Loader2, RotateCcw } from 'lucide-react'
import type { Attachment } from '../../api/types'
import { authenticatedMediaUrl } from '../../api/session'
import { useMediaDownload } from '../../hooks/useMediaDownload'
import ImageLightbox from './ImageLightbox'
import VideoPlayer from './VideoPlayer'
import AudioPlayer from './AudioPlayer'
import DocumentCard from './DocumentCard'
import GifPlayer from './GifPlayer'

interface Props {
  attachment: Attachment
  isOut: boolean
  /** Album cell: fill the grid cell */
  compact?: boolean
  /** Album: open the shared lightbox (receives the authenticated URL) instead of a per-item one */
  onOpen?: (src: string) => void
}

/** Attachment kinds MediaView renders (anything else is shown as a "[kind]" label) */
export const RENDERED_MEDIA = new Set(['photo', 'video', 'video_note', 'voice', 'audio', 'gif', 'document'])

/** Kinds shown in an album grid cell (the rest render as their own cards below it) */
export const VISUAL_MEDIA = new Set(['photo', 'video', 'gif'])

export function PhotoView({ attachment, isOut, compact, onOpen }: Props) {
  const media = useMediaDownload(attachment.id, { auto: true, initialUrl: attachment.url })
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
        alt={attachment.filename ?? 'photo'}
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

function MediaView({ attachment, isOut, compact, onOpen }: Props) {
  const common = { attachmentId: attachment.id, mediaUrl: attachment.url ?? null, isOut }
  switch (attachment.kind) {
    case 'photo':
      return <PhotoView attachment={attachment} isOut={isOut} compact={compact} onOpen={onOpen} />
    case 'video':
    case 'video_note':
      return <VideoPlayer {...common} isVideoNote={attachment.kind === 'video_note' && !compact} compact={compact} />
    case 'voice':
    case 'audio':
      return <AudioPlayer {...common} mediaType={attachment.kind} />
    case 'gif':
      return <GifPlayer {...common} />
    case 'document':
      return <DocumentCard {...common} filename={attachment.filename ?? null} fileSize={attachment.size ?? null} />
    default:
      return null
  }
}

export default memo(MediaView)
