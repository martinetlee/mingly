import { useEffect, useState, useCallback } from 'react'
import { Link } from 'react-router-dom'
import { X, Sparkles, Plus, Tag as TagIcon, Users, Crown, Shield, Bot, UserPlus, Image, FileText, LinkIcon, Download, ExternalLink, ShieldPlus, ShieldMinus, Ban, UserCheck, Link2, Settings, ClipboardList, Copy, Trash2, ChevronDown, ChevronUp, History, CheckCircle2, Loader2 } from 'lucide-react'
import type { Attachment, Conversation, ChatMember, Person, SharedMediaItem, Tag, TagSuggestion, AdminPermissions, InviteLink, GroupSettings, AdminActionLog } from '../../api/types'
import { getTags, getConversationTags, addTagToConversation, removeTagFromConversation, suggestTags, getConversationMembers, getPerson, getSharedMedia, promoteAdmin, demoteAdmin, banMember, approveMember, getInviteLinks, createInviteLink, revokeInviteLink, getGroupSettings, updateGroupSettings, getAdminActionLog, isAbortError, archiveConversation } from '../../api/client'
import { authenticatedMediaUrl } from '../../api/session'
import ContactInfoSection from '../people/ContactInfoSection'
import ModerationPanel from './ModerationPanel'
import ChannelIcon from '../channels/ChannelIcon'
import ConversationAvatar from './ConversationAvatar'
import { can } from '../../lib/capabilities'
import { conversationTitle, kindColor, kindLabel } from '../../lib/conversation'
import { personUrl } from '../../lib/routes'
import { useConfirm } from '../ui'
import { reportError, toast } from '../../lib/toast'
import { useAsyncAction } from '../../hooks/useAsyncAction'
import { fmtDateTime, fmtShortDate } from '../../utils/time'
import { createOrFindTag, tagErrorMessage } from './chatTags'
import PersonCard, { RELATIONSHIP_STATUS_COLORS, RELATIONSHIP_STATUS_LABELS, fmtRelative } from '../people/PersonCard'

const TAG_COLORS: Record<string, string> = {
  indigo:  'bg-indigo-500/20 text-indigo-300 border border-indigo-500/30',
  emerald: 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30',
  rose:    'bg-rose-500/20 text-rose-300 border border-rose-500/30',
  amber:   'bg-amber-500/20 text-amber-300 border border-amber-500/30',
  violet:  'bg-violet-500/20 text-violet-300 border border-violet-500/30',
  sky:     'bg-sky-500/20 text-sky-300 border border-sky-500/30',
  slate:   'bg-slate-500/20 text-slate-300 border border-slate-500/30',
}

const COLOR_OPTIONS = ['indigo', 'emerald', 'rose', 'amber', 'violet', 'sky', 'slate'] as const

const COLOR_DOTS: Record<string, string> = {
  indigo: 'bg-indigo-400', emerald: 'bg-emerald-400', rose: 'bg-rose-400',
  amber: 'bg-amber-400', violet: 'bg-violet-400', sky: 'bg-sky-400', slate: 'bg-slate-400',
}

