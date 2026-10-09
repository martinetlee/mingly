import { useCallback, useRef } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { ArrowLeft, MessageSquare, Star, CloudOff, RefreshCw, Phone, BookUser } from 'lucide-react'
import type { Identity, Person as PersonT } from '../api/types'
import { getPerson, getTelegramProfile, setPersonImportant, ApiError } from '../api/client'
import { useLatestQuery } from '../hooks/useLatestQuery'
import { useAsyncAction } from '../hooks/useAsyncAction'
import { useWsEvent } from '../hooks/useWebSocket'
import { invalidatePerson } from '../hooks/usePersonSummary'
import { errorMessage } from '../lib/toast'
import { chatUrl } from '../lib/routes'
import { fmtRelative } from '../utils/time'
import ContactInfoSection from '../components/ContactInfoSection'
import PersonCard from '../components/PersonCard'
import PersonNotes from '../components/PersonNotes'
import ChannelIcon, { channelLabel } from '../components/ChannelIcon'

/**
 * A person: the CRM record (local, works offline) plus every identity they
 * have on a channel. Two independent sources:
 * - local data (person, positions, notes) from /api/people/{person_id}
 * - the live Telegram profile of their Telegram identity (Telegram-only) —
 *   may be stale or unavailable; its failure never hides the local data.
 */
