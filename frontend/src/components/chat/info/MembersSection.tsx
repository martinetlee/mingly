import { useCallback, useEffect, useState } from 'react'
import { Users, Crown, Shield, Bot } from 'lucide-react'
import type { ChatMember, Conversation } from '../../../api/types'
import { getConversationMembers } from '../../../api/client'
import { can } from '../../../lib/capabilities'
import { reportError } from '../../../lib/toast'
import PersonCard from '../../people/PersonCard'
import MemberActionMenu from './MemberActionMenu'

/** The member list of a group / channel (loaded when ``enabled``) */
export function useChatMembers(conversationId: number, enabled: boolean) {
  const [members, setMembers] = useState<ChatMember[]>([])
  const [loadingMembers, setLoadingMembers] = useState(false)
  const [membersError, setMembersError] = useState<string | null>(null)

  useEffect(() => {
    if (enabled) {
      setLoadingMembers(true)
      setMembersError(null)
      getConversationMembers(conversationId)
        .then(setMembers)
        .catch((e) => { setMembersError(e.message); setMembers([]) })
        .finally(() => setLoadingMembers(false))
    } else {
      setMembers([])
    }
  }, [conversationId, enabled])

  return { members, setMembers, loadingMembers, membersError }
}

function MemberBadges({ member: m }: { member: ChatMember }) {
  return (
    <div className="flex items-center gap-1">
      {m.is_bot && <span title="Bot"><Bot size={10} className="text-violet-400" /></span>}
      {m.role === 'creator' && <span title="Creator"><Crown size={10} className="text-amber-400" /></span>}
      {m.role === 'admin' && <span title="Admin"><Shield size={10} className="text-sky-400" /></span>}
    </div>
  )
}

export default function MembersSection({ chat, members, loadingMembers, membersError, onMembersChange }: {
  chat: Conversation
  members: ChatMember[]
  loadingMembers: boolean
  membersError: string | null
  onMembersChange: (m: ChatMember[]) => void
}) {
  const refreshMembers = useCallback(() => {
    getConversationMembers(chat.id).then(onMembersChange).catch((e) => reportError(e, 'Couldn’t refresh members'))
  }, [chat.id, onMembersChange])

  return (
    <div className="px-4 py-4 border-t border-slate-700/50 flex flex-col gap-2">
      <div className="flex items-center gap-1.5 text-xs font-medium text-slate-400 uppercase tracking-wider">
        <Users size={11} />
        Members{members.length > 0 && ` (${members.length})`}
      </div>
      {loadingMembers ? (
        <p className="text-xs text-slate-500">Loading members...</p>
      ) : membersError ? (
        <p className="text-xs text-slate-500">{membersError}</p>
      ) : members.length === 0 ? (
        <p className="text-xs text-slate-500">No members found</p>
      ) : (
        <div className="flex flex-col gap-0.5 max-h-64 overflow-y-auto">
          {members.map((m) => (
            <div key={m.identity_id} className="flex items-center gap-1">
              <div className="flex-1 min-w-0">
                {m.person_id != null ? (
                  <PersonCard
                    personId={m.person_id}
                    name={m.name}
                    username={m.username}
                    showStatus={false}
                    extraBadge={<MemberBadges member={m} />}
                  />
                ) : (
                  <div className="flex items-center gap-2 px-2 py-1.5 text-xs text-gray-200">
                    <span className="truncate">{m.name}</span>
                    <MemberBadges member={m} />
                  </div>
                )}
              </div>
              {!m.is_bot && can(chat, 'admin_moderation') && (
                <MemberActionMenu chat={chat} member={m} onAction={refreshMembers} />
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