/** CRM details of a direct conversation's peer (every person has a CRM record) */
function PeerPersonSection({ personId }: { personId: number }) {
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

type SharedMediaTab = 'media' | 'files' | 'links'

function formatFileSize(bytes: number | null): string {
  if (bytes == null) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** The item's shown attachment (shared-media rows are messages with ≥1 attachment) */
function firstAttachment(item: SharedMediaItem): Attachment | null {
  return item.attachments?.[0] ?? null
}

function extractUrls(text: string | null): string[] {
  if (!text) return []
  const matches = text.match(/https?:\/\/[^\s<>"]+/g)
  return matches ?? []
}

function SharedMediaSection({ chat }: { chat: Conversation }) {
  const [activeTab, setActiveTab] = useState<SharedMediaTab>('media')
  const [items, setItems] = useState<SharedMediaItem[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    setLoading(true)
    setItems([])
    // Abort on tab/chat switch so a slower earlier response can't populate
    // the currently active tab with the wrong items
    const ac = new AbortController()
    getSharedMedia(chat.id, activeTab, 50, 0, ac.signal)
      .then((data) => {
        setItems(data)
        setLoading(false)
      })
      .catch((e) => {
        if (isAbortError(e)) return
        setItems([])
        setLoading(false)
      })
    return () => ac.abort()
  }, [chat.id, activeTab])

  const tabs: { key: SharedMediaTab; label: string; icon: React.ReactNode }[] = [
    { key: 'media', label: 'Media', icon: <Image size={11} /> },
    { key: 'files', label: 'Files', icon: <FileText size={11} /> },
    { key: 'links', label: 'Links', icon: <LinkIcon size={11} /> },
  ]

  return (
    <div className="px-4 py-4 border-t border-slate-700/50 flex flex-col gap-3">
      {/* Tab bar */}
      <div className="flex gap-1 rounded-lg bg-slate-700/50 p-0.5">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`flex flex-1 items-center justify-center gap-1 rounded-md px-2 py-1.5 text-[11px] font-medium transition-colors ${
              activeTab === tab.key
                ? 'bg-slate-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            {tab.icon}
            {tab.label}
          </button>
        ))}
      </div>

      {/* Content */}
      {loading ? (
        <p className="text-xs text-slate-500">Loading...</p>
      ) : items.length === 0 ? (
        <p className="text-xs text-slate-500">
          No {activeTab === 'media' ? 'media' : activeTab === 'files' ? 'files' : 'links'} shared yet
        </p>
      ) : activeTab === 'media' ? (
        <div className="grid grid-cols-3 gap-1 max-h-64 overflow-y-auto">
          {items.map((item) => ({ item, media: firstAttachment(item) })).map(({ item, media }) => (
            <div key={item.id} className="relative aspect-square overflow-hidden rounded">
              {media?.url && media.kind !== 'video' ? (
                <img
                  src={authenticatedMediaUrl(media.url)}
                  alt=""
                  className="h-full w-full object-cover hover:opacity-80 transition-opacity cursor-pointer"
                />
              ) : (
                <div className="flex h-full w-full items-center justify-center bg-slate-700 text-slate-500">
                  <Image size={16} />
                </div>
              )}
              {media?.kind === 'gif' && (
                <span className="absolute bottom-0.5 left-0.5 rounded bg-black/60 px-1 text-[8px] text-white">GIF</span>
              )}
              {media?.kind === 'video' && (
                <span className="absolute bottom-0.5 left-0.5 rounded bg-black/60 px-1 text-[8px] text-white">VID</span>
              )}
            </div>
          ))}
        </div>
      ) : activeTab === 'files' ? (
        <div className="flex flex-col gap-1.5 max-h-64 overflow-y-auto">
          {items.map((item) => ({ item, media: firstAttachment(item) })).map(({ item, media }) => (
            <a
              key={item.id}
              href={media?.url ? authenticatedMediaUrl(media.url) : '#'}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-2 rounded-lg bg-slate-700/50 px-2.5 py-2 hover:bg-slate-700 transition-colors"
            >
              <FileText size={14} className="flex-shrink-0 text-slate-400" />
              <div className="min-w-0 flex-1">
                <p className="truncate text-xs text-slate-200">
                  {media?.filename || 'Document'}
                </p>
                <p className="text-[10px] text-slate-500">
                  {formatFileSize(media?.size ?? null)}
                  {item.date && ` \u00b7 ${fmtShortDate(item.date)}`}
                </p>
              </div>
              <Download size={12} className="flex-shrink-0 text-slate-500" />
            </a>
          ))}
        </div>
      ) : (
        <div className="flex flex-col gap-1.5 max-h-64 overflow-y-auto">
          {items.map((item) => {
            const urls = extractUrls(item.text)
            return urls.map((url, urlIdx) => (
              <a
                key={`${item.id}-${urlIdx}`}
                href={url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-2 rounded-lg bg-slate-700/50 px-2.5 py-2 hover:bg-slate-700 transition-colors"
              >
                <ExternalLink size={12} className="flex-shrink-0 text-indigo-400" />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-xs text-indigo-300">{url}</p>
                  {item.sender_name && (
                    <p className="text-[10px] text-slate-500">
                      {item.sender_name}
                      {item.date && ` \u00b7 ${fmtShortDate(item.date)}`}
                    </p>
                  )}
                </div>
              </a>
            ))
          })}
        </div>
      )}
    </div>
  )
}

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

function MemberActionMenu({ chat, member, onAction }: { chat: Conversation; member: ChatMember; onAction: () => void }) {
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

function MemberBadges({ member: m }: { member: ChatMember }) {
  return (
    <div className="flex items-center gap-1">
      {m.is_bot && <span title="Bot"><Bot size={10} className="text-violet-400" /></span>}
      {m.role === 'creator' && <span title="Creator"><Crown size={10} className="text-amber-400" /></span>}
      {m.role === 'admin' && <span title="Admin"><Shield size={10} className="text-sky-400" /></span>}
    </div>
  )
}

function MembersSection({ chat, members, loadingMembers, membersError, onMembersChange }: {
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

function InviteLinksSection({ chat }: { chat: Conversation }) {
  const [expanded, setExpanded] = useState(false)
  const [links, setLinks] = useState<InviteLink[]>([])
  const [loading, setLoading] = useState(false)
  const [creating, setCreating] = useState(false)
  const [showCreate, setShowCreate] = useState(false)
  const [newTitle, setNewTitle] = useState('')
  const [newLimit, setNewLimit] = useState('')

  const loadLinks = useCallback(() => {
    setLoading(true)
    getInviteLinks(chat.id)
      .then(setLinks)
      .catch(() => setLinks([]))
      .finally(() => setLoading(false))
  }, [chat.id])

  useEffect(() => {
    if (expanded) loadLinks()
  }, [expanded, loadLinks])

  const handleCreate = async () => {
    setCreating(true)
    try {
      const link = await createInviteLink(chat.id, {
        title: newTitle || undefined,
        usage_limit: newLimit ? parseInt(newLimit, 10) : undefined,
      })
      setLinks((prev) => [...prev, link])
      setShowCreate(false)
      setNewTitle('')
      setNewLimit('')
    } catch (e) { reportError(e, 'Create invite link failed') }
    setCreating(false)
  }

  const handleRevoke = async (link: string) => {
    try {
      await revokeInviteLink(chat.id, link)
      setLinks((prev) => prev.filter((l) => l.link !== link))
    } catch (e) { reportError(e, 'Revoke failed') }
  }

  const handleCopy = (link: string) => {
    navigator.clipboard.writeText(link).then(() => toast.success('Link copied'), (e) => reportError(e, 'Copy failed'))
  }

  return (
    <div className="px-4 py-4 border-t border-slate-700/50 flex flex-col gap-2">
      <button onClick={() => setExpanded((v) => !v)} className="flex items-center gap-1.5 text-xs font-medium text-slate-400 uppercase tracking-wider hover:text-slate-300 transition-colors">
        <Link2 size={11} />
        Invite Links
        {expanded ? <ChevronUp size={11} className="ml-auto" /> : <ChevronDown size={11} className="ml-auto" />}
      </button>
      {expanded && (
        <div className="flex flex-col gap-2">
          {loading ? (
            <p className="text-xs text-slate-500">Loading...</p>
          ) : links.length === 0 ? (
            <p className="text-xs text-slate-500">No invite links</p>
          ) : (
            <div className="flex flex-col gap-1.5 max-h-40 overflow-y-auto">
              {links.map((l) => (
                <div key={l.link} className="rounded bg-slate-700/50 px-2 py-1.5 text-[11px] flex flex-col gap-1">
                  <div className="flex items-center gap-1">
                    <span className="text-slate-300 truncate flex-1" title={l.link}>
                      {l.title || l.link.split('/').pop()}
                    </span>
                    <button onClick={() => handleCopy(l.link)} title="Copy" className="text-slate-500 hover:text-slate-300">
                      <Copy size={10} />
                    </button>
                    <button onClick={() => handleRevoke(l.link)} title="Revoke" className="text-slate-500 hover:text-red-400">
                      <Trash2 size={10} />
                    </button>
                  </div>
                  <div className="flex gap-2 text-[10px] text-slate-500">
                    <span>{l.usage} uses{l.usage_limit ? ` / ${l.usage_limit}` : ''}</span>
                    {l.expire_date && <span>Expires: {fmtShortDate(l.expire_date)}</span>}
                  </div>
                </div>
              ))}
            </div>
          )}

          {showCreate ? (
            <div className="rounded border border-slate-600 bg-slate-700/50 p-2 flex flex-col gap-1.5">
              <input
                autoFocus
                value={newTitle}
                onChange={(e) => setNewTitle(e.target.value)}
                placeholder="Link title (optional)"
                className="w-full rounded bg-slate-700 border border-slate-600 px-2 py-1 text-xs text-slate-200 outline-none focus:border-indigo-500"
              />
              <input
                value={newLimit}
                onChange={(e) => setNewLimit(e.target.value.replace(/\D/g, ''))}
                placeholder="Usage limit (optional)"
                className="w-full rounded bg-slate-700 border border-slate-600 px-2 py-1 text-xs text-slate-200 outline-none focus:border-indigo-500"
              />
              <div className="flex gap-1.5">
                <button onClick={handleCreate} disabled={creating} className="flex-1 rounded bg-indigo-500 px-2 py-1 text-[10px] font-medium text-white hover:bg-indigo-600 disabled:opacity-40">
                  {creating ? '...' : 'Create'}
                </button>
                <button onClick={() => setShowCreate(false)} className="rounded px-2 py-1 text-[10px] text-slate-400 hover:text-slate-200">
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <button onClick={() => setShowCreate(true)} className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200 transition-colors">
              <Plus size={12} /> New link
            </button>
          )}
        </div>
      )}
    </div>
  )
}

function GroupSettingsSection({ chat }: { chat: Conversation }) {
  const [expanded, setExpanded] = useState(false)
  const [settings, setSettings] = useState<GroupSettings | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [editTitle, setEditTitle] = useState('')
  const [editAbout, setEditAbout] = useState('')
  const [dirty, setDirty] = useState(false)

  useEffect(() => {
    if (expanded && !settings) {
      setLoading(true)
      getGroupSettings(chat.id)
        .then((s) => {
          setSettings(s)
          setEditTitle(s.title)
          setEditAbout(s.about)
        })
        .catch((e) => reportError(e, 'Couldn’t load group settings'))
        .finally(() => setLoading(false))
    }
  }, [expanded, chat.id, settings])

  const handleSave = async () => {
    setSaving(true)
    try {
      const updates: { title?: string; about?: string } = {}
      if (editTitle !== settings?.title) updates.title = editTitle
      if (editAbout !== settings?.about) updates.about = editAbout
      await updateGroupSettings(chat.id, updates)
      setSettings({ title: editTitle, about: editAbout })
      setDirty(false)
    } catch (e) { reportError(e, 'Save failed') }
    setSaving(false)
  }

  return (
    <div className="px-4 py-4 border-t border-slate-700/50 flex flex-col gap-2">
      <button onClick={() => setExpanded((v) => !v)} className="flex items-center gap-1.5 text-xs font-medium text-slate-400 uppercase tracking-wider hover:text-slate-300 transition-colors">
        <Settings size={11} />
        Group Settings
        {expanded ? <ChevronUp size={11} className="ml-auto" /> : <ChevronDown size={11} className="ml-auto" />}
      </button>
      {expanded && (
        <div className="flex flex-col gap-2">
          {loading ? (
            <p className="text-xs text-slate-500">Loading...</p>
          ) : (
            <>
              <div className="flex flex-col gap-1">
                <label className="text-[10px] font-medium text-slate-500 uppercase">Title</label>
                <input
                  value={editTitle}
                  onChange={(e) => { setEditTitle(e.target.value); setDirty(true) }}
                  className="w-full rounded bg-slate-700 border border-slate-600 px-2 py-1 text-xs text-slate-200 outline-none focus:border-indigo-500"
                />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[10px] font-medium text-slate-500 uppercase">Description</label>
                <textarea
                  value={editAbout}
                  onChange={(e) => { setEditAbout(e.target.value); setDirty(true) }}
                  rows={3}
                  className="w-full rounded bg-slate-700 border border-slate-600 px-2 py-1 text-xs text-slate-200 outline-none focus:border-indigo-500 resize-none"
                />
              </div>
              {dirty && (
                <button onClick={handleSave} disabled={saving} className="self-start rounded bg-indigo-500 px-3 py-1 text-[10px] font-medium text-white hover:bg-indigo-600 disabled:opacity-40">
                  {saving ? 'Saving...' : 'Save Changes'}
                </button>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

function AdminActionLogSection({ chat }: { chat: Conversation }) {
  const [expanded, setExpanded] = useState(false)
  const [logs, setLogs] = useState<AdminActionLog[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (expanded) {
      setLoading(true)
      getAdminActionLog(chat.id)
        .then(setLogs)
        .catch(() => setLogs([]))
        .finally(() => setLoading(false))
    }
  }, [expanded, chat.id])

  const actionLabel = (action: string) => {
    const labels: Record<string, string> = {
      promote: 'Promoted user',
      demote: 'Demoted user',
      ban: 'Banned user',
      restrict: 'Restricted user',
      approve: 'Approved user',
      create_invite_link: 'Created invite link',
      revoke_invite_link: 'Revoked invite link',
      update_settings: 'Updated settings',
    }
    return labels[action] || action
  }

  const actionColor = (action: string) => {
    if (action === 'ban') return 'text-red-400'
    if (action === 'promote') return 'text-sky-400'
    if (action === 'demote') return 'text-amber-400'
    if (action === 'approve') return 'text-emerald-400'
    return 'text-slate-400'
  }

  return (
    <div className="px-4 py-4 border-t border-slate-700/50 flex flex-col gap-2">
      <button onClick={() => setExpanded((v) => !v)} className="flex items-center gap-1.5 text-xs font-medium text-slate-400 uppercase tracking-wider hover:text-slate-300 transition-colors">
        <ClipboardList size={11} />
        Admin Log
        {expanded ? <ChevronUp size={11} className="ml-auto" /> : <ChevronDown size={11} className="ml-auto" />}
      </button>
      {expanded && (
        <div className="flex flex-col gap-1 max-h-48 overflow-y-auto">
          {loading ? (
            <p className="text-xs text-slate-500">Loading...</p>
          ) : logs.length === 0 ? (
            <p className="text-xs text-slate-500">No admin actions recorded</p>
          ) : (
            logs.map((log) => (
              <div key={log.id} className="rounded bg-slate-700/30 px-2 py-1.5 text-[11px] flex flex-col gap-0.5">
                <span className={actionColor(log.action)}>{actionLabel(log.action)}</span>
                {log.target_person_id != null ? (
                  <Link to={personUrl(log.target_person_id)} className="text-slate-400 hover:text-indigo-300">View member</Link>
                ) : log.target_identity_id != null && (
                  <span className="text-slate-500">Member #{log.target_identity_id}</span>
                )}
                {log.details && (
                  <span className="text-slate-500 truncate">{log.details}</span>
                )}
                {log.created_at && (
                  <span className="text-[10px] text-slate-600">{fmtDateTime(log.created_at)}</span>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  )
}

/**
 * "Archive full history": queues a complete walk of this conversation's history
 * (DMs are also covered by the background archive). Complete once every
 * message from the first one is stored locally.
 */
export function ArchiveHistorySection({ chat }: { chat: Conversation }) {
  const [queued, setQueued] = useState(false)
  const [complete, setComplete] = useState(!!chat.history_complete)
  const [count, setCount] = useState<number | null>(null)
  useEffect(() => { if (chat.history_complete) setComplete(true) }, [chat.history_complete])

  const archive = useAsyncAction(async () => {
    const res = await archiveConversation(chat.id)
    setCount(res.coverage.message_count)
    if (res.coverage.history_complete) {
      setComplete(true)
      toast.success('Full history is already stored')
    } else {
      setQueued(res.queued)
      toast.info('Archiving full history in the background')
    }
  }, { errorContext: 'Couldn’t start archiving' })

  return (
    <div className="flex flex-col gap-2 border-b border-slate-700/50 px-4 py-4" data-testid="archive-history">
      <div className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wider text-slate-400">
        <History size={11} /> History
      </div>
      {complete ? (
        <p className="flex items-center gap-1.5 text-xs text-emerald-400">
          <CheckCircle2 size={12} /> Full history archived{count != null ? ` · ${count.toLocaleString()} messages` : ''}
        </p>
      ) : queued ? (
        <p className="flex items-center gap-1.5 text-xs text-indigo-300">
          <Loader2 size={12} className="animate-spin" /> Archiving…{count != null ? ` ${count.toLocaleString()} messages so far` : ''}
        </p>
      ) : (
        <>
          <p className="text-[11px] text-slate-500">
            Only part of this chat's history is stored locally. Archive it so search and agents see every message.
          </p>
          <button
            type="button"
            onClick={() => void archive.run()}
            disabled={archive.pending}
            className="flex items-center justify-center gap-1.5 rounded-lg border border-slate-600 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-700 disabled:opacity-50 transition-colors"
          >
            {archive.pending ? <Loader2 size={12} className="animate-spin" /> : <History size={12} />}
            Archive full history
          </button>
        </>
      )}
    </div>
  )
}

export default function ChatInfoPanel({ chat, onClose }: { chat: Conversation; onClose: () => void }) {
  // Member lists: groups and channels whose channel can list them (capability 'members')
  const hasMembers = (chat.kind === 'group' || chat.kind === 'channel') && can(chat, 'members')
  const [allTags, setAllTags] = useState<Tag[]>([])
  const [chatTags, setChatTags] = useState<Tag[]>([])
  const [suggestions, setSuggestions] = useState<TagSuggestion[]>([])
  const [suggesting, setSuggesting] = useState(false)
  const [showAddDropdown, setShowAddDropdown] = useState(false)
  const [showNewTag, setShowNewTag] = useState(false)
  const [newTagName, setNewTagName] = useState('')
  const [newTagColor, setNewTagColor] = useState<string>('slate')
  const [members, setMembers] = useState<ChatMember[]>([])
  const [loadingMembers, setLoadingMembers] = useState(false)
  const [membersError, setMembersError] = useState<string | null>(null)

  useEffect(() => {
    getTags().then(setAllTags).catch((e) => reportError(e, 'Couldn’t load tags'))
    getConversationTags(chat.id).then(setChatTags).catch((e) => reportError(e, 'Couldn’t load chat tags'))
    setSuggestions([])
    if (hasMembers) {
      setLoadingMembers(true)
      setMembersError(null)
      getConversationMembers(chat.id)
        .then(setMembers)
        .catch((e) => { setMembersError(e.message); setMembers([]) })
        .finally(() => setLoadingMembers(false))
    } else {
      setMembers([])
    }
  }, [chat.id, hasMembers])

  const assignedIds = new Set(chatTags.map((t) => t.id))
  // Stage/stream tags are managed from kanban boards, not the general picker
  const unassignedTags = allTags.filter(
    (t) => !assignedIds.has(t.id) && t.category !== 'stage' && t.category !== 'stream',
  )

  /** Resolves true when the tag is on the chat; failures are toasted */
  const handleAdd = async (tag: Tag): Promise<boolean> => {
    if (assignedIds.has(tag.id)) return true
    try {
      await addTagToConversation(chat.id, tag.id)
    } catch (e) {
      toast.error(`Couldn’t add tag: ${tagErrorMessage(e, tag.name)}`)
      return false
    }
    setChatTags((prev) => (prev.some((t) => t.id === tag.id) ? prev : [...prev, tag]))
    setShowAddDropdown(false)
    setSuggestions((prev) => prev.filter((s) => s.name !== tag.name))
    return true
  }

  /** Create (or reuse an existing same-named) tag and put it on the chat */
  const createAndAdd = async (name: string, color: string): Promise<boolean> => {
    let tag: Tag
    try {
      const res = await createOrFindTag(name, color, allTags)
      tag = res.tag
      if (res.created) setAllTags((prev) => [...prev, res.tag])
    } catch (e) {
      toast.error(`Couldn’t create tag “${name}”: ${tagErrorMessage(e, name)}`)
      return false
    }
    return handleAdd(tag)
  }

  const handleRemove = async (tag: Tag) => {
    try {
      await removeTagFromConversation(chat.id, tag.id)
      setChatTags((prev) => prev.filter((t) => t.id !== tag.id))
    } catch (e) {
      reportError(e, 'Remove tag failed')
    }
  }

  const handleCreateAndAdd = async () => {
    const name = newTagName.trim()
    if (!name) return
    // On failure the form stays open with the name, so the user can fix and retry
    if (await createAndAdd(name, newTagColor)) {
      setNewTagName('')
      setNewTagColor('slate')
      setShowNewTag(false)
    }
  }

  const handleSuggest = async () => {
    setSuggesting(true)
    try {
      const results = await suggestTags(chat.id)
      setSuggestions(results)
    } catch (e) {
      reportError(e, 'Tag suggestions failed')
    } finally {
      setSuggesting(false)
    }
  }

  const handleAcceptSuggestion = async (s: TagSuggestion) => {
    // The suggestion stays (so it can be retried) unless the tag really landed on the chat
    if (await createAndAdd(s.name, s.color)) {
      setSuggestions((prev) => prev.filter((x) => x.name !== s.name))
    }
  }

  const typeColor = kindColor(chat.kind)
  const peerPersonId = chat.kind === 'direct' ? chat.peer?.person_id ?? null : null
  const title = conversationTitle(chat)
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

      {/* Identity -- PersonCard for a direct conversation's peer, simple display otherwise */}
      {peerPersonId != null ? (
        <div className="px-4 py-5 border-b border-slate-700/50">
          <PersonCard
            personId={peerPersonId}
            name={title}
            username={chat.username}
            mode="full"
            showStatus
          />
        </div>
      ) : (
        <div className="flex flex-col items-center gap-2 px-4 py-5 border-b border-slate-700/50">
          <ConversationAvatar conversation={chat} size={56} />
          <p className="text-sm font-semibold text-gray-100 text-center leading-snug">{title}</p>
          <span className="flex items-center gap-1.5">
            <ChannelIcon channel={chat.channel} />
            <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${typeColor}`}>{kindLabel(chat.kind)}</span>
          </span>
        </div>
      )}

      {/* Tags section */}
      <div className="px-4 py-4 flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1.5 text-xs font-medium text-slate-400 uppercase tracking-wider">
            <TagIcon size={11} />
            Tags
          </div>
          <button
            onClick={handleSuggest}
            disabled={suggesting}
            title="Get AI tag suggestions"
            className="flex items-center gap-1 rounded px-2 py-1 text-[11px] text-violet-400 hover:bg-violet-500/10 disabled:opacity-50 transition-colors"
          >
            <Sparkles size={11} />
            {suggesting ? 'Thinking...' : 'Suggest'}
          </button>
        </div>

        {/* Assigned tags grouped by category */}
        {chatTags.length === 0 ? (
          <p className="text-xs text-slate-500">No tags yet</p>
        ) : (
          <div className="flex flex-col gap-2.5">
            {(() => {
              const grouped = chatTags.reduce<Record<string, Tag[]>>((acc, tag) => {
                const cat = tag.category?.trim() || 'Uncategorized'
                ;(acc[cat] = acc[cat] || []).push(tag)
                return acc
              }, {})
              const cats = Object.keys(grouped).sort((a, b) => {
                if (a === 'Uncategorized') return -1
                if (b === 'Uncategorized') return 1
                return a.localeCompare(b)
              })
              return cats.map((cat) => (
                <div key={cat}>
                  <p className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-slate-600">{cat}</p>
                  <div className="flex flex-wrap gap-1.5">
                    {grouped[cat].map((tag) => (
                      <span
                        key={tag.id}
                        className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${TAG_COLORS[tag.color] ?? TAG_COLORS.slate}`}
                      >
                        {tag.name}
                        <button
                          onClick={() => handleRemove(tag)}
                          className="opacity-60 hover:opacity-100 transition-opacity"
                        >
                          <X size={10} />
                        </button>
                      </span>
                    ))}
                  </div>
                </div>
              ))
            })()}
          </div>
        )}

        {/* AI Suggestions */}
        {suggestions.length > 0 && (
          <div className="rounded-lg bg-violet-500/5 border border-violet-500/20 p-2.5 flex flex-col gap-2">
            <p className="text-[10px] font-medium text-violet-400 uppercase tracking-wider">AI Suggestions</p>
            <div className="flex flex-wrap gap-1.5">
              {suggestions.map((s) => (
                <button
                  key={s.name}
                  onClick={() => handleAcceptSuggestion(s)}
                  title={s.reason}
                  className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium border border-dashed hover:border-solid transition-all ${TAG_COLORS[s.color] ?? TAG_COLORS.slate}`}
                >
                  <Plus size={9} />
                  {s.name}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Add existing tag */}
        <div className="relative">
          <button
            onClick={() => { setShowAddDropdown((v) => !v); setShowNewTag(false) }}
            className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200 transition-colors"
          >
            <Plus size={12} />
            Add tag
          </button>
          {showAddDropdown && (
            <div className="absolute left-0 top-6 z-20 w-48 rounded-lg border border-slate-600 bg-slate-800 shadow-xl overflow-hidden">
              {unassignedTags.length === 0 ? (
                <p className="px-3 py-2 text-xs text-slate-500">All tags assigned</p>
              ) : (
                <div className="max-h-48 overflow-y-auto py-1">
                  {(() => {
                    const grouped = unassignedTags.reduce<Record<string, Tag[]>>((acc, tag) => {
                      const cat = tag.category?.trim() || 'Uncategorized'
                      ;(acc[cat] = acc[cat] || []).push(tag)
                      return acc
                    }, {})
                    const cats = Object.keys(grouped).sort((a, b) => {
                      if (a === 'Uncategorized') return -1
                      if (b === 'Uncategorized') return 1
                      return a.localeCompare(b)
                    })
                    return cats.map((cat, i) => (
                      <div key={cat}>
                        {(cats.length > 1 || cat !== 'Uncategorized') && (
                          <p className={`px-3 py-1 text-[10px] font-semibold uppercase tracking-wider text-slate-500 ${i > 0 ? 'border-t border-slate-700 mt-1 pt-1.5' : ''}`}>{cat}</p>
                        )}
                        {grouped[cat].map((tag) => (
                          <button
                            key={tag.id}
                            onClick={() => handleAdd(tag)}
                            className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-slate-300 hover:bg-slate-700 transition-colors"
                          >
                            <span className={`h-2 w-2 rounded-full ${COLOR_DOTS[tag.color] ?? 'bg-slate-400'}`} />
                            {tag.name}
                          </button>
                        ))}
                      </div>
                    ))
                  })()}
                </div>
              )}
              <div className="border-t border-slate-700 px-3 py-1.5">
                <button
                  onClick={() => { setShowNewTag(true); setShowAddDropdown(false) }}
                  className="flex items-center gap-1.5 text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
                >
                  <Plus size={11} />
                  Create new tag
                </button>
              </div>
            </div>
          )}
        </div>

        {/* New tag form */}
        {showNewTag && (
          <div className="rounded-lg border border-slate-600 bg-slate-700/50 p-3 flex flex-col gap-2">
            <input
              autoFocus
              value={newTagName}
              onChange={(e) => setNewTagName(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') handleCreateAndAdd(); if (e.key === 'Escape') setShowNewTag(false) }}
              placeholder="Tag name..."
              className="w-full rounded bg-slate-700 border border-slate-600 px-2 py-1 text-xs text-slate-200 outline-none focus:border-indigo-500"
            />
            <div className="flex gap-1.5">
              {COLOR_OPTIONS.map((c) => (
                <button
                  key={c}
                  onClick={() => setNewTagColor(c)}
                  className={`h-4 w-4 rounded-full ${COLOR_DOTS[c]} transition-transform ${newTagColor === c ? 'scale-125 ring-2 ring-white/40' : 'opacity-60 hover:opacity-100'}`}
                />
              ))}
            </div>
            <div className="flex gap-2">
              <button
                onClick={handleCreateAndAdd}
                disabled={!newTagName.trim()}
                className="flex-1 rounded bg-indigo-500 px-2 py-1 text-xs font-medium text-white hover:bg-indigo-600 disabled:opacity-40 transition-colors"
              >
                Create & Add
              </button>
              <button
                onClick={() => setShowNewTag(false)}
                className="rounded px-2 py-1 text-xs text-slate-400 hover:text-slate-200 transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>

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
