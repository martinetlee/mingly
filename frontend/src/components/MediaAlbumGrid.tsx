import { useState } from 'react'
import type { Attachment } from '../api/types'
import ImageLightbox from './ImageLightbox'
import MediaView, { VISUAL_MEDIA } from './MediaView'

interface Props {
  /** Every attachment of the album (a grouped set of messages, or one message with several attachments) */
  attachments: Attachment[]
  isOut: boolean
}

/**
 * Album grid. Every item renders through MediaView (photos auto-download,
 * videos are click-to-load and play in the video lightbox, documents/audio
 * render as their own cards), so items without a stored url aren't
 * dropped and videos never render as <img>.
 */
export default function MediaAlbumGrid({ attachments, isOut }: Props) {
  const [lightboxSrc, setLightboxSrc] = useState<string | null>(null)
  const visual = attachments.filter((a) => VISUAL_MEDIA.has(a.kind))
  const other = attachments.filter((a) => !VISUAL_MEDIA.has(a.kind))
  const count = visual.length

  return (
    <div className={isOut ? 'ml-auto' : 'mr-auto'} style={{ maxWidth: 360 }}>
      {count > 0 && (
        <div className={`grid gap-0.5 ${count === 1 ? 'grid-cols-1' : 'grid-cols-2'}`}>
          {visual.map((a, i) => {
            // 3 items: the first spans the full width
            const spanFull = count === 3 && i === 0
            return (
              <div
                key={a.id}
                className={`relative overflow-hidden ${spanFull ? 'col-span-2' : ''}`}
                style={{ height: spanFull ? 240 : count === 1 ? undefined : 180 }}
              >
                <MediaView
                  attachment={a}
                  isOut={isOut}
                  compact
                  onOpen={setLightboxSrc}
                />
              </div>
            )
          })}
        </div>
      )}
      {other.map((a) => (
        <MediaView key={a.id} attachment={a} isOut={isOut} />
      ))}
      {lightboxSrc && <ImageLightbox src={lightboxSrc} onClose={() => setLightboxSrc(null)} />}
    </div>
  )
}
