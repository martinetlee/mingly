import { memo, useState } from 'react'
import { Link } from 'react-router-dom'
import { authenticatedMediaUrl } from '../api/session'

const AVATAR_COLORS = [
  'bg-rose-600',
  'bg-amber-600',
  'bg-emerald-600',
  'bg-sky-600',
  'bg-violet-600',
  'bg-pink-600',
  'bg-teal-600',
  'bg-orange-600',
]

// Module-level set of ids whose photos failed to load (avoids repeated 404s)
const failedPhotos = new Set<number>()

interface SenderAvatarProps {
  /** Telegram id of a user — or of a chat (profile photos share one store) */
  senderId: number | null
  senderName: string | null
  size?: number
  onContextMenu?: (e: React.MouseEvent) => void
  /** Wrap in a link to the profile (default true; false for chat avatars) */
  link?: boolean
}

function SenderAvatar({ senderId, senderName, size = 28, onContextMenu, link = true }: SenderAvatarProps) {
  const [imgFailed, setImgFailed] = useState(false)

  const initial = (senderName || '?')[0].toUpperCase()
  const colorIndex = senderId != null ? Math.abs(senderId) % AVATAR_COLORS.length : 0
  const bgColor = AVATAR_COLORS[colorIndex]

  const hasPhoto = senderId != null && !failedPhotos.has(senderId) && !imgFailed
  const photoUrl = senderId != null
    ? authenticatedMediaUrl(`/api/media/profile_photos/${senderId}.jpg`)
    : null

  const handleError = () => {
    if (senderId != null) failedPhotos.add(senderId)
    setImgFailed(true)
  }

  const avatar = (
    <div
      className="relative flex-shrink-0"
      style={{ width: size, height: size }}
      onContextMenu={link ? undefined : onContextMenu}
    >
      {/* Fallback initial — always rendered underneath */}
      <div
        className={`absolute inset-0 flex items-center justify-center rounded-full text-white font-medium ${bgColor}`}
        style={{ fontSize: size * 0.4 }}
      >
        {initial}
      </div>

      {/* Photo overlay — renders on top when available */}
      {hasPhoto && photoUrl && (
        <img
          src={photoUrl}
          alt=""
          className="absolute inset-0 rounded-full object-cover"
          style={{ width: size, height: size }}
          onError={handleError}
        />
      )}
    </div>
  )

  if (link && senderId != null) {
    return (
      <Link to={`/profile/${senderId}`} className="flex-shrink-0" onContextMenu={onContextMenu} aria-label={senderName ?? undefined}>
        {avatar}
      </Link>
    )
  }

  return avatar
}

export default memo(SenderAvatar)
