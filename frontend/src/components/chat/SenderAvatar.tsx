import { memo, useState } from 'react'
import { Link } from 'react-router-dom'
import { authenticatedMediaUrl } from '../../api/session'
import { personUrl } from '../../lib/routes'

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

// Module-level set of photo URLs that failed to load (avoids repeated 404s)
const failedPhotos = new Set<string>()

function hashSeed(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0
  return h
}

interface SenderAvatarProps {
  /** Served avatar (person / identity / conversation avatar_url); null → initial only */
  avatarUrl: string | null | undefined
  name: string | null
  /** Person the avatar links to (and seeds the fallback colour) */
  personId?: number | null
  /** Fallback-colour seed when there is no person (e.g. a conversation id) */
  colorSeed?: number | null
  size?: number
  onContextMenu?: (e: React.MouseEvent) => void
  /** Wrap in a link to the person's page (default true; needs personId) */
  link?: boolean
}

function SenderAvatar({ avatarUrl, name, personId = null, colorSeed = null, size = 28, onContextMenu, link = true }: SenderAvatarProps) {
  const [failedUrl, setFailedUrl] = useState<string | null>(null)

  const initial = (name || '?')[0].toUpperCase()
  const seed = personId ?? colorSeed ?? hashSeed(name ?? '')
  const bgColor = AVATAR_COLORS[Math.abs(seed) % AVATAR_COLORS.length]

  const hasPhoto = !!avatarUrl && !failedPhotos.has(avatarUrl) && failedUrl !== avatarUrl
  const photoUrl = avatarUrl ? authenticatedMediaUrl(avatarUrl) : null

  const handleError = () => {
    if (!avatarUrl) return
    failedPhotos.add(avatarUrl)
    setFailedUrl(avatarUrl)
  }

  const avatar = (
    <div
      className="relative flex-shrink-0"
      style={{ width: size, height: size }}
      onContextMenu={link && personId != null ? undefined : onContextMenu}
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

  if (link && personId != null) {
    return (
      <Link to={personUrl(personId)} className="flex-shrink-0" onContextMenu={onContextMenu} aria-label={name ?? undefined}>
        {avatar}
      </Link>
    )
  }

  return avatar
}

export default memo(SenderAvatar)
