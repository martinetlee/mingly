import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import {
  ArrowLeft, MessageSquare, Star, CloudOff, RefreshCw, Phone, BookUser, GitMerge, Unlink, Undo2, X,
} from 'lucide-react'
import type { Identity, Person as PersonT } from '../api/types'
import { getPerson, getTelegramProfile, setPersonImportant, ApiError } from '../api/client'
import {
  detachIdentity, dismissMergeSuggestion, getPersonMergeSuggestions, getPersonMerges, mergePerson,
  suggestionOther, suggestionReasonLabel, undoPersonMerge, type MergeResult, type MergeSuggestion,
  type PersonMergeRecord,
} from '../api/peopleLinks'
import { useLatestQuery } from '../hooks/useLatestQuery'
import { useAsyncAction } from '../hooks/useAsyncAction'
import { useWsEvent } from '../hooks/useWebSocket'
import { useCoalescedRefetch } from '../hooks/useCoalescedRefetch'
import { invalidatePerson } from '../hooks/usePersonSummary'
import { errorMessage, toast } from '../lib/toast'
import { chatUrl, personUrl } from '../lib/routes'
import { fmtRelative } from '../utils/time'
import ContactInfoSection from '../components/ContactInfoSection'
import PersonCard from '../components/PersonCard'
import PersonNotes from '../components/PersonNotes'
import PersonTimeline from '../components/PersonTimeline'
import UpcomingMeetings from './calendar/UpcomingMeetings'
import ChannelIcon, { channelLabel } from '../components/ChannelIcon'
import MergePersonDialog from '../components/MergePersonDialog'
import { useConfirm } from '../components/ui'

/** Coalescing window for person_changed refreshes */
export const PERSON_REFRESH_MS = 1000

/**
 * The person a removed person was merged into, when the 404 says so
 * (`{"detail", "merged_into": <person id>}`).
 */
