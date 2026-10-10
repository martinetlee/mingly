import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { UserPlus } from 'lucide-react'
import type { Person } from '../../../api/types'
import { getPerson, isAbortError } from '../../../api/client'
import ContactInfoSection from '../../people/ContactInfoSection'
import { RELATIONSHIP_STATUS_COLORS, RELATIONSHIP_STATUS_LABELS, fmtRelative } from '../../people/PersonCard'
import { personUrl } from '../../../lib/routes'

/** CRM details of a direct conversation's peer (every person has a CRM record) */
export default function PeerPersonSection({ personId }: { personId: number }) {
  const [person, setPerson] = useState<Person | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const ac = new AbortController()
    setLoading(true)
    setPerson(null)
    getPerson(personId, ac.signal)
      .then(setPerson)
      .catch((e) => { if (!isAbortError(e)) setPerson(null) })
      .finally(() => { if (!ac.signal.aborted) setLoading(false) })
    return () => ac.abort()
  }, [personId])

  const activePosition = person?.positions?.filter((p) => p.is_active !== false)?.[0] ?? null

  return (
    <div className="px-4 py-4 border-t border-slate-700/50 flex flex-col gap-3">
      <div className="flex items-center gap-1.5 text-xs font-medium text-slate-400 uppercase tracking-wider">
        <UserPlus size={11} />
        Person
      </div>

      {loading ? (
        <p className="text-xs text-slate-500">Loading...</p>
      ) : !person ? (
        <p className="text-xs text-slate-500">Couldn’t load this person</p>
      ) : (
        <>
          {/* Relationship status and recency */}
          {(person.relationship_status || person.last_interaction_at) && (
            <div className="flex items-center gap-2 text-xs">
              {person.relationship_status && (
                <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${RELATIONSHIP_STATUS_COLORS[person.relationship_status] ?? ''}`}>
                  {RELATIONSHIP_STATUS_LABELS[person.relationship_status] ?? person.relationship_status}
                </span>
              )}
              {person.last_interaction_at && (
                <span className="text-slate-500">Last: {fmtRelative(person.last_interaction_at)}</span>
              )}
            </div>
          )}

          {/* Position */}
          {activePosition && (
            <p className="text-xs text-slate-400">
              {activePosition.title ? `${activePosition.title} @ ${activePosition.organization}` : activePosition.organization}
            </p>
          )}

          <Link
            to={personUrl(person.id)}
            className="text-[10px] text-indigo-400 hover:text-indigo-300 transition-colors self-start"
          >
            View full profile
          </Link>
          <ContactInfoSection person={person} onPersonChange={setPerson} compact />
        </>
      )}
    </div>
  )
}
