import { useCallback, useRef } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { ArrowLeft, Globe, Phone, AtSign, MessageSquare, Star, UserPlus, CloudOff, RefreshCw, Loader2 } from 'lucide-react'
import type { Contact, ContactPosition, PersonDetail } from '../api/types'
import { getProfile, getPerson, setContactImportantByTelegram, createContact, ApiError } from '../api/client'
import { useLatestQuery } from '../hooks/useLatestQuery'
import { useAsyncAction } from '../hooks/useAsyncAction'
import { useWsEvent } from '../hooks/useWebSocket'
import { useCoalescedRefetch } from '../hooks/useCoalescedRefetch'
import { invalidatePerson } from '../hooks/usePersonSummary'
import { errorMessage } from '../lib/toast'
import { fmtRelative } from '../utils/time'
import ContactInfoSection from '../components/ContactInfoSection'
import PositionsEditor from '../components/PositionsEditor'
import PersonCard from '../components/PersonCard'
import PersonNotes from '../components/PersonNotes'

/**
 * Two independent sources:
 * - local data (contact, notes, positions) from /api/people/{id} — works offline
 * - the Telegram profile from /api/profiles/{id} — may be stale or unavailable
 * A Telegram failure never hides the local data.
 */
export default function Profile() {
  const { userId = '' } = useParams<{ userId: string }>()
  const navigate = useNavigate()
  const isUsername = userId.startsWith('@')

  const profileQ = useLatestQuery(
    ['profile', userId],
    (signal) => getProfile(isUsername ? userId : Number(userId), signal),
    { enabled: userId !== '' },
  )
  const profile = profileQ.data

  // A @username resolves to a numeric id only through Telegram
  const numericId = isUsername ? profile?.id : Number(userId)
  const hasId = numericId != null && Number.isFinite(numericId) && numericId !== 0
  const personQ = useLatestQuery(
    ['person', numericId],
    (signal) => getPerson(numericId as number, signal),
    { enabled: hasId },
  )
  const person = personQ.data
  const contact = person?.contact ?? null
  const positions = person?.positions ?? []

  const idRef = useRef(numericId)
  idRef.current = numericId

  // An agent (MCP) changed this person. The editors refresh themselves in
  // place (keeping open edits; ContactInfoSection patches the contact through
  // setContact); without a contact, reload the local data here — the agent
  // may have created one or changed the header's position.
  const refetchPerson = useCoalescedRefetch(() => personQ.refetch(), 1000)
  useWsEvent('person_changed', (e) => {
    if (e.telegram_id !== idRef.current) return
    invalidatePerson(e.telegram_id)
    if (!personQ.data?.contact) refetchPerson()
  })
  useWsEvent('resync', () => { if (hasId) void personQ.refetch() })

  /** Patch the local person — only if it's still the one on screen */
  const patchPerson = useCallback((tid: number, fn: (p: PersonDetail) => PersonDetail) => {
    if (idRef.current !== tid) return
    personQ.setData((prev) => (prev ? fn(prev) : prev))
  }, [personQ])

  const setContact = (updated: Contact) => {
    invalidatePerson(updated.telegram_id)
    patchPerson(updated.telegram_id, (p) => ({ ...p, contact: updated, positions: updated.positions ?? p.positions }))
  }
  const setPositions = (tid: number, next: ContactPosition[]) =>
    patchPerson(tid, (p) => ({ ...p, positions: next, contact: p.contact ? { ...p.contact, positions: next } : null }))

  const toggleImportant = useAsyncAction(async (tid: number, value: boolean) => {
    setContact(await setContactImportantByTelegram(tid, value))
  }, { errorContext: 'Couldn\'t update important' })

  const addContact = useAsyncAction(async (tid: number) => {
    setContact(await createContact({
      telegram_id: tid,
      first_name: profile?.first_name || undefined,
      last_name: profile?.last_name || undefined,
      username: profile?.username || undefined,
    }))
  }, { errorContext: 'Couldn\'t add contact' })

  const refreshProfile = useAsyncAction(async () => {
    const key = userId
    const fresh = await getProfile(isUsername ? userId : Number(userId), undefined, { refresh: true })
    if (key === userId) profileQ.setData(fresh)
  }, { errorContext: 'Telegram profile' })

  const profileError = profileQ.error
  const profileMissing = profileError instanceof ApiError && profileError.status === 404

  if ((profileQ.loading && (!hasId || personQ.loading)) || (hasId && personQ.loading && !profile)) {
    return (
      <div className="flex flex-1 items-center justify-center text-slate-500 text-sm">
        Loading profile...
      </div>
    )
  }

  const hasLocal = !!person && (person.contact != null || person.positions.length > 0)
  if (!profile && !hasLocal && (profileMissing || !hasId || personQ.error)) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 text-slate-500">
        <p className="text-sm">
          {profileMissing ? 'Profile not found' : errorMessage(profileError ?? personQ.error ?? 'Profile not found')}
        </p>
        <button
          onClick={() => navigate(-1)}
          className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
        >
          Go back
        </button>
      </div>
    )
  }

  const tid = (profile?.id ?? numericId) as number
  const displayName = profile
    ? profile.type === 'user'
      ? [profile.first_name, profile.last_name].filter(Boolean).join(' ') || `User ${profile.id}`
      : profile.title || `Entity ${profile.id}`
    : person?.name ?? String(tid)
  // Without Telegram we can't know the entity type; local data means a person
  const isUser = profile ? profile.type === 'user' : true

  const bioText = profile?.bio || profile?.about
  const isImportant = contact?.is_important ?? false
  const activePosition = positions.find((p) => p.is_active !== false) ?? null

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

        {/* Telegram availability */}
        {(!profile && profileError != null && !profileMissing) && (
          <div className="mb-4 flex items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
            <CloudOff size={13} className="flex-shrink-0" />
            <span className="flex-1">Telegram offline — showing local data only.</span>
            <button
              onClick={() => profileQ.refetch()}
              disabled={profileQ.fetching}
              className="flex items-center gap-1 text-amber-200 hover:text-amber-100 disabled:opacity-50"
            >
              <RefreshCw size={11} className={profileQ.fetching ? 'animate-spin' : ''} /> Retry
            </button>
          </div>
        )}
        {profile?.stale && (
          <div className="mb-4 flex items-center gap-2 rounded-lg border border-slate-600 bg-slate-800 px-3 py-2 text-xs text-slate-400">
            <CloudOff size={13} className="flex-shrink-0 text-amber-400" />
            <span className="flex-1">
              Stale — Telegram is unreachable; profile cached {profile.fetched_at ? fmtRelative(profile.fetched_at * 1000) : 'earlier'}.
            </span>
            <button
              onClick={() => refreshProfile.run()}
              disabled={refreshProfile.pending}
              className="flex items-center gap-1 text-slate-300 hover:text-slate-100 disabled:opacity-50"
            >
              <RefreshCw size={11} className={refreshProfile.pending ? 'animate-spin' : ''} /> Refresh
            </button>
          </div>
        )}

        {/* Profile header -- unified PersonCard */}
        <PersonCard
          telegramId={tid}
          name={displayName}
          username={profile ? profile.username : contact?.username ?? null}
          photoUrl={profile?.photo_url}
          isVerified={profile?.is_verified}
          isPremium={profile?.is_premium}
          isBot={profile?.is_bot}
          onlineStatus={profile?.status}
          bio={bioText}
          isImportant={isImportant}
          status={contact?.relationship_status ?? null}
          position={activePosition ? { title: activePosition.title, organization: activePosition.organization } : null}
          lastInteraction={contact?.last_interaction_at ?? null}
          phone={contact?.phone ?? profile?.phone ?? null}
          twitterHandle={contact?.twitter_handle ?? null}
          linkedinUrl={contact?.linkedin_url ?? null}
          hasNotes={!!contact?.notes}
          mode="profile"
          disableLink
          showStatus={!!contact}
          actions={
            isUser ? (
              <button
                onClick={() => toggleImportant.run(tid, !isImportant)}
                disabled={toggleImportant.pending}
                title={isImportant ? 'Remove from important' : 'Mark as important'}
                className={`rounded-full p-1 transition-colors disabled:opacity-60 ${
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
            {profile && <DetailRow icon={<Globe size={14} />} label="Type" value={profile.type} />}
            <DetailRow icon={<AtSign size={14} />} label="ID" value={String(tid)} />
            {profile?.phone && (
              <DetailRow icon={<Phone size={14} />} label="Phone" value={`+${profile.phone}`} />
            )}
            {profile?.username && (
              <DetailRow icon={<AtSign size={14} />} label="Username" value={`@${profile.username}`} />
            )}
          </div>
        </div>

        {/* Non-user entity: members count */}
        {profile && profile.type !== 'user' && profile.members_count != null && (
          <div className="mb-6 rounded-xl bg-slate-800 border border-slate-700/50 p-4">
            <p className="text-xs text-slate-400">{profile.members_count.toLocaleString()} members</p>
          </div>
        )}

        {/* Local data: contact info + positions */}
        {personQ.error != null && !person && (
          <p className="mb-6 text-xs text-rose-400">Couldn't load local data: {errorMessage(personQ.error)}</p>
        )}
        {contact ? (
          <div className="mb-6 flex flex-col gap-4">
            <ContactInfoSection contact={contact} onContactChange={setContact} />
          </div>
        ) : isUser && person && (
          <div className="mb-6 flex flex-col gap-4">
            {/* Positions work for every user, contact or not */}
            <PositionsEditor
              key={tid}
              telegramId={tid}
              initialPositions={positions}
              onChange={(next) => setPositions(tid, next)}
            />
            <button
              onClick={() => addContact.run(tid)}
              disabled={addContact.pending}
              className="flex items-center gap-2 rounded-lg border border-dashed border-slate-600 px-4 py-3 text-xs text-slate-400 hover:border-slate-500 hover:text-slate-300 transition-colors w-full justify-center disabled:opacity-60"
            >
              {addContact.pending ? <Loader2 size={14} className="animate-spin" /> : <UserPlus size={14} />}
              Add to contacts to manage status, socials & more
            </button>
          </div>
        )}

        {/* Notes timeline (agent + user), for any person */}
        {isUser && Number.isFinite(tid) && tid !== 0 && <PersonNotes telegramId={tid} />}

        {/* Common chats */}
        {profile?.common_chats && profile.common_chats.length > 0 && (
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

