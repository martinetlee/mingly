import { useState } from 'react'
import { Loader2, Play, RotateCcw } from 'lucide-react'
import { authenticatedMediaUrl } from '../api/session'
import { useMediaDownload } from '../hooks/useMediaDownload'
import ImageLightbox from './ImageLightbox'

interface Props {
  attachmentId: number
  mediaUrl: string | null
  isOut: boolean
}

export default function GifPlayer({ attachmentId, mediaUrl, isOut }: Props) {
  // GIFs are small: download automatically (through the shared, rate-limited pipeline)
  const media = useMediaDownload(attachmentId, { auto: true, initialUrl: mediaUrl })
  const [playing, setPlaying] = useState(false)
  const [lightboxOpen, setLightboxOpen] = useState(false)

  if (!media.url) {
    const failed = media.status === 'error'
    return (
      <button
        onClick={failed ? media.retry : undefined}
        disabled={!failed}
        className={`flex items-center gap-2 px-3 py-4 text-xs ${isOut ? 'text-indigo-300' : 'text-slate-400'}`}
      >
        {failed ? <RotateCcw size={14} /> : <Loader2 size={14} className="animate-spin" />}
        {failed ? `${media.error ?? 'GIF failed to load'} — retry` : 'Loading GIF…'}
      </button>
    )
  }

  const url = authenticatedMediaUrl(media.url)
  const badge = (
    <span className="absolute bottom-2 left-2 rounded bg-black/60 px-1.5 py-0.5 text-[10px] font-medium text-white">GIF</span>
  )

  if (playing) {
    return (
      <>
        <div className="relative">
          <img
            src={url}
            alt="GIF"
            className={`block cursor-pointer object-cover hover:opacity-90 transition-opacity ${isOut ? 'ml-auto' : 'mr-auto'}`}
            style={{ maxWidth: 320, maxHeight: 320 }}
            onClick={() => setLightboxOpen(true)}
          />
          {badge}
        </div>
        {lightboxOpen && <ImageLightbox src={url} onClose={() => setLightboxOpen(false)} />}
      </>
    )
  }

  return (
    <div
      className={`relative cursor-pointer group ${isOut ? 'ml-auto' : 'mr-auto'}`}
      style={{ maxWidth: 320, maxHeight: 320 }}
      onClick={() => setPlaying(true)}
    >
      <img
        src={url}
        alt="GIF thumbnail"
        className="block object-cover opacity-80 group-hover:opacity-100 transition-opacity"
        style={{ maxWidth: 320, maxHeight: 320 }}
      />
      <div className="absolute inset-0 flex items-center justify-center">
        <div className="flex h-12 w-12 items-center justify-center rounded-full bg-black/50 text-white group-hover:bg-black/70 transition-colors">
          <Play size={22} className="ml-0.5" />
        </div>
      </div>
      {badge}
    </div>
  )
}
