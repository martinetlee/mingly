import { useEffect, useState } from 'react'
import { Settings, ChevronDown, ChevronUp } from 'lucide-react'
import type { Conversation, GroupSettings } from '../../../api/types'
import { getGroupSettings, updateGroupSettings } from '../../../api/client'
import { reportError } from '../../../lib/toast'

export default function GroupSettingsSection({ chat }: { chat: Conversation }) {
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
