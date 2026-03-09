import { useEffect, useState, useCallback, useRef } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { RefreshCw, Radio, EyeOff, Eye, FileText, Info, X, Sparkles, Plus, Tag as TagIcon, Users, Crown, Shield, Bot, Phone, Twitter, Linkedin, StickyNote, Building2, Pencil, UserPlus } from 'lucide-react'
import type { Chat, ChatMember, Contact, ContactPosition, Folder, Message, WSEvent, Tag, TagSuggestion } from '../api/types'
import { getChats, getFolders, getMessages, syncChat, sendMessage, getHiddenChats, setHidden, setBriefing, setTracked, getTags, getChatTags, addTagToChat, removeTagFromChat, createTag, suggestTags, getChatMembers, getContactByTelegram, createContact, updateContact, addPosition, removePosition, markChatRead, reactToMessage } from '../api/client'
import { useWebSocket } from '../hooks/useWebSocket'
import MessageList from '../components/MessageList'
import ComposeBar from '../components/ComposeBar'
import ComboInput from '../components/ComboInput'

function ChatItem({
  chat,
  selected,
  onClick,
  onToggleTrack,
  onHide,
  onBriefing,
}: {
  chat: Chat
  selected: boolean
  onClick: () => void
  onToggleTrack: () => void
  onHide: () => void
  onBriefing: () => void
}) {
  const [hovered, setHovered] = useState(false)

  const typeColor = {
    private: 'bg-indigo-500/20 text-indigo-400',
    group: 'bg-purple-500/20 text-purple-400',
    channel: 'bg-amber-500/20 text-amber-400',
  }[chat.type] ?? 'bg-slate-600 text-slate-400'

  return (
    <div
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      className={`flex cursor-pointer items-center gap-3 px-3 py-2.5 transition-colors ${
        selected ? 'bg-indigo-500/15' : 'hover:bg-slate-700/50'
      }`}
    >
      {/* Avatar */}
      <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full bg-slate-700 text-sm font-medium text-slate-300">
        {chat.name.charAt(0).toUpperCase()}
      </div>

      {/* Name + type */}
      <div className="min-w-0 flex-1">
        <p className={`truncate text-sm font-medium ${selected ? 'text-indigo-300' : 'text-gray-200'}`}>
          {chat.name}
        </p>
        <span className={`inline-block rounded px-1 text-[10px] font-medium ${typeColor}`}>
          {chat.type}
        </span>
      </div>

      {/* Actions */}
      <div className="flex flex-shrink-0 items-center gap-0.5">
        {hovered && (
          <>
            <button
              onClick={(e) => { e.stopPropagation(); onBriefing() }}
              title="Add to briefing"
              className="rounded p-1 text-slate-500 hover:text-purple-400 transition-colors"
            >
              <FileText size={14} />
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); onHide() }}
              title="Hide this chat"
              className="rounded p-1 text-slate-500 hover:text-slate-300 transition-colors"
            >
              <EyeOff size={14} />
            </button>
          </>
        )}
        {!hovered && (chat.unread_count ?? 0) > 0 && (
          <span className="min-w-[18px] rounded-full bg-indigo-500 px-1.5 py-0.5 text-center text-[10px] font-semibold leading-tight text-white">
            {(chat.unread_count ?? 0) > 99 ? '99+' : chat.unread_count}
          </span>
        )}
        <button
          onClick={(e) => { e.stopPropagation(); onToggleTrack() }}
          title={chat.is_tracked ? 'Stop tracking' : 'Track this chat'}
          className={`rounded p-1 transition-colors ${
            chat.is_tracked
              ? 'text-indigo-400 hover:text-indigo-300'
              : 'text-slate-600 hover:text-slate-400'
          }`}
        >
          <Radio size={14} />
        </button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Tag colours
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// EditableField (reusable inside ChatInfoPanel / PrivateContactSection)
// ---------------------------------------------------------------------------

