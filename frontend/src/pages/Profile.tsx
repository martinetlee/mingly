import { useEffect, useState } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { ArrowLeft, Globe, Phone, AtSign, MessageSquare, Star, UserPlus, Building2, Plus, X, UserMinus, RotateCcw } from 'lucide-react'
import type { TelegramProfile, Contact, ContactPosition } from '../api/types'
import { getProfile, getContactByTelegram, setContactImportantByTelegram, createContact, getPositions, addPosition, updatePosition, removePosition, isAbortError } from '../api/client'
import ContactInfoSection from '../components/ContactInfoSection'
import PersonCard from '../components/PersonCard'
import ComboInput from '../components/ComboInput'

export default function Profile() {
  const { userId } = useParams<{ userId: string }>()
  const navigate = useNavigate()
  const [profile, setProfile] = useState<TelegramProfile | null>(null)
  const [contact, setContact] = useState<Contact | null>(null)
  const [positions, setPositions] = useState<ContactPosition[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [togglingImportant, setTogglingImportant] = useState(false)

  useEffect(() => {
    if (!userId) return
    setLoading(true)
    setError(null)
    // Abort on profile change so a slow response for the previous userId
    // can't overwrite (or mismatch with) the current profile's data
    const ac = new AbortController()
    // Support both numeric IDs and @username lookups
    const isUsername = userId.startsWith('@')
    const profileKey = isUsername ? userId : Number(userId)
    const contactKey = isUsername ? NaN : Number(userId)
    Promise.all([
      getProfile(profileKey, ac.signal),
      isUsername ? Promise.resolve(null) : getContactByTelegram(contactKey, ac.signal).catch((e) => {
        if (isAbortError(e)) throw e
        return null
      }),
    ])
      .then(([p, c]) => {
        setProfile(p)
        setContact(c)
        // Load positions by telegram_id (works even without a contact record)
        if (p?.id) {
          getPositions(p.id, ac.signal).then(setPositions).catch((e) => {
            if (!isAbortError(e)) setPositions([])
          })
        }
        // If resolved via username, fetch contact info using the returned numeric ID
        if (isUsername && p?.id) {
          getContactByTelegram(p.id, ac.signal).then(setContact).catch(() => {})
        }
        setLoading(false)
      })
      .catch((e) => {
        if (isAbortError(e)) return
        setError(e.message)
        setLoading(false)
      })
    return () => ac.abort()
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
  const activePosition = positions.filter((p) => p.is_active !== false)?.[0] ?? null

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

        {/* Contact extra info (twitter, linkedin, notes) + Positions */}
        {contact ? (
          <div className="mb-6 flex flex-col gap-4">
            <ContactInfoSection contact={contact} onContactChange={(updated) => {
              setContact(updated)
              // Sync positions from the updated contact
              if (updated.positions) setPositions(updated.positions)
            }} />
          </div>
        ) : profile.type === 'user' && (
          <div className="mb-6 flex flex-col gap-4">
            {/* Positions section — available for all users, even non-contacts */}
            <PositionsSection
              telegramId={profile.id}
              positions={positions}
              onPositionsChange={setPositions}
            />
            <button
              onClick={async () => {
                try {
                  const c = await createContact({
                    telegram_id: profile.id,
                    first_name: profile.first_name || undefined,
                    last_name: profile.last_name || undefined,
                    username: profile.username || undefined,
                  })
                  setContact(c)
                } catch { /* ignore */ }
              }}
              className="flex items-center gap-2 rounded-lg border border-dashed border-slate-600 px-4 py-3 text-xs text-slate-400 hover:border-slate-500 hover:text-slate-300 transition-colors w-full justify-center"
            >
              <UserPlus size={14} />
              Add to contacts to manage notes, socials & more
            </button>
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

// ---------------------------------------------------------------------------
// Standalone Positions Section (for non-contact profiles)
// ---------------------------------------------------------------------------

function PositionsSection({
  telegramId,
  positions,
  onPositionsChange,
}: {
  telegramId: number
  positions: ContactPosition[]
  onPositionsChange: (positions: ContactPosition[]) => void
}) {
  const [showAddPos, setShowAddPos] = useState(false)
  const [newOrg, setNewOrg] = useState('')
  const [newTitle, setNewTitle] = useState('')
  const [newIsFormer, setNewIsFormer] = useState(false)
  const [newEndedAt, setNewEndedAt] = useState('')
  const [formerDrafts, setFormerDrafts] = useState<Record<number, string>>({})
  const [confirmingFormer, setConfirmingFormer] = useState<Set<number>>(new Set())

  const handleAddPosition = async () => {
    if (!newOrg.trim()) return
    const pos = await addPosition(
      telegramId,
      newOrg.trim(),
      newTitle.trim() || undefined,
      !newIsFormer,
      newIsFormer ? newEndedAt || undefined : undefined,
    )
    onPositionsChange([...positions, pos])
    setNewOrg('')
    setNewTitle('')
    setNewIsFormer(false)
    setNewEndedAt('')
    setShowAddPos(false)
  }

  const handleRemovePosition = async (posId: number) => {
    await removePosition(telegramId, posId)
    onPositionsChange(positions.filter((p) => p.id !== posId))
  }

  const handleMarkFormer = async (posId: number, endedAt: string) => {
    const updatedPos = await updatePosition(telegramId, posId, {
      is_active: false,
      ended_at: endedAt || null,
    })
    onPositionsChange(positions.map((p) => p.id === posId ? updatedPos : p))
  }

  const handleReactivate = async (posId: number) => {
    const updatedPos = await updatePosition(telegramId, posId, {
      is_active: true,
      ended_at: null,
    })
    onPositionsChange(positions.map((p) => p.id === posId ? updatedPos : p))
  }

  return (
    <div className="rounded-xl bg-slate-800 border border-slate-700/50 p-4 flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-medium uppercase tracking-wider text-slate-500 flex items-center gap-1.5">
          <Building2 size={11} />
          Positions
        </h3>
        <button
          onClick={() => setShowAddPos((v) => !v)}
          className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors flex items-center gap-1"
        >
          <Plus size={11} />
          Add
        </button>
      </div>

      {positions.length === 0 && !showAddPos && (
        <p className="text-xs text-slate-500">No positions added</p>
      )}

      {positions.map((p) => {
        const isFormer = p.is_active === false
        const isConfirming = confirmingFormer.has(p.id)
        return (
          <div key={p.id} className="flex flex-col gap-1">
            <div className={`flex items-center gap-2 group ${isFormer ? 'opacity-60' : ''}`}>
              <Building2 size={12} className={`flex-shrink-0 ${isFormer ? 'text-slate-600' : 'text-slate-500'}`} />
              <div className="flex-1 min-w-0">
                <p className={`text-xs truncate ${isFormer ? 'line-through text-slate-500' : 'text-gray-200'}`}>
                  {p.title ? <><span className={isFormer ? 'text-slate-600' : 'text-slate-400'}>{p.title}</span> @ </> : ''}
                  {p.organization}
                </p>
                {isFormer && p.ended_at && (
                  <p className="text-[10px] text-slate-600">
                    left {new Date(p.ended_at).toLocaleDateString()}
                  </p>
                )}
                {isFormer && !p.ended_at && (
                  <p className="text-[10px] text-slate-600 italic">former</p>
                )}
              </div>
              <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                {isFormer && (
                  <button
                    onClick={() => handleReactivate(p.id)}
                    title="Reactivate"
                    className="text-slate-500 hover:text-emerald-400 transition-colors p-0.5"
                  >
                    <RotateCcw size={10} />
                  </button>
                )}
                {!isFormer && (
                  <button
                    onClick={() => {
                      setConfirmingFormer((prev) => {
                        const next = new Set(prev)
                        if (next.has(p.id)) { next.delete(p.id) } else { next.add(p.id) }
                        return next
                      })
                      setFormerDrafts((prev) => ({ ...prev, [p.id]: '' }))
                    }}
                    title="Mark as former"
                    className="text-slate-500 hover:text-amber-400 transition-colors p-0.5"
                  >
                    <UserMinus size={10} />
                  </button>
                )}
                <button
                  onClick={() => handleRemovePosition(p.id)}
                  title="Remove position"
                  className="text-slate-500 hover:text-rose-400 transition-colors p-0.5"
                >
                  <X size={10} />
                </button>
              </div>
            </div>
            {isConfirming && !isFormer && (
              <div className="ml-5 flex items-center gap-2 rounded bg-slate-700/50 border border-slate-600 px-2 py-1.5">
                <span className="text-[10px] text-slate-400 whitespace-nowrap">End date:</span>
                <input
                  type="date"
                  value={formerDrafts[p.id] || ''}
                  onChange={(e) => setFormerDrafts((prev) => ({ ...prev, [p.id]: e.target.value }))}
                  className="flex-1 bg-transparent text-[10px] text-slate-200 outline-none min-w-0"
                />
                <button
                  onClick={async () => {
                    await handleMarkFormer(p.id, formerDrafts[p.id] || '')
                    setConfirmingFormer((prev) => { const next = new Set(prev); next.delete(p.id); return next })
                  }}
                  className="text-[10px] text-amber-400 hover:text-amber-300 transition-colors whitespace-nowrap font-medium"
                >
                  Mark former
                </button>
                <button
                  onClick={() => setConfirmingFormer((prev) => { const next = new Set(prev); next.delete(p.id); return next })}
                  className="text-[10px] text-slate-500 hover:text-slate-300 transition-colors"
                >
                  <X size={9} />
                </button>
              </div>
            )}
          </div>
        )
      })}

      {showAddPos && (
        <div className="flex flex-col gap-2 rounded-lg border border-slate-600 bg-slate-700/50 p-3">
          <ComboInput
            value={newOrg}
            onChange={setNewOrg}
            placeholder="Organization name..."
            autoFocus
          />
          <input
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') handleAddPosition() }}
            placeholder="Title (optional)..."
            className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1 text-xs text-slate-200 outline-none focus:border-indigo-500"
          />
          <label className="flex items-center gap-2 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={newIsFormer}
              onChange={(e) => {
                setNewIsFormer(e.target.checked)
                if (!e.target.checked) setNewEndedAt('')
              }}
              className="rounded border-slate-500 bg-slate-700 text-amber-500 focus:ring-amber-500/30"
            />
            <span className="text-xs text-slate-400">Former position</span>
          </label>
          {newIsFormer && (
            <div className="flex items-center gap-2">
              <span className="text-[10px] text-slate-500 whitespace-nowrap">End date (optional):</span>
              <input
                type="date"
                value={newEndedAt}
                onChange={(e) => setNewEndedAt(e.target.value)}
                className="flex-1 bg-slate-700 border border-slate-600 rounded px-2 py-0.5 text-xs text-slate-200 outline-none focus:border-amber-500 min-w-0"
              />
            </div>
          )}
          <div className="flex gap-2">
            <button
              onClick={handleAddPosition}
              disabled={!newOrg.trim()}
              className={`flex-1 rounded px-2 py-1 text-xs font-medium text-white disabled:opacity-40 transition-colors ${
                newIsFormer ? 'bg-amber-600 hover:bg-amber-500' : 'bg-indigo-500 hover:bg-indigo-600'
              }`}
            >
              {newIsFormer ? 'Add Former Position' : 'Add Position'}
            </button>
            <button
              onClick={() => { setShowAddPos(false); setNewIsFormer(false); setNewEndedAt('') }}
              className="rounded px-2 py-1 text-xs text-slate-400 hover:text-slate-200 transition-colors"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
