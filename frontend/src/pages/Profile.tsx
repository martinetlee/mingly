import { useEffect, useState } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { ArrowLeft, Globe, Phone, AtSign, MessageSquare, Star } from 'lucide-react'
import type { TelegramProfile, Contact } from '../api/types'
import { getProfile, getContactByTelegram, setContactImportantByTelegram } from '../api/client'
import ContactInfoSection from '../components/ContactInfoSection'
import PersonCard from '../components/PersonCard'

export default function Profile() {
  const { userId } = useParams<{ userId: string }>()
  const navigate = useNavigate()
  const [profile, setProfile] = useState<TelegramProfile | null>(null)
  const [contact, setContact] = useState<Contact | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [togglingImportant, setTogglingImportant] = useState(false)

  useEffect(() => {
    if (!userId) return
    setLoading(true)
    setError(null)
    // Support both numeric IDs and @username lookups
    const isUsername = userId.startsWith('@')
    const profileKey = isUsername ? userId : Number(userId)
    const contactKey = isUsername ? NaN : Number(userId)
    Promise.all([
      getProfile(profileKey),
      isUsername ? Promise.resolve(null) : getContactByTelegram(contactKey).catch(() => null),
    ])
      .then(([p, c]) => {
        setProfile(p)
        setContact(c)
        // If resolved via username, fetch contact info using the returned numeric ID
        if (isUsername && p?.id) {
          getContactByTelegram(p.id).then(setContact).catch(() => {})
        }
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [userId])

  async function handleToggleImportant() {
    if (!profile || togglingImportant) return
    const newVal = !(contact?.is_important ?? false)
    setTogglingImportant(true)
    try {
      const updated = await setContactImportantByTelegram(profile.id, newVal)
      setContact(updated)
    } catch {
      // ignore
    } finally {
      setTogglingImportant(false)
    }
  }

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
  const isImportant = contact?.is_important ?? false
  const activePosition = contact?.positions?.filter((p) => p.is_active !== false)?.[0] ?? null

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

        {/* Profile header -- unified PersonCard */}
        <PersonCard
          telegramId={profile.id}
          name={displayName}
          username={profile.username}
          photoUrl={profile.photo_url}
          isVerified={profile.is_verified}
          isPremium={profile.is_premium}
          isBot={profile.is_bot}
          onlineStatus={profile.status}
          bio={bioText}
          isImportant={isImportant}
          status={contact?.relationship_status}
          position={activePosition ? { title: activePosition.title, organization: activePosition.organization } : null}
          lastInteraction={contact?.last_interaction_at}
          phone={contact?.phone ?? profile.phone}
          twitterHandle={contact?.twitter_handle}
          linkedinUrl={contact?.linkedin_url}
          hasNotes={!!contact?.notes}
          contactId={contact?.id}
          mode="profile"
          disableLink
          showStatus={!!contact}
          actions={
            profile.type === 'user' ? (
              <button
                onClick={handleToggleImportant}
                disabled={togglingImportant}
                title={isImportant ? 'Remove from important' : 'Mark as important'}
                className={`rounded-full p-1 transition-colors ${
                  isImportant
                    ? 'text-amber-400 hover:text-amber-300'
                    : 'text-slate-600 hover:text-amber-400'
                }`}
              >
                <Star
                  size={18}
                  className={isImportant ? 'fill-amber-400' : ''}
                />
              </button>
            ) : undefined
          }
        />

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

        {/* Non-user entity: members count */}
        {profile.type !== 'user' && profile.members_count != null && (
          <div className="mb-6 rounded-xl bg-slate-800 border border-slate-700/50 p-4">
            <p className="text-xs text-slate-400">{profile.members_count.toLocaleString()} members</p>
          </div>
        )}

        {/* Contact extra info (twitter, linkedin, notes, positions) */}
        {contact && (
          <div className="mb-6 flex flex-col gap-4">
            <ContactInfoSection contact={contact} onContactChange={setContact} />
          </div>
        )}

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