function EditableField({
  icon,
  label,
  value,
  placeholder,
  onSave,
  isUrl,
}: {
  icon: React.ReactNode
  label: string
  value: string | null
  placeholder: string
  onSave: (val: string) => void
  isUrl?: boolean
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(value || '')

  useEffect(() => { setDraft(value || ''); setEditing(false) }, [value])

  if (editing) {
    return (
      <div className="flex items-center gap-2">
        <span className="text-slate-500 flex-shrink-0">{icon}</span>
        <input
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') { onSave(draft); setEditing(false) }
            if (e.key === 'Escape') setEditing(false)
          }}
          onBlur={() => { onSave(draft); setEditing(false) }}
          placeholder={placeholder}
          className="flex-1 bg-slate-700 border border-slate-600 rounded px-2 py-0.5 text-xs text-slate-200 outline-none focus:border-indigo-500"
        />
      </div>
    )
  }

  return (
    <div className="flex items-center gap-2 group">
      <span className="text-slate-500 flex-shrink-0">{icon}</span>
      <span className="w-14 text-[10px] text-slate-500 uppercase tracking-wider flex-shrink-0">{label}</span>
      {value ? (
        isUrl ? (
          <a
            href={value.startsWith('http') ? value : `https://${value}`}
            target="_blank"
            rel="noopener noreferrer"
            className="flex-1 text-xs text-indigo-400 hover:text-indigo-300 truncate transition-colors"
          >
            {value}
          </a>
        ) : (
          <span className="flex-1 text-xs text-gray-200 truncate">{value}</span>
        )
      ) : (
        <span className="flex-1 text-xs text-slate-600 italic">{placeholder}</span>
      )}
      <button
        onClick={() => setEditing(true)}
        className="opacity-0 group-hover:opacity-100 text-slate-500 hover:text-slate-300 transition-opacity p-0.5"
      >
        <Pencil size={10} />
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// PrivateContactSection
// ---------------------------------------------------------------------------

function PrivateContactSection({ chat }: { chat: Chat }) {
  const [contact, setContact] = useState<Contact | null>(null)
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [showAddPos, setShowAddPos] = useState(false)
  const [newOrg, setNewOrg] = useState('')
  const [newTitle, setNewTitle] = useState('')

  useEffect(() => {
    setLoading(true)
    setContact(null)
    setShowAddPos(false)
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

  const handleUpdateField = async (field: string, value: string) => {
    if (!contact) return
    const updated = await updateContact(contact.id, { [field]: value })
    setContact(updated)
  }

  const handleAddPosition = async () => {
    if (!contact || !newOrg.trim()) return
    const pos = await addPosition(contact.id, newOrg.trim(), newTitle.trim() || undefined)
    setContact((prev) => prev ? { ...prev, positions: [...prev.positions, pos] } : prev)
    setNewOrg('')
    setNewTitle('')
    setShowAddPos(false)
  }

  const handleRemovePosition = async (posId: number) => {
    if (!contact) return
    await removePosition(contact.id, posId)
    setContact((prev) => prev ? { ...prev, positions: prev.positions.filter((p: ContactPosition) => p.id !== posId) } : prev)
  }

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
          {/* Link to full contact/profile */}
          <Link
            to={`/profile/${chat.telegram_id}`}
            className="text-[10px] text-indigo-400 hover:text-indigo-300 transition-colors self-start"
          >
            View full profile
          </Link>

          {/* Editable fields */}
          <div className="flex flex-col gap-2.5">
            <EditableField
              icon={<Phone size={12} />}
              label="Phone"
              value={contact.phone}
              placeholder="Add phone..."
              onSave={(v) => handleUpdateField('phone', v)}
            />
            <EditableField
              icon={<Twitter size={12} />}
              label="Twitter"
              value={contact.twitter_handle}
              placeholder="Add handle..."
              onSave={(v) => handleUpdateField('twitter_handle', v)}
            />
            <EditableField
              icon={<Linkedin size={12} />}
              label="LinkedIn"
              value={contact.linkedin_url}
              placeholder="Add URL..."
              onSave={(v) => handleUpdateField('linkedin_url', v)}
              isUrl
            />
            <EditableField
              icon={<StickyNote size={12} />}
              label="Notes"
              value={contact.notes}
              placeholder="Add notes..."
              onSave={(v) => handleUpdateField('notes', v)}
            />
          </div>

          {/* Positions */}
          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <span className="text-[10px] font-medium uppercase tracking-wider text-slate-500 flex items-center gap-1">
                <Building2 size={10} />
                Positions
              </span>
              <button
                onClick={() => setShowAddPos((v) => !v)}
                className="text-[10px] text-indigo-400 hover:text-indigo-300 transition-colors flex items-center gap-0.5"
              >
                <Plus size={10} />
                Add
              </button>
            </div>

            {contact.positions.length === 0 && !showAddPos && (
              <p className="text-xs text-slate-500">No positions</p>
            )}

            {contact.positions.map((p: ContactPosition) => (
              <div key={p.id} className="flex items-center gap-2 group">
                <Building2 size={11} className="text-slate-500 flex-shrink-0" />
                <div className="flex-1 min-w-0">
                  <p className="text-xs text-gray-200 truncate">
                    {p.title ? <><span className="text-slate-400">{p.title}</span> @ </> : ''}
                    {p.organization}
                  </p>
                </div>
                <button
                  onClick={() => handleRemovePosition(p.id)}
                  className="opacity-0 group-hover:opacity-100 text-slate-500 hover:text-rose-400 transition-all p-0.5"
                >
                  <X size={10} />
                </button>
              </div>
            ))}

            {showAddPos && (
              <div className="flex flex-col gap-2 rounded-lg border border-slate-600 bg-slate-700/50 p-2.5">
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
                <div className="flex gap-2">
                  <button
                    onClick={handleAddPosition}
                    disabled={!newOrg.trim()}
                    className="flex-1 rounded bg-indigo-500 px-2 py-1 text-xs font-medium text-white hover:bg-indigo-600 disabled:opacity-40 transition-colors"
                  >
                    Add
                  </button>
                  <button
                    onClick={() => setShowAddPos(false)}
                    className="rounded px-2 py-1 text-xs text-slate-400 hover:text-slate-200 transition-colors"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// ChatInfoPanel
// ---------------------------------------------------------------------------

function ChatInfoPanel({ chat, onClose }: { chat: Chat; onClose: () => void }) {
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
    // Load members for groups/channels
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
  const unassignedTags = allTags.filter((t) => !assignedIds.has(t.id))

  const handleAdd = async (tag: Tag) => {
    await addTagToChat(chat.telegram_id, tag.id)
    setChatTags((prev) => [...prev, tag])
    setAllTags((prev) => prev) // unchanged
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
      // tag name already exists — find and add it
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
    // Find existing tag or create new
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

      {/* Chat identity */}
      <div className="flex flex-col items-center gap-2 px-4 py-5 border-b border-slate-700/50">
        <div className="flex h-14 w-14 items-center justify-center rounded-full bg-slate-700 text-xl font-semibold text-slate-300">
          {chat.name.charAt(0).toUpperCase()}
        </div>
        <p className="text-sm font-semibold text-gray-100 text-center leading-snug">{chat.name}</p>
        <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${typeColor}`}>{chat.type}</span>
      </div>

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
            {suggesting ? 'Thinking…' : 'Suggest'}
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
              placeholder="Tag name…"
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

      {/* Contact section (private chats only) */}
      {chat.type === 'private' && (
        <PrivateContactSection chat={chat} />
      )}

      {/* Members section (groups/channels only) */}
      {chat.type !== 'private' && (
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
                <Link
                  key={m.id}
                  to={`/profile/${m.id}`}
                  className="flex items-center gap-2 rounded-lg px-2 py-1.5 hover:bg-slate-700/50 transition-colors group"
                >
                  <div className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full bg-slate-700 text-[10px] font-medium text-slate-300">
                    {m.name.charAt(0).toUpperCase()}
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-xs text-gray-200 group-hover:text-indigo-300 transition-colors">
                      {m.name}
                    </p>
                    {m.username && (
                      <p className="truncate text-[10px] text-slate-500">@{m.username}</p>
                    )}
                  </div>
                  <div className="flex items-center gap-1 flex-shrink-0">
                    {m.is_bot && <span title="Bot"><Bot size={10} className="text-violet-400" /></span>}
                    {m.role === 'creator' && <span title="Creator"><Crown size={10} className="text-amber-400" /></span>}
                    {m.role === 'admin' && <span title="Admin"><Shield size={10} className="text-sky-400" /></span>}
                  </div>
                </Link>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

export default function Chats() {
  const location = useLocation()
  const [chats, setChats] = useState<Chat[]>([])
  const [folders, setFolders] = useState<Folder[]>([])
  const [selectedFolder, setSelectedFolder] = useState<number | null>(null)
  const [selectedChat, setSelectedChat] = useState<Chat | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [loadingMessages, setLoadingMessages] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [messageOffset, setMessageOffset] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [search, setSearch] = useState('')
  const [showHidden, setShowHidden] = useState(false)
  const [hiddenChats, setHiddenChats] = useState<Chat[]>([])
  const [showInfo, setShowInfo] = useState(false)
  const [sidebarWidth, setSidebarWidth] = useState(288) // 72 * 4 = w-72
  const dragging = useRef(false)
  const LIMIT = 100
  const CHAT_PAGE = 50
  const [chatOffset, setChatOffset] = useState(0)
  const [hasMoreChats, setHasMoreChats] = useState(false)
  const [loadingMoreChats, setLoadingMoreChats] = useState(false)
  const chatListRef = useRef<HTMLDivElement>(null)
  // Track which chat telegram_ids have been auto-synced to avoid duplicate requests
  const autoSyncedRef = useRef<Set<number>>(new Set())
  // Highlight a specific message (set from URL params when navigating from Mentions)
  const [highlightMsgId, setHighlightMsgId] = useState<number | null>(null)
  const pendingNavRef = useRef<{ chatId: number; msgId: number } | null>(null)
  // Reply-to state
  const [replyToMessage, setReplyToMessage] = useState<import('../api/types').Message | null>(null)

  const onDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    dragging.current = true
    const startX = e.clientX
    const startWidth = sidebarWidth

    const onMove = (e: MouseEvent) => {
      if (!dragging.current) return
      const next = Math.min(480, Math.max(200, startWidth + e.clientX - startX))
      setSidebarWidth(next)
    }
    const onUp = () => {
      dragging.current = false
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }, [sidebarWidth])

  const loadChats = useCallback(async () => {
    const data = await getChats(CHAT_PAGE, 0)
    setChats(data)
    setChatOffset(0)
    setHasMoreChats(data.length === CHAT_PAGE)
  }, [CHAT_PAGE])

  const loadMoreChats = useCallback(async () => {
    if (loadingMoreChats || !hasMoreChats) return
    setLoadingMoreChats(true)
    try {
      const nextOffset = chatOffset + CHAT_PAGE
      const data = await getChats(CHAT_PAGE, nextOffset)
      setChats((prev) => {
        const existingIds = new Set(prev.map((c) => c.telegram_id))
        const newChats = data.filter((c) => !existingIds.has(c.telegram_id))
        // Auto-sync 25 messages for each newly visible chat (fire-and-forget)
        for (const c of newChats) {
          if (!autoSyncedRef.current.has(c.telegram_id)) {
            autoSyncedRef.current.add(c.telegram_id)
            syncChat(c.telegram_id, 25).catch(() => {})
          }
        }
        return [...prev, ...newChats]
      })
      setChatOffset(nextOffset)
      setHasMoreChats(data.length === CHAT_PAGE)
    } finally {
      setLoadingMoreChats(false)
    }
  }, [loadingMoreChats, hasMoreChats, chatOffset, CHAT_PAGE])

  const loadFolders = useCallback(async () => {
    const data = await getFolders()
    setFolders(data)
  }, [])

  const loadMessages = useCallback(async (chat: Chat) => {
    setLoadingMessages(true)
    setMessageOffset(0)
    try {
      const data = await getMessages(chat.telegram_id, LIMIT, 0)
      setMessages(data)
      setHasMore(data.length === LIMIT)
    } finally {
      setLoadingMessages(false)
    }
  }, [LIMIT])

  const loadMoreMessages = useCallback(async () => {
    if (!selectedChat || loadingMore) return
    setLoadingMore(true)
    const nextOffset = messageOffset + LIMIT
    try {
      const data = await getMessages(selectedChat.telegram_id, LIMIT, nextOffset)
      setMessages((prev) => [...data, ...prev])
      setMessageOffset(nextOffset)
      setHasMore(data.length === LIMIT)
    } finally {
      setLoadingMore(false)
    }
  }, [selectedChat, messageOffset, loadingMore, LIMIT])

  useEffect(() => {
    // Parse URL params for mention navigation: ?chatId=X&msgId=Y
    const params = new URLSearchParams(location.search)
    const chatIdParam = params.get('chatId')
    const msgIdParam = params.get('msgId')
    if (chatIdParam && msgIdParam) {
      const chatId = parseInt(chatIdParam, 10)
      const msgId = parseInt(msgIdParam, 10)
      if (!isNaN(chatId) && !isNaN(msgId)) {
        pendingNavRef.current = { chatId, msgId }
      }
    }
    loadChats()
    loadFolders()
  }, [loadChats, loadFolders, location.search])

  // When chats load, auto-select the chat from URL params if pending
  useEffect(() => {
    if (chats.length === 0 || !pendingNavRef.current) return
    const { chatId, msgId } = pendingNavRef.current
    pendingNavRef.current = null
    const target = chats.find((c) => c.telegram_id === chatId)
    if (target) {
      setSelectedChat(target)
      setMessages([])
      setHasMore(false)
      setMessageOffset(0)
      setShowInfo(false)
      setHighlightMsgId(msgId)
    }
  }, [chats])

  useEffect(() => {
    if (selectedChat) {
      loadMessages(selectedChat)
    }
  }, [selectedChat, loadMessages])

  const handleWsEvent = useCallback((event: WSEvent) => {
    if (event.type === 'new_message') {
      // If this chat_id is new (not yet seen in our list), sync 25 messages after loading
      setChats((prev) => {
        const known = prev.some((c) => c.telegram_id === event.chat_id)
        if (!known && !autoSyncedRef.current.has(event.chat_id)) {
          autoSyncedRef.current.add(event.chat_id)
          // Delay slightly so the chat upsert from loadChats completes first
          setTimeout(() => syncChat(event.chat_id, 25).catch(() => {}), 1500)
        }
        return prev
      })
      loadChats()
      if (selectedChat && event.chat_id === selectedChat.telegram_id) {
        loadMessages(selectedChat)
      }
      // Fire desktop notification if not outgoing
      if (!event.is_outgoing) {
        const pref = localStorage.getItem('notifPref') ?? 'tracked'
        const shouldNotify =
          pref === 'all' || (pref === 'tracked' && event.is_tracked)
        if (shouldNotify && Notification.permission === 'granted') {
          const title = event.chat_name ?? 'New message'
          const body = event.sender_name
            ? event.sender_name + ': ' + (event.text ?? '')
            : (event.text ?? '')
          new Notification(title, { body, silent: false })
        }
      }
    }
  }, [selectedChat, loadChats, loadMessages])

  useWebSocket(handleWsEvent)

  const handleSelectChat = (chat: Chat) => {
    if (selectedChat?.telegram_id === chat.telegram_id) return
    setSelectedChat(chat)
    setMessages([])
    setHasMore(false)
    setMessageOffset(0)
    setShowInfo(false)
    setHighlightMsgId(null)
    setReplyToMessage(null)
    // Mark as read: clear badge locally and persist to backend
    if ((chat.unread_count ?? 0) > 0) {
      setChats((prev) =>
        prev.map((c) => c.telegram_id === chat.telegram_id ? { ...c, unread_count: 0 } : c)
      )
      markChatRead(chat.telegram_id).catch(() => {})
    }
  }

  const handleTrackChat = async (chat: Chat) => {
    const tracked = !chat.is_tracked
    await setTracked(chat.telegram_id, tracked)
    setChats((prev) =>
      prev.map((c) => (c.telegram_id === chat.telegram_id ? { ...c, is_tracked: tracked } : c))
    )
    if (selectedChat?.telegram_id === chat.telegram_id) {
      setSelectedChat((c) => c ? { ...c, is_tracked: tracked } : c)
    }
  }

  const handleBriefingChat = async (chat: Chat) => {
    await setBriefing(chat.telegram_id, true)
    setChats((prev) => prev.filter((c) => c.telegram_id !== chat.telegram_id))
    if (selectedChat?.telegram_id === chat.telegram_id) {
      setSelectedChat(null)
      setMessages([])
    }
  }

  const handleHideChat = async (chat: Chat) => {
    await setHidden(chat.telegram_id, true)
    setChats((prev) => prev.filter((c) => c.telegram_id !== chat.telegram_id))
    if (selectedChat?.telegram_id === chat.telegram_id) {
      setSelectedChat(null)
      setMessages([])
    }
  }

  const handleUnhideChat = async (chat: Chat) => {
    await setHidden(chat.telegram_id, false)
    setHiddenChats((prev) => prev.filter((c) => c.telegram_id !== chat.telegram_id))
    setChats((prev) => [...prev, { ...chat, is_hidden: false }])
  }

  const handleToggleHiddenPanel = async () => {
    if (!showHidden) {
      const data = await getHiddenChats()
      setHiddenChats(data)
    }
    setShowHidden((v) => !v)
  }

  const handleSync = async () => {
    if (!selectedChat) return
    setSyncing(true)
    try {
      await syncChat(selectedChat.telegram_id)
      await loadMessages(selectedChat)
    } finally {
      setSyncing(false)
    }
  }

  const handleSend = async (text: string) => {
    if (!selectedChat) return
    await sendMessage(selectedChat.telegram_id, text, replyToMessage?.telegram_id)
    setReplyToMessage(null)
    await loadMessages(selectedChat)
  }

  const handleReact = async (msg: Message, emoji: string | null) => {
    if (!selectedChat) return
    try {
      await reactToMessage(selectedChat.telegram_id, msg.telegram_id, emoji)
      // Update the message's my_reaction in local state immediately
      setMessages((prev) =>
        prev.map((m) => m.telegram_id === msg.telegram_id ? { ...m, my_reaction: emoji } : m)
      )
    } catch {
      // silently ignore
    }
  }

  const folderFiltered = selectedFolder === null
    ? chats
    : (() => {
        const folder = folders.find((f) => f.id === selectedFolder)
        if (!folder) return chats
        return chats.filter((c) => {
          if (folder.chat_ids.includes(c.telegram_id)) return true
          if (folder.include_groups && c.type === 'group') return true
          if (folder.include_channels && c.type === 'channel') return true
          if (folder.include_contacts && c.type === 'private') return true
          return false
        })
      })()

  const filtered = folderFiltered.filter((c) =>
    c.name.toLowerCase().includes(search.toLowerCase())
  )

  return (
    <div className="flex flex-1 overflow-hidden">
      {/* Chat list */}
      <div className="flex flex-shrink-0 flex-col border-r border-slate-700/50 bg-slate-800 relative" style={{ width: sidebarWidth }}>
        {/* Drag handle */}
        <div
          onMouseDown={onDragStart}
          className="absolute right-0 top-0 h-full w-1 cursor-col-resize hover:bg-indigo-500 transition-colors z-10"
        />
        {folders.length > 0 && (
          <div className="folder-scrollbar flex overflow-x-auto border-b border-slate-700/50 px-2 pt-2 pb-1 gap-1">
            <button
              onClick={() => setSelectedFolder(null)}
              className={`flex-shrink-0 rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                selectedFolder === null
                  ? 'bg-indigo-500/20 text-indigo-400'
                  : 'text-slate-400 hover:bg-slate-700'
              }`}
            >
              All
            </button>
            {folders.map((folder) => (
              <button
                key={folder.id}
                onClick={() => setSelectedFolder(folder.id)}
                className={`flex-shrink-0 rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                  selectedFolder === folder.id
                    ? 'bg-indigo-500/20 text-indigo-400'
                    : 'text-slate-400 hover:bg-slate-700'
                }`}
              >
                {folder.title}
              </button>
            ))}
          </div>
        )}
        <div className="border-b border-slate-700/50 px-3 py-3">
          <input
            type="text"
            placeholder="Search chats…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-sm text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500/30"
          />
        </div>
        <div
          ref={chatListRef}
          className="flex-1 overflow-y-auto"
          onScroll={(e) => {
            const el = e.currentTarget
            if (el.scrollHeight - el.scrollTop - el.clientHeight < 80) {
              loadMoreChats()
            }
          }}
        >
          {filtered.length === 0 ? (
            <p className="px-4 py-6 text-center text-sm text-slate-500">No chats found</p>
          ) : (
            filtered.map((chat) => (
              <ChatItem
                key={chat.telegram_id}
                chat={chat}
                selected={selectedChat?.telegram_id === chat.telegram_id}
                onClick={() => handleSelectChat(chat)}
                onToggleTrack={() => handleTrackChat(chat)}
                onHide={() => handleHideChat(chat)}
                onBriefing={() => handleBriefingChat(chat)}
              />
            ))
          )}
          {loadingMoreChats && (
            <p className="px-4 py-3 text-center text-xs text-slate-500">Loading…</p>
          )}
        </div>

        {/* Hidden chats panel */}
        <div className="border-t border-slate-700/50">
          <button
            onClick={handleToggleHiddenPanel}
            className="flex w-full items-center gap-1.5 px-3 py-2 text-xs text-slate-500 hover:text-slate-300 transition-colors"
          >
            <Eye size={12} />
            {showHidden ? 'Hide hidden chats' : 'Show hidden chats'}
          </button>
          {showHidden && (
            <div className="max-h-48 overflow-y-auto border-t border-slate-700/50 bg-slate-900/50">
              {hiddenChats.length === 0 ? (
                <p className="px-4 py-3 text-center text-xs text-slate-500">No hidden chats</p>
              ) : (
                hiddenChats.map((chat) => (
                  <div key={chat.telegram_id} className="flex items-center gap-2 px-3 py-2">
                    <div className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full bg-slate-700 text-xs font-medium text-slate-400">
                      {chat.name.charAt(0).toUpperCase()}
                    </div>
                    <p className="min-w-0 flex-1 truncate text-xs text-slate-400">{chat.name}</p>
                    <button
                      onClick={() => handleUnhideChat(chat)}
                      title="Unhide"
                      className="flex-shrink-0 rounded p-1 text-slate-500 hover:text-slate-300 transition-colors"
                    >
                      <Eye size={12} />
                    </button>
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      </div>

      {/* Message area + optional info panel */}
      <div className="flex flex-1 overflow-hidden">
        <div className="flex flex-1 flex-col overflow-hidden bg-slate-900">
          {selectedChat ? (
            <>
              {/* Header */}
              <div className="flex items-center justify-between border-b border-slate-700/50 bg-slate-800 px-4 py-3">
                <div>
                  <h2 className="text-sm font-semibold text-gray-100">{selectedChat.name}</h2>
                  <p className="text-xs text-slate-400">{selectedChat.type}</p>
                </div>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => setShowInfo((v) => !v)}
                    title="Chat info & tags"
                    className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs transition-colors ${
                      showInfo ? 'bg-indigo-500/20 text-indigo-400' : 'text-slate-400 hover:bg-slate-700'
                    }`}
                  >
                    <Info size={13} />
                    Info
                  </button>
                  <button
                    onClick={handleSync}
                    disabled={syncing}
                    title="Sync messages"
                    className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs text-slate-400 hover:bg-slate-700 disabled:opacity-50"
                  >
                    <RefreshCw size={13} className={syncing ? 'animate-spin' : ''} />
                    Sync
                  </button>
                </div>
              </div>

              {/* Messages */}
              {loadingMessages ? (
                <div className="flex flex-1 items-center justify-center text-sm text-slate-500">
                  Loading…
                </div>
              ) : (
                <div className="flex flex-1 flex-col overflow-hidden">
                  {hasMore && (
                    <div className="flex justify-center border-b border-slate-700/50 py-2">
                      <button
                        onClick={loadMoreMessages}
                        disabled={loadingMore}
                        className="text-xs text-indigo-400 hover:text-indigo-300 disabled:opacity-50"
                      >
                        {loadingMore ? 'Loading…' : 'Load older messages'}
                      </button>
                    </div>
                  )}
                  <MessageList messages={messages} chatType={selectedChat.type} highlightTelegramId={highlightMsgId} onReply={setReplyToMessage} onReact={handleReact} />
                </div>
              )}

              {/* Compose */}
              <ComposeBar onSend={handleSend} replyTo={replyToMessage} onCancelReply={() => setReplyToMessage(null)} />
            </>
          ) : (
            <div className="flex flex-1 flex-col items-center justify-center text-slate-500">
              <p className="text-sm">Select a chat to start</p>
            </div>
          )}
        </div>

        {/* Info panel */}
        {showInfo && selectedChat && (
          <ChatInfoPanel chat={selectedChat} onClose={() => setShowInfo(false)} />
        )}
      </div>
    </div>
  )
}
