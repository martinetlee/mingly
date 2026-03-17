import { useState } from 'react'
import type { Message } from '../api/types'
import { authenticatedMediaUrl } from '../api/session'
import ImageLightbox from './ImageLightbox'

interface Props {
  messages: Message[]
  isOut: boolean
}

export default function MediaAlbumGrid({ messages, isOut }: Props) {
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null)

  const mediaMessages = messages.filter(
    (m) => m.media_url && (m.media_type === 'photo' || m.media_type === 'video')
  )

  if (mediaMessages.length === 0) return null

  const count = mediaMessages.length

  // Determine grid layout based on count
  let gridClass = 'grid gap-0.5'
  if (count === 1) {
    gridClass += ' grid-cols-1'
  } else if (count === 2) {
    gridClass += ' grid-cols-2'
  } else if (count === 3) {
    gridClass += ' grid-cols-2'
  } else {
    gridClass += ' grid-cols-2'
  }

  return (
    <div
      className={`overflow-hidden ${isOut ? 'ml-auto' : 'mr-auto'}`}
      style={{ maxWidth: 360 }}
    >
      <div className={gridClass}>
        {mediaMessages.map((m, i) => {
          // For 3 items: first image spans full width
          const spanFull = count === 3 && i === 0

          return (
            <div
              key={m.id}
              className={`relative cursor-pointer overflow-hidden ${
                spanFull ? 'col-span-2' : ''
              }`}
              onClick={() => setLightboxIndex(i)}
            >
              <img
                src={authenticatedMediaUrl(m.media_url!)}
                alt={`album item ${i + 1}`}
                className="block h-full w-full object-cover hover:opacity-90 transition-opacity"
                style={{
                  maxHeight: spanFull ? 240 : 180,
                  minHeight: 80,
                }}
              />
              {m.media_type === 'video' && (
                <span className="absolute bottom-1 left-1 rounded bg-black/60 px-1 py-0.5 text-[9px] text-white">
                  VIDEO
                </span>
              )}
            </div>
          )
        })}
      </div>

      {lightboxIndex !== null && mediaMessages[lightboxIndex]?.media_url && (
        <ImageLightbox
          src={authenticatedMediaUrl(mediaMessages[lightboxIndex].media_url!)}
          onClose={() => setLightboxIndex(null)}
        />
      )}
    </div>
  )
}
