import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Star, BadgeCheck, Crown, Bot } from 'lucide-react'
import type { PersonSummary } from '../api/types'
import { resolvePerson } from '../api/client'
import { sanitizeLinkedInUrl } from '../utils/sanitizeUrl'

// Module-level cache for resolved person data (session lifetime)
const personCache = new Map<number, PersonSummary>()
const pendingRequests = new Map<number, Promise<PersonSummary>>()

export function invalidatePersonCache(telegramId?: number) {
  if (telegramId != null) {
    personCache.delete(telegramId)
  } else {
    personCache.clear()
  }
}

export function preloadPersonCache(data: Record<number, PersonSummary>) {
  for (const [tid, summary] of Object.entries(data)) {
    personCache.set(Number(tid), summary)
  }
}

export const RELATIONSHIP_STATUS_COLORS: Record<string, string> = {
  new: 'bg-blue-500/20 text-blue-400',
  active: 'bg-emerald-500/20 text-emerald-400',
  nurturing: 'bg-amber-500/20 text-amber-400',
  stalled: 'bg-orange-500/20 text-orange-400',
  cold: 'bg-slate-500/20 text-slate-400',
  needs_reply: 'bg-red-500/20 text-red-400',
}

export const RELATIONSHIP_STATUS_LABELS: Record<string, string> = {
  new: 'New',
  active: 'Active',
  nurturing: 'Nurturing',
  stalled: 'Stalled',
  cold: 'Cold',
  needs_reply: 'Needs Reply',
}

const ONLINE_STATUS_LABELS: Record<string, string> = {
  online: 'Online',
  recently: 'Last seen recently',
  offline: 'Offline',
  last_week: 'Last seen within a week',
  last_month: 'Last seen within a month',
  unknown: 'Last seen a long time ago',
}

const ONLINE_STATUS_COLORS: Record<string, string> = {
  online: 'text-emerald-400',
  recently: 'text-slate-400',
  offline: 'text-slate-500',
}

export function fmtRelative(iso: string): string {
  const d = new Date(iso)
  const now = new Date()
  const diffMs = now.getTime() - d.getTime()
  const diffDays = Math.floor(diffMs / 86400000)
  if (diffDays === 0) return 'Today'
  if (diffDays === 1) return 'Yesterday'
  if (diffDays < 7) return `${diffDays}d ago`
  if (diffDays < 30) return `${Math.floor(diffDays / 7)}w ago`
  if (diffDays < 365) return `${Math.floor(diffDays / 30)}mo ago`
  return `${Math.floor(diffDays / 365)}y ago`
}

interface PersonCardProps {
  telegramId: number
  name?: string
  username?: string | null
  status?: string | null
  position?: { title: string | null; organization: string } | null
  lastInteraction?: string | null
  isImportant?: boolean
  contactId?: number | null
  phone?: string | null
  twitterHandle?: string | null
  linkedinUrl?: string | null
  hasNotes?: boolean
  photoUrl?: string | null
  isVerified?: boolean
  isPremium?: boolean
  isBot?: boolean
  onlineStatus?: string | null
  bio?: string | null
  mode?: 'compact' | 'full' | 'profile' | 'header'
  onClick?: () => void
  showStatus?: boolean
  disableLink?: boolean
  extraBadge?: React.ReactNode
  actions?: React.ReactNode
  className?: string
  muted?: boolean
}

function useResolvedPerson(
  telegramId: number,
  preloaded: boolean,
): PersonSummary | null {
  const [person, setPerson] = useState<PersonSummary | null>(
    personCache.get(telegramId) ?? null,
  )

  useEffect(() => {
    if (preloaded) return
    const cached = personCache.get(telegramId)
    if (cached) {
      setPerson(cached)
      return
    }
    let req = pendingRequests.get(telegramId)
    if (!req) {
      req = resolvePerson(telegramId)
      pendingRequests.set(telegramId, req)
    }
    req
      .then((data) => {
        personCache.set(telegramId, data)
        setPerson(data)
      })
      .catch(() => {
        setPerson({
          telegram_id: telegramId,
          name: String(telegramId),
          username: null,
          is_important: false,
          contact_id: null,
          relationship_status: null,
          last_interaction_at: null,
          primary_position: null,
          phone: null,
          twitter_handle: null,
          linkedin_url: null,
          has_notes: false,
        })
      })
      .finally(() => {
        pendingRequests.delete(telegramId)
      })
  }, [telegramId, preloaded])

  return person
}