export function mergedIntoOf(error: ApiError | null): number | null {
  const v = error?.body?.merged_into
  return typeof v === 'number' && Number.isInteger(v) && v > 0 ? v : null
}

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

  // This person changed elsewhere (an agent over MCP, automatic linking
  // during a sync, a merge from another person's page): refresh the summary
  // cache for every card showing them and re-read them — identities may have
  // moved, or the person may be gone (merged into someone else). Open edits
  // survive: editors adopt outside values only when not editing.
  const refreshPerson = useCoalescedRefetch(() => (validId ? personQ.refetch() : undefined), PERSON_REFRESH_MS)
  useWsEvent('person_changed', (e) => {
    if (e.person_id !== idRef.current) return
    invalidatePerson(e.person_id)
    refreshPerson()
  })
  useWsEvent('resync', () => { if (validId) void personQ.refetch() })

  // Merged into someone else (now, or before this link was followed): go there
  const goneError = personQ.error instanceof ApiError && personQ.error.status === 404 ? personQ.error : null
  const mergedInto = mergedIntoOf(goneError)
  useEffect(() => {
    if (mergedInto != null && mergedInto !== personId) navigate(personUrl(mergedInto), { replace: true })
  }, [mergedInto, personId, navigate])

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

  if (mergedInto != null) {
    return <div className="flex flex-1 items-center justify-center text-slate-500 text-sm">Opening the merged person…</div>
  }

  if (!validId || !person) {
    const notFound = !validId || goneError != null
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

  // Merge / detach / undo change the identity set: the panels reload on it
  const identityKey = person.identities.map((i) => i.id).sort((a, b) => a - b).join(',')
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

        {goneError && (
          <div className="mb-4 flex items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300" data-testid="person-gone">
            <CloudOff size={13} className="flex-shrink-0" />
            <span className="flex-1">This person no longer exists (merged or removed). What you see is the last copy.</span>
          </div>
        )}

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

        {/* Linked accounts (identities) + possible duplicates, merges, undo */}
        <LinkedAccounts person={person} onPersonChange={setPerson} />

        {/* Local CRM data: contact info + positions */}
        <div className="mb-6 flex flex-col gap-4">
          <ContactInfoSection person={person} onPersonChange={setPerson} />
        </div>

        {/* Notes timeline (agent + user) */}
        <PersonNotes personId={person.id} identityKey={identityKey} />

        {/* Next calendar events with this person (hidden when there are none) */}
        <UpcomingMeetings personId={person.id} identityKey={identityKey} />

        {/* Messages across every channel (chats, groups, email) and past meetings */}
        <PersonTimeline personId={person.id} identityKey={identityKey} />

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

/**
 * The person's linked accounts (one identity per channel handle), people who
 * may be the same human (merge / dismiss), "merge with…" search, detaching
 * an account into its own person, and undo of merges into this person.
 */
function LinkedAccounts({ person, onPersonChange }: { person: PersonT; onPersonChange: (p: PersonT) => void }) {
  const confirm = useConfirm()
  const [merging, setMerging] = useState(false)
  const personId = person.id
  const suggestionsQ = useLatestQuery(
    ['person-merge-suggestions', personId],
    (signal) => getPersonMergeSuggestions(personId, signal),
  )
  const mergesQ = useLatestQuery(['person-merges', personId], (signal) => getPersonMerges(personId, signal))
  const suggestions = suggestionsQ.data ?? []
  const merges = mergesQ.data ?? []

  const idRef = useRef(personId)
  idRef.current = personId
  const relatedRef = useRef<Set<number>>(new Set())
  relatedRef.current = new Set(suggestions.map((s) => suggestionOther(s, personId).id))

  const refetchLinks = useCallback(() => {
    void suggestionsQ.refetch()
    void mergesQ.refetch()
  }, [suggestionsQ, mergesQ])
  useWsEvent('person_changed', (e) => {
    if (e.person_id === idRef.current || relatedRef.current.has(e.person_id)) refetchLinks()
  })

  /** Reload this person after a linking change elsewhere in the graph */
  const reloadPerson = async (id: number) => {
    const fresh = await getPerson(id)
    if (idRef.current === id) onPersonChange(fresh)
  }

  const merged = (result: MergeResult, otherId: number, otherName: string) => {
    invalidatePerson(otherId)
    const { merge_id: _mergeId, ...fresh } = result
    if (idRef.current === result.id) onPersonChange(fresh)
    refetchLinks()
    toast.success(`Merged ${otherName} into ${result.name}`)
  }

  const mergeSuggested = (s: MergeSuggestion) => {
    const other = suggestionOther(s, personId)
    void confirm({
      title: `Merge ${other.name} into ${person.name}?`,
      message: `${suggestionReasonLabel(s.reason)}. ${other.name}'s linked accounts, positions and notes move here and ${other.name} is removed. You can undo this.`,
      confirmLabel: 'Merge',
      onConfirm: async () => merged(await mergePerson(personId, other.id), other.id, other.name),
    })
  }

  const dismiss = useAsyncAction(async (s: MergeSuggestion) => {
    await dismissMergeSuggestion(s.id)
    suggestionsQ.setData((prev) => prev?.filter((x) => x.id !== s.id))
  }, { errorContext: 'Couldn\'t dismiss suggestion' })

  const detach = (ident: Identity) => {
    const label = identityLabel(ident)
    void confirm({
      title: `Detach ${label}?`,
      message: `This ${channelLabel(ident.channel)} account becomes a separate person, with its conversations. Notes and positions stay with ${person.name}.`,
      confirmLabel: 'Detach',
      onConfirm: async () => {
        const created = await detachIdentity(personId, ident.id)
        invalidatePerson(created.id)
        await reloadPerson(personId)
        refetchLinks()
        toast.success(`${label} is now a separate person`)
      },
    })
  }

  const undo = (m: PersonMergeRecord) => {
    const name = m.from_name || 'the merged person'
    void confirm({
      title: `Undo merge of ${name}?`,
      message: `${name} becomes a separate person again with the accounts, positions and notes that came with the merge.`,
      confirmLabel: 'Undo merge',
      onConfirm: async () => {
        const restored = await undoPersonMerge(m.id)
        invalidatePerson(restored.id)
        await reloadPerson(personId)
        refetchLinks()
        toast.success(`${restored.name} is a separate person again`)
      },
    })
  }

  const canDetach = person.identities.length > 1
  return (
    <div className="mb-6 rounded-xl bg-slate-800 border border-slate-700/50 p-4" data-testid="linked-accounts">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-xs font-medium uppercase tracking-wider text-slate-500">
          Linked accounts ({person.identities.length})
        </h3>
        {!person.is_self && (
          <button
            onClick={() => setMerging(true)}
            className="flex items-center gap-1 text-xs text-slate-400 hover:text-indigo-300 transition-colors"
          >
            <GitMerge size={12} /> Merge with…
          </button>
        )}
      </div>
      <div className="flex flex-col gap-2.5">
        {person.identities.map((ident) => (
          <IdentityRow
            key={ident.id}
            identity={ident}
            action={canDetach ? (
              <button
                onClick={() => detach(ident)}
                title="Detach into a separate person"
                aria-label={`Detach ${identityLabel(ident)}`}
                className="rounded p-0.5 text-slate-600 hover:text-slate-300 transition-colors"
              >
                <Unlink size={12} />
              </button>
            ) : null}
          />
        ))}
      </div>

      {suggestions.length > 0 && (
        <div className="mt-4 border-t border-slate-700/50 pt-3" data-testid="merge-suggestions">
          <h4 className="mb-2 text-[11px] font-medium uppercase tracking-wider text-slate-500">Possible duplicates</h4>
          <div className="flex flex-col gap-2">
            {suggestions.map((s) => {
              const other = suggestionOther(s, personId)
              return (
                <div key={s.id} className="flex items-center gap-2 text-sm">
                  <span className="flex items-center gap-1">
                    {other.identities.map((i) => <ChannelIcon key={i.id} channel={i.channel} size={11} />)}
                  </span>
                  <span className="min-w-0 flex-1 truncate">
                    <Link to={personUrl(other.id)} className="text-gray-200 hover:text-indigo-300">{other.name}</Link>
                    <span className="ml-1.5 text-xs text-slate-500">{suggestionReasonLabel(s.reason)}</span>
                  </span>
                  <button
                    onClick={() => mergeSuggested(s)}
                    className="rounded px-2 py-0.5 text-xs text-indigo-300 hover:bg-indigo-500/10"
                  >
                    Merge
                  </button>
                  <button
                    onClick={() => dismiss.run(s)}
                    disabled={dismiss.pending}
                    title="Not the same person"
                    aria-label={`Dismiss ${other.name}`}
                    className="rounded p-0.5 text-slate-500 hover:text-slate-300 disabled:opacity-50"
                  >
                    <X size={12} />
                  </button>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {merges.length > 0 && (
        <div className="mt-4 border-t border-slate-700/50 pt-3" data-testid="recent-merges">
          <h4 className="mb-2 text-[11px] font-medium uppercase tracking-wider text-slate-500">Merged here</h4>
          <div className="flex flex-col gap-2">
            {merges.map((m) => (
              <div key={m.id} className="flex items-center gap-2 text-xs text-slate-400">
                <span className="flex items-center gap-1">
                  {m.identities.map((i) => <ChannelIcon key={i.id} channel={i.channel} size={11} />)}
                </span>
                <span className="min-w-0 flex-1 truncate">
                  {m.from_name || 'Someone'}
                  <span className="ml-1.5 text-slate-500">
                    {m.automatic ? 'linked automatically' : 'merged'}
                    {m.created_at ? ` ${fmtRelative(m.created_at)}` : ''}
                  </span>
                </span>
                <button
                  onClick={() => undo(m)}
                  className="flex items-center gap-1 rounded px-2 py-0.5 text-slate-400 hover:bg-slate-700/50 hover:text-slate-200"
                >
                  <Undo2 size={11} /> Undo
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {merging && (
        <MergePersonDialog
          person={person}
          onClose={() => setMerging(false)}
          onMerged={(result, other) => {
            setMerging(false)
            merged(result, other.id, other.name)
          }}
        />
      )}
    </div>
  )
}

function identityLabel(identity: Identity): string {
  return identity.display_name || (identity.username ? `@${identity.username}` : identity.external_id)
}

function IdentityRow({ identity, action }: { identity: Identity; action?: ReactNode }) {
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
      {action}
    </div>
  )
}
