import { X } from 'lucide-react'
import type { Conversation } from '../../api/types'
import ModerationPanel from './ModerationPanel'
import { can } from '../../lib/capabilities'
import AdminActionLogSection from './info/AdminActionLogSection'
import ArchiveHistorySection from './info/ArchiveHistorySection'
import ConversationIdentity from './info/ConversationIdentity'
import GroupSettingsSection from './info/GroupSettingsSection'
import InviteLinksSection from './info/InviteLinksSection'
import MembersSection, { useChatMembers } from './info/MembersSection'
import PeerPersonSection from './info/PeerPersonSection'
import SharedMediaSection from './info/SharedMediaSection'
import TagsSection from './info/TagsSection'

export { ArchiveHistorySection }

/**
 * Side panel of a conversation: identity, tags, history archive, shared
 * media, the peer's CRM record and, per capability, members and admin tools.
 * Each section lives in ./info/.
 */
export default function ChatInfoPanel({ chat, onClose }: { chat: Conversation; onClose: () => void }) {
  // Member lists: groups and channels whose channel can list them (capability 'members')
  const hasMembers = (chat.kind === 'group' || chat.kind === 'channel') && can(chat, 'members')
  const { members, setMembers, loadingMembers, membersError } = useChatMembers(chat.id, hasMembers)
  const peerPersonId = chat.kind === 'direct' ? chat.peer?.person_id ?? null : null
  const isAdmin = can(chat, 'admin_moderation')

  return (
    <div className="flex w-72 flex-shrink-0 flex-col border-l border-slate-700/50 bg-slate-800 overflow-y-auto">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-slate-700/50 px-4 py-3">
        <span className="text-xs font-medium text-slate-400 uppercase tracking-wider">Conversation Info</span>
        <button onClick={onClose} className="rounded p-1 text-slate-500 hover:text-slate-300 transition-colors">
          <X size={14} />
        </button>
      </div>

      {/* Identity */}
      <ConversationIdentity chat={chat} />

      {/* Tags */}
      <TagsSection chat={chat} />

      {/* Full-history archive: groups/channels on demand, DMs until complete */}
      {can(chat, 'history_backfill') && (chat.kind !== 'direct' || !chat.history_complete) && (
        <ArchiveHistorySection key={chat.id} chat={chat} />
      )}

      {/* Shared media section */}
      <SharedMediaSection chat={chat} />

      {/* The peer's CRM record (direct conversations) */}
      {peerPersonId != null && (
        <PeerPersonSection key={peerPersonId} personId={peerPersonId} />
      )}

      {/* Moderation controls (capability 'admin_moderation') */}
      {isAdmin && chat.kind !== 'direct' && (
        <ModerationPanel key={chat.id} chat={chat} />
      )}

      {/* Members section (groups/channels) */}
      {hasMembers && (
        <MembersSection chat={chat} members={members} loadingMembers={loadingMembers} membersError={membersError} onMembersChange={setMembers} />
      )}

      {/* Admin sections (capability 'admin_moderation') */}
      {isAdmin && chat.kind !== 'direct' && (
        <>
          <InviteLinksSection chat={chat} />
          <GroupSettingsSection chat={chat} />
          <AdminActionLogSection chat={chat} />
        </>
      )}
    </div>
  )
}
