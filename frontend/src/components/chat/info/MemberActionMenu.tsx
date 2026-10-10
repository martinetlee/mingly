import { useState } from 'react'
import { Settings, ShieldPlus, ShieldMinus, Ban, UserCheck } from 'lucide-react'
import type { AdminPermissions, ChatMember, Conversation } from '../../../api/types'
import { promoteAdmin, demoteAdmin, banMember, approveMember } from '../../../api/client'
import { conversationTitle } from '../../../lib/conversation'
import { reportError } from '../../../lib/toast'
import { useConfirm } from '../../ui'

const DEFAULT_PERMISSIONS: AdminPermissions = {
  change_info: false,
  post_messages: false,
  edit_messages: false,
  delete_messages: false,
  ban_users: false,
  invite_users: false,
  pin_messages: false,
  manage_call: false,
  anonymous: false,
  add_admins: false,
}

/** Per-member admin actions: promote (with permissions), demote, lift restrictions, ban */
export default function MemberActionMenu({ chat, member, onAction }: { chat: Conversation; member: ChatMember; onAction: () => void }) {
  const [open, setOpen] = useState(false)
  const [showPromote, setShowPromote] = useState(false)
  const [perms, setPerms] = useState<AdminPermissions>({ ...DEFAULT_PERMISSIONS })
  const [loading, setLoading] = useState(false)
  const confirm = useConfirm()

  if (member.role === 'creator') return null

  const handlePromote = async () => {
    setLoading(true)
    try {
      await promoteAdmin(chat.id, member.identity_id, perms)
      setShowPromote(false)
      setOpen(false)
      onAction()
    } catch (e) { reportError(e, 'Promote failed') }
    setLoading(false)
  }

  const handleDemote = async () => {
    setLoading(true)
    try {
      await demoteAdmin(chat.id, member.identity_id)
      setOpen(false)
      onAction()
    } catch (e) { reportError(e, 'Demote failed') }
    setLoading(false)
  }

  const handleBan = async () => {
    // Target captured now: the confirm acts on this chat/member even if the panel changes
    const chatId = chat.id
    const memberId = member.identity_id
    setOpen(false)
    const ok = await confirm({
      title: `Ban ${member.name}?`,
      message: `They will be removed from ${conversationTitle(chat)} and can't rejoin.`,
      destructive: true,
      confirmLabel: 'Ban',
      onConfirm: () => banMember(chatId, memberId),
    })
    if (ok) onAction()
  }

  const handleApprove = async () => {
    setLoading(true)
    try {
      await approveMember(chat.id, member.identity_id)
      setOpen(false)
      onAction()
    } catch (e) { reportError(e, 'Remove restrictions failed') }
    setLoading(false)
  }

  const permLabels: { key: keyof AdminPermissions; label: string }[] = [
    { key: 'change_info', label: 'Change info' },
    { key: 'delete_messages', label: 'Delete messages' },
    { key: 'ban_users', label: 'Ban users' },
    { key: 'invite_users', label: 'Invite users' },
    { key: 'pin_messages', label: 'Pin messages' },
    { key: 'manage_call', label: 'Manage calls' },
    { key: 'add_admins', label: 'Add admins' },
  ]

  if (chat.kind === 'channel') {
    permLabels.unshift(
      { key: 'post_messages', label: 'Post messages' },
      { key: 'edit_messages', label: 'Edit messages' },
    )
  }

  return (
    <div className="relative">
      <button
        onClick={() => { setOpen((v) => !v); setShowPromote(false) }}
        className="rounded p-0.5 text-slate-500 hover:text-slate-300 hover:bg-slate-700 transition-colors"
        title="Admin actions"
      >
        <Settings size={11} />
      </button>
      {open && (
        <div className="absolute right-0 top-5 z-30 w-44 rounded-lg border border-slate-600 bg-slate-800 shadow-xl overflow-hidden">
          {!showPromote ? (
            <div className="py-1">
              {member.role === 'member' && (
                <button onClick={() => setShowPromote(true)} disabled={loading} className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-700">
                  <ShieldPlus size={11} className="text-sky-400" /> Promote to admin
                </button>
              )}
              {member.role === 'admin' && (
                <button onClick={handleDemote} disabled={loading} className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-700">
                  <ShieldMinus size={11} className="text-amber-400" /> Demote
                </button>
              )}
              <button onClick={handleApprove} disabled={loading} className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-700">
                <UserCheck size={11} className="text-emerald-400" /> Remove restrictions
              </button>
              <button onClick={handleBan} disabled={loading} className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-red-400 hover:bg-slate-700">
                <Ban size={11} /> Ban
              </button>
            </div>
          ) : (
            <div className="p-2 flex flex-col gap-1.5">
              <p className="text-[10px] font-medium text-slate-400 uppercase">Permissions</p>
              {permLabels.map(({ key, label }) => (
                <label key={key} className="flex items-center gap-1.5 text-[11px] text-slate-300 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={perms[key]}
                    onChange={(e) => setPerms((p) => ({ ...p, [key]: e.target.checked }))}
                    className="rounded border-slate-500 bg-slate-700 text-indigo-500 h-3 w-3"
                  />
                  {label}
                </label>
              ))}
              <div className="flex gap-1.5 mt-1">
                <button onClick={handlePromote} disabled={loading} className="flex-1 rounded bg-sky-600 px-2 py-1 text-[10px] font-medium text-white hover:bg-sky-700 disabled:opacity-40">
                  {loading ? '...' : 'Promote'}
                </button>
                <button onClick={() => setShowPromote(false)} className="rounded px-2 py-1 text-[10px] text-slate-400 hover:text-slate-200">
                  Back
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
