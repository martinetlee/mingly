import { useEffect, useState, useCallback } from 'react'
import { Link } from 'react-router-dom'
import { X, Sparkles, Plus, Tag as TagIcon, Users, Crown, Shield, Bot, UserPlus, Image, FileText, LinkIcon, Download, ExternalLink, ShieldPlus, ShieldMinus, Ban, UserCheck, Link2, Settings, ClipboardList, Copy, Trash2, ChevronDown, ChevronUp } from 'lucide-react'
import type { Chat, ChatMember, Contact, SharedMediaItem, Tag, TagSuggestion, AdminPermissions, InviteLink, GroupSettings, AdminActionLog } from '../api/types'
import { getTags, getChatTags, addTagToChat, removeTagFromChat, createTag, suggestTags, getChatMembers, getContactByTelegram, createContact, getSharedMedia, promoteAdmin, demoteAdmin, banMember, approveMember, getInviteLinks, createInviteLink, revokeInviteLink, getGroupSettings, updateGroupSettings, getAdminActionLog, isAbortError } from '../api/client'
import { authenticatedMediaUrl } from '../api/session'
import ContactInfoSection from './ContactInfoSection'
import ModerationPanel from './ModerationPanel'
import PersonCard, { RELATIONSHIP_STATUS_COLORS, RELATIONSHIP_STATUS_LABELS, fmtRelative } from './PersonCard'

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