export default function PersonCard({
  telegramId,
  name: propName,
  username: propUsername,
  status: propStatus,
  position: propPosition,
  lastInteraction: propLastInteraction,
  isImportant: propIsImportant,
  contactId: propContactId,
  phone: propPhone,
  twitterHandle: propTwitter,
  linkedinUrl: propLinkedin,
  hasNotes: propHasNotes,
  photoUrl,
  isVerified,
  isPremium,
  isBot: propIsBot,
  onlineStatus,
  bio,
  mode = 'compact',
  onClick,
  showStatus = true,
  disableLink = false,
  extraBadge,
  actions,
  className = '',
  muted = false,
}: PersonCardProps) {
  const hasPreloaded = propName != null
  const resolved = useResolvedPerson(telegramId, hasPreloaded)

  const name = propName ?? resolved?.name ?? ''
  const username = propUsername !== undefined ? propUsername : resolved?.username
  const status = propStatus !== undefined ? propStatus : resolved?.relationship_status
  const position = propPosition !== undefined ? propPosition : resolved?.primary_position
  const lastInteraction = propLastInteraction !== undefined ? propLastInteraction : resolved?.last_interaction_at
  const isImportant = propIsImportant ?? resolved?.is_important ?? false
  const contactId = propContactId !== undefined ? propContactId : resolved?.contact_id
  const phone = propPhone !== undefined ? propPhone : resolved?.phone
  const twitterHandle = propTwitter !== undefined ? propTwitter : resolved?.twitter_handle
  const linkedinUrl = propLinkedin !== undefined ? propLinkedin : resolved?.linkedin_url
  const hasNotes = propHasNotes ?? resolved?.has_notes ?? false

  const profileLink = contactId ? `/contacts` : `/profile/${telegramId}`
  const initial = name ? name.charAt(0).toUpperCase() : '?'

  if (!name && !resolved) {
    return (
      <div className={`flex items-center gap-2 px-2 py-1.5 ${className}`}>
        <div className="h-6 w-6 rounded-full bg-slate-700 animate-pulse" />
        <div className="h-3 w-20 rounded bg-slate-700 animate-pulse" />
      </div>
    )
  }

  if (mode === 'header') {
    return (
      <div
        className={`flex items-center gap-3 ${onClick ? 'cursor-pointer' : ''} ${className}`}
        onClick={onClick}
      >
        <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-slate-700 text-sm font-medium text-slate-300">
          {initial}
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <h2 className="text-sm font-semibold text-gray-100">{name}</h2>
            {isImportant && (
              <Star size={11} className="flex-shrink-0 fill-amber-400 text-amber-400" />
            )}
            {showStatus && status && (
              <span className={`flex-shrink-0 rounded-full px-1.5 py-0.5 text-[9px] font-medium ${RELATIONSHIP_STATUS_COLORS[status] ?? ''}`}>
                {RELATIONSHIP_STATUS_LABELS[status] ?? status}
              </span>
            )}
          </div>
          {username && (
            <p className="text-xs text-slate-400">@{username}</p>
          )}
        </div>
        {extraBadge && <div className="flex-shrink-0">{extraBadge}</div>}
        {actions && <div className="flex-shrink-0">{actions}</div>}
      </div>
    )
  }

  if (mode === 'profile') {
    const avatar = photoUrl ? (
      <img
        src={photoUrl}
        alt={name}
        className="h-20 w-20 rounded-full object-cover ring-2 ring-slate-700"
      />
    ) : (
      <div className="flex h-20 w-20 items-center justify-center rounded-full bg-slate-700 text-2xl font-semibold text-slate-300 ring-2 ring-slate-600">
        {initial}
      </div>
    )

    return (
      <div className={className}>
        <div className="flex items-start gap-5 mb-8">
          {!disableLink ? (
            <Link to={`/profile/${telegramId}`}>{avatar}</Link>
          ) : (
            avatar
          )}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h1 className="text-xl font-bold text-gray-100">{name}</h1>
              {isVerified && (
                <span title="Verified"><BadgeCheck size={18} className="text-sky-400" /></span>
              )}
              {isPremium && (
                <span title="Premium"><Crown size={16} className="text-amber-400" /></span>
              )}
              {propIsBot && (
                <span title="Bot"><Bot size={16} className="text-violet-400" /></span>
              )}
              {isImportant && (
                <Star size={14} className="flex-shrink-0 fill-amber-400 text-amber-400" />
              )}
              {showStatus && status && (
                <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${RELATIONSHIP_STATUS_COLORS[status] ?? ''}`}>
                  {RELATIONSHIP_STATUS_LABELS[status] ?? status}
                </span>
              )}
              {actions}
            </div>
            {username && (
              <p className="mt-0.5 text-sm text-indigo-400">@{username}</p>
            )}
            {onlineStatus && (
              <p className={`mt-1 text-xs ${ONLINE_STATUS_COLORS[onlineStatus] ?? 'text-slate-500'}`}>
                {ONLINE_STATUS_LABELS[onlineStatus] ?? onlineStatus}
              </p>
            )}
            {position && (
              <p className="text-xs text-slate-400 mt-0.5">
                {position.title ? `${position.title} @ ${position.organization}` : position.organization}
              </p>
            )}
            {lastInteraction && (
              <p className="text-xs text-slate-500 mt-0.5">
                Last talked: {fmtRelative(lastInteraction)}
              </p>
            )}
          </div>
        </div>

        {bio && (
          <div className="mb-6 rounded-xl bg-slate-800 border border-slate-700/50 p-4">
            <h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-slate-500">Bio</h3>
            <p className="text-sm text-gray-200 whitespace-pre-wrap leading-relaxed">{bio}</p>
          </div>
        )}

        {(phone || twitterHandle || linkedinUrl || hasNotes) && (
          <div className="flex flex-wrap gap-3 text-xs text-slate-400">
            {phone && (
              <span title={phone}>
                <span className="text-slate-500">Phone:</span> {phone}
              </span>
            )}
            {twitterHandle && (
              <a
                href={`https://twitter.com/${twitterHandle.replace('@', '')}`}
                target="_blank"
                rel="noopener noreferrer"
                className="hover:text-sky-400 transition-colors"
              >
                @{twitterHandle.replace('@', '')}
              </a>
            )}
            {linkedinUrl && (() => {
              const safeHref = sanitizeLinkedInUrl(linkedinUrl)
              return safeHref ? (
                <a
                  href={safeHref}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="hover:text-sky-400 transition-colors"
                >
                  LinkedIn
                </a>
              ) : (
                <span>LinkedIn (invalid URL)</span>
              )
            })()}
            {hasNotes && (
              <span className="text-slate-500">Has notes</span>
            )}
          </div>
        )}
      </div>
    )
  }

  if (mode === 'compact') {
    const content = (
      <div
        className={`flex items-center gap-2 rounded-lg px-2 py-1.5 transition-colors group ${
          onClick ? 'cursor-pointer hover:bg-slate-700/50' : ''
        } ${muted ? 'opacity-60' : ''} ${className}`}
        onClick={onClick}
      >
        <div className={`flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full text-[10px] font-medium ${
          muted ? 'bg-slate-700/60 text-slate-400' : 'bg-slate-700 text-slate-300'
        }`}>
          {initial}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1">
            <p title={name} className={`truncate text-xs font-medium ${
              muted ? 'text-slate-400 group-hover:text-slate-300' : 'text-gray-200 group-hover:text-indigo-300'
            } transition-colors`}>
              {name}
            </p>
            {isImportant && (
              <Star size={9} className="flex-shrink-0 fill-amber-400 text-amber-400" />
            )}
            {showStatus && status && (
              <span className={`flex-shrink-0 rounded-full px-1.5 py-0.5 text-[8px] font-medium ${RELATIONSHIP_STATUS_COLORS[status] ?? ''}`}>
                {RELATIONSHIP_STATUS_LABELS[status] ?? status}
              </span>
            )}
          </div>
          <div className="flex items-center gap-1 text-[10px] text-slate-500 truncate">
            {username && <span>@{username}</span>}
            {username && position && <span>&middot;</span>}
            {position && (
              <span>{position.title ? `${position.title} @ ${position.organization}` : position.organization}</span>
            )}
            {(username || position) && lastInteraction && <span>&middot;</span>}
            {lastInteraction && <span>{fmtRelative(lastInteraction)}</span>}
          </div>
        </div>
        {extraBadge && <div className="flex-shrink-0">{extraBadge}</div>}
      </div>
    )

    if (!onClick && !disableLink) {
      return <Link to={profileLink}>{content}</Link>
    }
    return content
  }

  // Full mode
  return (
    <div className={`${className}`}>
      <div className="flex items-start gap-4 mb-4">
        {!disableLink ? (
          <Link to={`/profile/${telegramId}`}>
            <div className="flex h-16 w-16 items-center justify-center rounded-full bg-slate-700 text-xl font-semibold text-slate-300 hover:ring-2 hover:ring-indigo-500 transition-all">
              {initial}
            </div>
          </Link>
        ) : (
          <div className="flex h-16 w-16 items-center justify-center rounded-full bg-slate-700 text-xl font-semibold text-slate-300">
            {initial}
          </div>
        )}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            {!disableLink ? (
              <Link
                to={`/profile/${telegramId}`}
                className="text-lg font-bold text-gray-100 hover:text-indigo-300 transition-colors"
              >
                {name}
              </Link>
            ) : (
              <span className="text-lg font-bold text-gray-100">{name}</span>
            )}
            {isImportant && (
              <Star size={14} className="flex-shrink-0 fill-amber-400 text-amber-400" />
            )}
            {showStatus && status && (
              <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${RELATIONSHIP_STATUS_COLORS[status] ?? ''}`}>
                {RELATIONSHIP_STATUS_LABELS[status] ?? status}
              </span>
            )}
            {actions}
          </div>
          {username && (
            <p className="text-sm text-indigo-400">@{username}</p>
          )}
          {position && (
            <p className="text-xs text-slate-400 mt-0.5">
              {position.title ? `${position.title} @ ${position.organization}` : position.organization}
            </p>
          )}
          {lastInteraction && (
            <p className="text-xs text-slate-500 mt-0.5">
              Last talked: {fmtRelative(lastInteraction)}
            </p>
          )}
        </div>
      </div>

      {(phone || twitterHandle || linkedinUrl || hasNotes) && (
        <div className="flex flex-wrap gap-3 text-xs text-slate-400">
          {phone && (
            <span title={phone}>
              <span className="text-slate-500">Phone:</span> {phone}
            </span>
          )}
          {twitterHandle && (
            <a
              href={`https://twitter.com/${twitterHandle.replace('@', '')}`}
              target="_blank"
              rel="noopener noreferrer"
              className="hover:text-sky-400 transition-colors"
            >
              @{twitterHandle.replace('@', '')}
            </a>
          )}
          {linkedinUrl && (() => {
            const safeHref = sanitizeLinkedInUrl(linkedinUrl)
            return safeHref ? (
              <a
                href={safeHref}
                target="_blank"
                rel="noopener noreferrer"
                className="hover:text-sky-400 transition-colors"
              >
                LinkedIn
              </a>
            ) : (
              <span>LinkedIn (invalid URL)</span>
            )
          })()}
          {hasNotes && (
            <span className="text-slate-500">Has notes</span>
          )}
        </div>
      )}
    </div>
  )
}
