import { useState } from 'react'
import type { Message } from '../api/types'
import ImageLightbox from './ImageLightbox'
import MediaView from './MediaView'

interface Props {
  messages: Message[]
  isOut: boolean
}

/**
 * Album grid. Every item renders through MediaView (photos auto-download,
 * videos are click-to-load and play in the video lightbox, documents/audio
 * render as their own cards), so items without a stored media_url aren't
 * dropped and videos never render as <img>.
 */
export default function MediaAlbumGrid({ messages, isOut }: Props) {
  const [lightboxSrc, setLightboxSrc] = useState<string | null>(null)
  const visual = messages.filter((m) => m.media_type === 'photo' || m.media_type === 'video' || m.media_type === 'gif')
  const other = messages.filter((m) => m.media_type && !visual.includes(m))
  const count = visual.length

  return (
    <div className={isOut ? 'ml-auto' : 'mr-auto'} style={{ maxWidth: 360 }}>
      {count > 0 && (
        <div className={`grid gap-0.5 ${count === 1 ? 'grid-cols-1' : 'grid-cols-2'}`}>
          {visual.map((m, i) => {
            // 3 items: the first spans the full width
            const spanFull = count === 3 && i === 0
            return (
              <div
                key={m.tg_message_id}
                className={`relative overflow-hidden ${spanFull ? 'col-span-2' : ''}`}
                style={{ height: spanFull ? 240 : count === 1 ? undefined : 180 }}
              >
                <MediaView
                  message={m}
                  isOut={isOut}
                  compact
                  onOpen={setLightboxSrc}
                />
              </div>
            )
          })}
        </div>
      )}
      {other.map((m) => (
        <MediaView key={m.tg_message_id} message={m} isOut={isOut} />
      ))}
      {lightboxSrc && <ImageLightbox src={lightboxSrc} onClose={() => setLightboxSrc(null)} />}
    </div>
  )
}