function PrivateContactSection({ chat }: { chat: Chat }) {
  const [contact, setContact] = useState<Contact | null>(null)
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)

  useEffect(() => {
    setLoading(true)
    setContact(null)
    getContactByTelegram(chat.telegram_id)
      .then(setContact)
      .catch(() => setContact(null))
      .finally(() => setLoading(false))
  }, [chat.telegram_id])

  const handleAddToContacts = async () => {
    setCreating(true)
    try {
      const created = await createContact({
        telegram_id: chat.telegram_id,
        first_name: chat.name.split(' ')[0] || chat.name,
        last_name: chat.name.split(' ').slice(1).join(' ') || undefined,
        username: chat.username || undefined,
      })
      setContact(created)
    } finally {
      setCreating(false)
    }
  }

  const activePosition = contact?.positions?.filter((p) => p.is_active !== false)?.[0] ?? null

  return (
    <div className="px-4 py-4 border-t border-slate-700/50 flex flex-col gap-3">
      <div className="flex items-center gap-1.5 text-xs font-medium text-slate-400 uppercase tracking-wider">
        <UserPlus size={11} />
        Contact
      </div>

      {loading ? (
        <p className="text-xs text-slate-500">Loading...</p>
      ) : !contact ? (
        <button
          onClick={handleAddToContacts}
          disabled={creating}
          className="flex items-center gap-1.5 self-start rounded-lg border border-slate-600 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-700 hover:text-white disabled:opacity-50 transition-colors"
        >
          <UserPlus size={12} />
          {creating ? 'Adding...' : 'Add to Contacts'}
        </button>
      ) : (
        <>
          {/* Relationship status and recency */}
          {(contact.relationship_status || contact.last_interaction_at) && (
            <div className="flex items-center gap-2 text-xs">
              {contact.relationship_status && (
                <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${RELATIONSHIP_STATUS_COLORS[contact.relationship_status] ?? ''}`}>
                  {RELATIONSHIP_STATUS_LABELS[contact.relationship_status] ?? contact.relationship_status}
                </span>
              )}
              {contact.last_interaction_at && (
                <span className="text-slate-500">Last: {fmtRelative(contact.last_interaction_at)}</span>
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
            to={`/profile/${chat.telegram_id}`}
            className="text-[10px] text-indigo-400 hover:text-indigo-300 transition-colors self-start"
          >
            View full profile
          </Link>
          <ContactInfoSection contact={contact} onContactChange={setContact} compact />
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

function extractUrls(text: string | null): string[] {
  if (!text) return []
  const matches = text.match(/https?:\/\/[^\s<>"]+/g)
  return matches ?? []
}

function SharedMediaSection({ chat }: { chat: Chat }) {
  const [activeTab, setActiveTab] = useState<SharedMediaTab>('media')
  const [items, setItems] = useState<SharedMediaItem[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    setLoading(true)
    setItems([])
    // Abort on tab/chat switch so a slower earlier response can't populate
    // the currently active tab with the wrong items
    const ac = new AbortController()
    getSharedMedia(chat.telegram_id, activeTab, 50, 0, ac.signal)
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
  }, [chat.telegram_id, activeTab])

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
          {items.map((item) => (
            <div key={item.id} className="relative aspect-square overflow-hidden rounded">
              {item.media_url ? (
                <img
                  src={authenticatedMediaUrl(item.media_url)}
                  alt=""
                  className="h-full w-full object-cover hover:opacity-80 transition-opacity cursor-pointer"
                />
              ) : (
                <div className="flex h-full w-full items-center justify-center bg-slate-700 text-slate-500">
                  <Image size={16} />
                </div>
              )}
              {item.media_type === 'gif' && (
                <span className="absolute bottom-0.5 left-0.5 rounded bg-black/60 px-1 text-[8px] text-white">GIF</span>
              )}
              {item.media_type === 'video' && (
                <span className="absolute bottom-0.5 left-0.5 rounded bg-black/60 px-1 text-[8px] text-white">VID</span>
              )}
            </div>
          ))}
        </div>
      ) : activeTab === 'files' ? (
        <div className="flex flex-col gap-1.5 max-h-64 overflow-y-auto">
          {items.map((item) => (
            <a
              key={item.id}
              href={item.media_url ? authenticatedMediaUrl(item.media_url) : '#'}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-2 rounded-lg bg-slate-700/50 px-2.5 py-2 hover:bg-slate-700 transition-colors"
            >
              <FileText size={14} className="flex-shrink-0 text-slate-400" />
              <div className="min-w-0 flex-1">
                <p className="truncate text-xs text-slate-200">
                  {item.media_filename || 'Document'}
                </p>
                <p className="text-[10px] text-slate-500">
                  {formatFileSize(item.media_size)}
                  {item.date && ` \u00b7 ${new Date(item.date).toLocaleDateString()}`}
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
                      {item.date && ` \u00b7 ${new Date(item.date).toLocaleDateString()}`}
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

function MemberActionMenu({ chat, member, onAction }: { chat: Chat; member: ChatMember; onAction: () => void }) {
  const [open, setOpen] = useState(false)
  const [showPromote, setShowPromote] = useState(false)
  const [perms, setPerms] = useState<AdminPermissions>({ ...DEFAULT_PERMISSIONS })
  const [loading, setLoading] = useState(false)

  if (member.role === 'creator') return null

  const handlePromote = async () => {
    setLoading(true)
    try {
      await promoteAdmin(chat.telegram_id, member.id, perms)
      setShowPromote(false)
      setOpen(false)
      onAction()
    } catch { /* ignore */ }
    setLoading(false)
  }

  const handleDemote = async () => {
    setLoading(true)
    try {
      await demoteAdmin(chat.telegram_id, member.id)
      setOpen(false)
      onAction()
    } catch { /* ignore */ }
    setLoading(false)
  }

  const handleBan = async () => {
    if (!confirm(`Ban ${member.name}?`)) return
    setLoading(true)
    try {
      await banMember(chat.telegram_id, member.id)
      setOpen(false)
      onAction()
    } catch { /* ignore */ }
    setLoading(false)
  }

  const handleApprove = async () => {
    setLoading(true)
    try {
      await approveMember(chat.telegram_id, member.id)
      setOpen(false)
      onAction()
    } catch { /* ignore */ }
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

  if (chat.type === 'channel') {
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

function MembersSection({ chat, members, loadingMembers, membersError, onMembersChange }: {
  chat: Chat
  members: ChatMember[]
  loadingMembers: boolean
  membersError: string | null
  onMembersChange: (m: ChatMember[]) => void
}) {
  const refreshMembers = useCallback(() => {
    getChatMembers(chat.telegram_id).then(onMembersChange).catch(() => {})
  }, [chat.telegram_id, onMembersChange])

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
            <div key={m.id} className="flex items-center gap-1">
              <div className="flex-1 min-w-0">
                <PersonCard
                  telegramId={m.id}
                  name={m.name}
                  username={m.username}
                  showStatus={false}
                  extraBadge={
                    <div className="flex items-center gap-1">
                      {m.is_bot && <span title="Bot"><Bot size={10} className="text-violet-400" /></span>}
                      {m.role === 'creator' && <span title="Creator"><Crown size={10} className="text-amber-400" /></span>}
                      {m.role === 'admin' && <span title="Admin"><Shield size={10} className="text-sky-400" /></span>}
                    </div>
                  }
                />
              </div>
              {!m.is_bot && (
                <MemberActionMenu chat={chat} member={m} onAction={refreshMembers} />
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function InviteLinksSection({ chat }: { chat: Chat }) {
  const [expanded, setExpanded] = useState(false)
  const [links, setLinks] = useState<InviteLink[]>([])
  const [loading, setLoading] = useState(false)
  const [creating, setCreating] = useState(false)
  const [showCreate, setShowCreate] = useState(false)
  const [newTitle, setNewTitle] = useState('')
  const [newLimit, setNewLimit] = useState('')

  const loadLinks = useCallback(() => {
    setLoading(true)
    getInviteLinks(chat.telegram_id)
      .then(setLinks)
      .catch(() => setLinks([]))
      .finally(() => setLoading(false))
  }, [chat.telegram_id])

  useEffect(() => {
    if (expanded) loadLinks()
  }, [expanded, loadLinks])

  const handleCreate = async () => {
    setCreating(true)
    try {
      const link = await createInviteLink(chat.telegram_id, {
        title: newTitle || undefined,
        usage_limit: newLimit ? parseInt(newLimit, 10) : undefined,
      })
      setLinks((prev) => [...prev, link])
      setShowCreate(false)
      setNewTitle('')
      setNewLimit('')
    } catch { /* ignore */ }
    setCreating(false)
  }

  const handleRevoke = async (link: string) => {
    try {
      await revokeInviteLink(chat.telegram_id, link)
      setLinks((prev) => prev.filter((l) => l.link !== link))
    } catch { /* ignore */ }
  }

  const handleCopy = (link: string) => {
    navigator.clipboard.writeText(link).catch(() => {})
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
                    {l.expire_date && <span>Expires: {new Date(l.expire_date).toLocaleDateString()}</span>}
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

function GroupSettingsSection({ chat }: { chat: Chat }) {
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
      getGroupSettings(chat.telegram_id)
        .then((s) => {
          setSettings(s)
          setEditTitle(s.title)
          setEditAbout(s.about)
        })
        .catch(() => {})
        .finally(() => setLoading(false))
    }
  }, [expanded, chat.telegram_id, settings])

  const handleSave = async () => {
    setSaving(true)
    try {
      const updates: { title?: string; about?: string } = {}
      if (editTitle !== settings?.title) updates.title = editTitle
      if (editAbout !== settings?.about) updates.about = editAbout
      await updateGroupSettings(chat.telegram_id, updates)
      setSettings({ title: editTitle, about: editAbout })
      setDirty(false)
    } catch { /* ignore */ }
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

function AdminActionLogSection({ chat }: { chat: Chat }) {
  const [expanded, setExpanded] = useState(false)
  const [logs, setLogs] = useState<AdminActionLog[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (expanded) {
      setLoading(true)
      getAdminActionLog(chat.telegram_id)
        .then(setLogs)
        .catch(() => setLogs([]))
        .finally(() => setLoading(false))
    }
  }, [expanded, chat.telegram_id])

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
                {log.target_user_id && (
                  <span className="text-slate-500">User ID: {log.target_user_id}</span>
                )}
                {log.details && (
                  <span className="text-slate-500 truncate">{log.details}</span>
                )}
                {log.created_at && (
                  <span className="text-[10px] text-slate-600">{new Date(log.created_at).toLocaleString()}</span>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  )
}

export default function ChatInfoPanel({ chat, onClose }: { chat: Chat; onClose: () => void }) {
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
    getTags().then(setAllTags).catch(() => {})
    getChatTags(chat.telegram_id).then(setChatTags).catch(() => {})
    setSuggestions([])
    if (chat.type !== 'private') {
      setLoadingMembers(true)
      setMembersError(null)
      getChatMembers(chat.telegram_id)
        .then(setMembers)
        .catch((e) => { setMembersError(e.message); setMembers([]) })
        .finally(() => setLoadingMembers(false))
    } else {
      setMembers([])
    }
  }, [chat.telegram_id, chat.type])

  const assignedIds = new Set(chatTags.map((t) => t.id))
  // Stage/stream tags are managed from kanban boards, not the general picker
  const unassignedTags = allTags.filter(
    (t) => !assignedIds.has(t.id) && t.category !== 'stage' && t.category !== 'stream',
  )

  const handleAdd = async (tag: Tag) => {
    await addTagToChat(chat.telegram_id, tag.id)
    setChatTags((prev) => [...prev, tag])
    setAllTags((prev) => prev)
    setShowAddDropdown(false)
    setSuggestions((prev) => prev.filter((s) => s.name !== tag.name))
  }

  const handleRemove = async (tag: Tag) => {
    await removeTagFromChat(chat.telegram_id, tag.id)
    setChatTags((prev) => prev.filter((t) => t.id !== tag.id))
  }

  const handleCreateAndAdd = async () => {
    const name = newTagName.trim()
    if (!name) return
    try {
      const tag = await createTag(name, newTagColor)
      setAllTags((prev) => [...prev, tag])
      await addTagToChat(chat.telegram_id, tag.id)
      setChatTags((prev) => [...prev, tag])
      setNewTagName('')
      setNewTagColor('slate')
      setShowNewTag(false)
    } catch {
      const existing = allTags.find((t) => t.name.toLowerCase() === name.toLowerCase())
      if (existing && !assignedIds.has(existing.id)) await handleAdd(existing)
    }
  }

  const handleSuggest = async () => {
    setSuggesting(true)
    try {
      const results = await suggestTags(chat.telegram_id)
      setSuggestions(results)
    } finally {
      setSuggesting(false)
    }
  }

  const handleAcceptSuggestion = async (s: TagSuggestion) => {
    const existing = allTags.find((t) => t.name.toLowerCase() === s.name.toLowerCase())
    if (existing) {
      if (!assignedIds.has(existing.id)) await handleAdd(existing)
    } else {
      try {
        const tag = await createTag(s.name, s.color)
        setAllTags((prev) => [...prev, tag])
        await addTagToChat(chat.telegram_id, tag.id)
        setChatTags((prev) => [...prev, tag])
      } catch { /* already exists */ }
    }
    setSuggestions((prev) => prev.filter((x) => x.name !== s.name))
  }

  const typeColor = {
    private: 'bg-indigo-500/20 text-indigo-400',
    group: 'bg-purple-500/20 text-purple-400',
    channel: 'bg-amber-500/20 text-amber-400',
  }[chat.type] ?? 'bg-slate-600 text-slate-400'

  return (
    <div className="flex w-72 flex-shrink-0 flex-col border-l border-slate-700/50 bg-slate-800 overflow-y-auto">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-slate-700/50 px-4 py-3">
        <span className="text-xs font-medium text-slate-400 uppercase tracking-wider">Chat Info</span>
        <button onClick={onClose} className="rounded p-1 text-slate-500 hover:text-slate-300 transition-colors">
          <X size={14} />
        </button>
      </div>

      {/* Chat identity -- PersonCard for private chats, simple display for groups/channels */}
      {chat.type === 'private' ? (
        <div className="px-4 py-5 border-b border-slate-700/50">
          <PersonCard
            telegramId={chat.telegram_id}
            name={chat.name}
            username={chat.username}
            mode="full"
            showStatus
          />
        </div>
      ) : (
        <div className="flex flex-col items-center gap-2 px-4 py-5 border-b border-slate-700/50">
          <div className="flex h-14 w-14 items-center justify-center rounded-full bg-slate-700 text-xl font-semibold text-slate-300">
            {chat.name.charAt(0).toUpperCase()}
          </div>
          <p className="text-sm font-semibold text-gray-100 text-center leading-snug">{chat.name}</p>
          <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${typeColor}`}>{chat.type}</span>
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

      {/* Shared media section */}
      <SharedMediaSection chat={chat} />

      {/* Contact section (private chats only) */}
      {chat.type === 'private' && (
        <PrivateContactSection chat={chat} />
      )}

      {/* Moderation controls (groups/channels) */}
      {chat.type !== 'private' && (
        <ModerationPanel chat={chat} />
      )}

      {/* Members section (groups/channels only) */}
      {chat.type !== 'private' && (
        <MembersSection chat={chat} members={members} loadingMembers={loadingMembers} membersError={membersError} onMembersChange={setMembers} />
      )}

      {/* Admin sections (groups/channels only) */}
      {chat.type !== 'private' && (
        <>
          <InviteLinksSection chat={chat} />
          <GroupSettingsSection chat={chat} />
          <AdminActionLogSection chat={chat} />
        </>
      )}
    </div>
  )
}