export default function Person() {
  const { personId: param = '' } = useParams<{ personId: string }>()
  const navigate = useNavigate()
  const personId = Number(param)
  const validId = Number.isInteger(personId) && personId > 0

  const personQ = useLatestQuery(
    ['person', personId],
    (signal) => getPerson(personId, signal),
    { enabled: validId },
  )
  const person = personQ.data

  // Live profile: only Telegram has one today
  const tgIdentity = person?.identities.find((i) => i.channel === 'telegram') ?? null
  const profileQ = useLatestQuery(
    ['telegram-profile', tgIdentity?.id ?? null],
    (signal) => getTelegramProfile(tgIdentity!.id, signal),
    { enabled: tgIdentity != null },
  )
  const profile = profileQ.data

  const idRef = useRef(personId)
  idRef.current = personId

  // An agent (MCP) changed this person. The editors refresh themselves in
  // place (keeping open edits; ContactInfoSection patches the person through
  // setPerson); refresh the summary cache for every card showing them.
  useWsEvent('person_changed', (e) => {
    if (e.person_id !== idRef.current) return
    invalidatePerson(e.person_id)
  })
  useWsEvent('resync', () => { if (validId) void personQ.refetch() })

  /** Patch the local person — only if it's still the one on screen */
  const setPerson = useCallback((updated: PersonT) => {
    invalidatePerson(updated.id)
    if (idRef.current !== updated.id) return
    personQ.setData((prev) => (prev ? { ...updated, positions: updated.positions ?? prev.positions } : prev))
  }, [personQ])

  const toggleImportant = useAsyncAction(async (pid: number, value: boolean) => {
    setPerson(await setPersonImportant(pid, value))
  }, { errorContext: 'Couldn\'t update important' })

  const refreshProfile = useAsyncAction(async () => {
    const identityId = tgIdentity?.id
    if (identityId == null) return
    const fresh = await getTelegramProfile(identityId, undefined, { refresh: true })
    if (identityId === tgIdentity?.id) profileQ.setData(fresh)
  }, { errorContext: 'Telegram profile' })

  if (validId && personQ.loading) {
    return (
      <div className="flex flex-1 items-center justify-center text-slate-500 text-sm">
        Loading profile...
      </div>
    )
  }

  if (!validId || !person) {
    const notFound = !validId || (personQ.error instanceof ApiError && personQ.error.status === 404)
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 text-slate-500">
        <p className="text-sm">
          {notFound ? 'Person not found' : errorMessage(personQ.error ?? 'Couldn\'t load this person')}
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

  const profileError = profileQ.error
  const profileMissing = profileError instanceof ApiError && profileError.status === 404
  const isImportant = person.is_important
  const positions = person.positions ?? []
  const activePosition = positions.find((p) => p.is_active !== false) ?? null
  const username = profile?.username ?? person.identities.find((i) => i.username)?.username ?? null
  const bioText = profile?.bio || person.identities.find((i) => i.bio)?.bio || null

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

        {/* Telegram availability (live Telegram profile only) */}
        {tgIdentity && !profile && profileError != null && !profileMissing && (
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
          personId={person.id}
          name={person.name}
          username={username}
          photoUrl={profile?.avatar_url ?? person.avatar_url ?? null}
          isVerified={profile?.is_verified}
          isPremium={profile?.is_premium}
          isBot={profile?.is_bot ?? person.identities.some((i) => i.is_bot)}
          onlineStatus={profile?.status}
          bio={bioText}
          isImportant={isImportant}
          status={person.relationship_status}
          position={activePosition ? { title: activePosition.title, organization: activePosition.organization } : null}
          lastInteraction={person.last_interaction_at}
          phone={null}
          twitterHandle={person.twitter_handle}
          linkedinUrl={person.linkedin_url}
          hasNotes={!!person.notes}
          mode="profile"
          disableLink
          showStatus
          actions={
            <button
              onClick={() => toggleImportant.run(person.id, !isImportant)}
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
          }
        />

        {/* Identities: one per channel handle */}
        {person.identities.length > 0 && (
          <div className="mb-6 rounded-xl bg-slate-800 border border-slate-700/50 p-4">
            <h3 className="mb-3 text-xs font-medium uppercase tracking-wider text-slate-500">
              Identities ({person.identities.length})
            </h3>
            <div className="flex flex-col gap-2.5">
              {person.identities.map((ident) => <IdentityRow key={ident.id} identity={ident} />)}
            </div>
          </div>
        )}

        {/* Local CRM data: contact info + positions */}
        <div className="mb-6 flex flex-col gap-4">
          <ContactInfoSection person={person} onPersonChange={setPerson} />
        </div>

        {/* Notes timeline (agent + user) */}
        <PersonNotes personId={person.id} />

        {/* Common chats (Telegram) */}
        {profile?.common_conversations && profile.common_conversations.length > 0 && (
          <div className="rounded-xl bg-slate-800 border border-slate-700/50 p-4">
            <h3 className="mb-3 text-xs font-medium uppercase tracking-wider text-slate-500">
              Common chats ({profile.common_conversations.length})
            </h3>
            <div className="flex flex-col gap-1">
              {profile.common_conversations.map((c) => (
                <Link
                  key={c.conversation_id}
                  to={chatUrl(c.conversation_id)}
                  className="flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm text-gray-200 hover:bg-slate-700/50 transition-colors"
                >
                  <MessageSquare size={14} className="text-slate-500" />
                  {c.title || 'Untitled chat'}
                </Link>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function IdentityRow({ identity }: { identity: Identity }) {
  const handle = identity.username ? `@${identity.username}` : identity.channel === 'telegram' ? null : identity.external_id
  return (
    <div className="flex items-center gap-3 text-sm" data-testid="identity">
      <ChannelIcon channel={identity.channel} size={13} />
      <span className="min-w-0 flex-1 truncate text-gray-200">
        {identity.display_name || handle || channelLabel(identity.channel)}
        {handle && identity.display_name && <span className="ml-1.5 text-xs text-slate-500">{handle}</span>}
      </span>
      {identity.phone && (
        <span className="flex items-center gap-1 text-xs text-slate-400" title="Phone">
          <Phone size={11} /> {identity.phone}
        </span>
      )}
      {identity.is_contact && (
        <span title={`In your ${channelLabel(identity.channel)} contacts`} className="text-slate-500">
          <BookUser size={12} />
        </span>
      )}
    </div>
  )
}
