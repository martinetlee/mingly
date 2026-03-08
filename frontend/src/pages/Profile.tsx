import { useEffect, useState } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { ArrowLeft, Bot, Crown, BadgeCheck, Globe, Phone, AtSign, MessageSquare } from 'lucide-react'
import type { TelegramProfile } from '../api/types'
import { getProfile } from '../api/client'

const STATUS_LABELS: Record<string, string> = {
  online: 'Online',
  recently: 'Last seen recently',
  offline: 'Offline',
  last_week: 'Last seen within a week',
  last_month: 'Last seen within a month',
  unknown: 'Last seen a long time ago',
}

const STATUS_COLORS: Record<string, string> = {
  online: 'text-emerald-400',
  recently: 'text-slate-400',
  offline: 'text-slate-500',
}

export default function Profile() {
  const { userId } = useParams<{ userId: string }>()
  const navigate = useNavigate()
  const [profile, setProfile] = useState<TelegramProfile | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!userId) return
    setLoading(true)
    setError(null)
    getProfile(Number(userId))
      .then(setProfile)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [userId])

  if (loading) {
    return (
      <div className="flex flex-1 items-center justify-center text-slate-500 text-sm">
        Loading profile...
      </div>
    )
  }

  if (error || !profile) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 text-slate-500">
        <p className="text-sm">{error || 'Profile not found'}</p>
        <button
          onClick={() => navigate(-1)}
          className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
        >
          Go back
        </button>
      </div>
    )
  }

  const displayName = profile.type === 'user'
    ? [profile.first_name, profile.last_name].filter(Boolean).join(' ') || `User ${profile.id}`
    : profile.title || `Entity ${profile.id}`

  const bioText = profile.bio || profile.about

  return (
    <div className="flex flex-1 overflow-hidden">
      <div className="mx-auto w-full max-w-2xl overflow-y-auto px-6 py-8">
        {/* Back button */}
        <button
          onClick={() => navigate(-1)}
          className="mb-6 flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200 transition-colors"
        >
          <ArrowLeft size={14} />
          Back
        </button>

        {/* Profile header */}
        <div className="flex items-start gap-5 mb-8">
          {profile.photo_url ? (
            <img
              src={profile.photo_url}
              alt={displayName}
              className="h-20 w-20 rounded-full object-cover ring-2 ring-slate-700"
            />
          ) : (
            <div className="flex h-20 w-20 items-center justify-center rounded-full bg-slate-700 text-2xl font-semibold text-slate-300 ring-2 ring-slate-600">
              {displayName.charAt(0).toUpperCase()}
            </div>
          )}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h1 className="text-xl font-bold text-gray-100">{displayName}</h1>
              {profile.is_verified && (
                <BadgeCheck size={18} className="text-sky-400" title="Verified" />
              )}
              {profile.is_premium && (
                <Crown size={16} className="text-amber-400" title="Premium" />
              )}
              {profile.is_bot && (
                <Bot size={16} className="text-violet-400" title="Bot" />
              )}
            </div>
            {profile.username && (
              <p className="mt-0.5 text-sm text-indigo-400">@{profile.username}</p>
            )}
            {profile.status && (
              <p className={`mt-1 text-xs ${STATUS_COLORS[profile.status] ?? 'text-slate-500'}`}>
                {STATUS_LABELS[profile.status] ?? profile.status}
              </p>
            )}
            {profile.type !== 'user' && profile.members_count != null && (
              <p className="mt-1 text-xs text-slate-400">{profile.members_count.toLocaleString()} members</p>
            )}
          </div>
        </div>

        {/* Bio / About */}
        {bioText && (
          <div className="mb-6 rounded-xl bg-slate-800 border border-slate-700/50 p-4">
            <h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-slate-500">
              {profile.type === 'user' ? 'Bio' : 'About'}
            </h3>
            <p className="text-sm text-gray-200 whitespace-pre-wrap leading-relaxed">{bioText}</p>
          </div>
        )}

        {/* Details */}
        <div className="mb-6 rounded-xl bg-slate-800 border border-slate-700/50 p-4">
          <h3 className="mb-3 text-xs font-medium uppercase tracking-wider text-slate-500">Details</h3>
          <div className="flex flex-col gap-2.5">
            <DetailRow icon={<Globe size={14} />} label="Type" value={profile.type} />
            <DetailRow icon={<AtSign size={14} />} label="ID" value={String(profile.id)} />
            {profile.phone && (
              <DetailRow icon={<Phone size={14} />} label="Phone" value={`+${profile.phone}`} />
            )}
            {profile.username && (
              <DetailRow icon={<AtSign size={14} />} label="Username" value={`@${profile.username}`} />
            )}
          </div>
        </div>

        {/* Common chats */}
        {profile.common_chats && profile.common_chats.length > 0 && (
          <div className="rounded-xl bg-slate-800 border border-slate-700/50 p-4">
            <h3 className="mb-3 text-xs font-medium uppercase tracking-wider text-slate-500">
              Common chats ({profile.common_chats.length})
            </h3>
            <div className="flex flex-col gap-1">
              {profile.common_chats.map((chat) => (
                <Link
                  key={chat.id}
                  to={`/profile/${chat.id}`}
                  className="flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm text-gray-200 hover:bg-slate-700/50 transition-colors"
                >
                  <MessageSquare size={14} className="text-slate-500" />
                  {chat.title}
                </Link>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function DetailRow({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return (
    <div className="flex items-center gap-3 text-sm">
      <span className="text-slate-500">{icon}</span>
      <span className="w-20 text-slate-500">{label}</span>
      <span className="text-gray-200">{value}</span>
    </div>
  )
}
