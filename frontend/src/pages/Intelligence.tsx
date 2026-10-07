import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { AtSign, Brain, Newspaper } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import ChatTab from './intelligence/ChatTab'
import MentionsTab from './intelligence/MentionsTab'
import BriefingTab from './intelligence/BriefingTab'

type Tab = 'chat' | 'mentions' | 'briefing'

const TABS: { id: Tab; label: string; icon: LucideIcon; active: string }[] = [
  { id: 'chat', label: 'Chat', icon: Brain, active: 'border-violet-500 text-violet-400' },
  { id: 'mentions', label: 'Mentions', icon: AtSign, active: 'border-amber-500 text-amber-400' },
  { id: 'briefing', label: 'Briefing', icon: Newspaper, active: 'border-emerald-500 text-emerald-400' },
]

const isTab = (v: string | null): v is Tab => v === 'chat' || v === 'mentions' || v === 'briefing'

export default function Intelligence() {
  const [searchParams] = useSearchParams()
  const initial = searchParams.get('tab')
  const [activeTab, setActiveTab] = useState<Tab>(isTab(initial) ? initial : 'chat')

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      <div className="flex shrink-0 border-b border-slate-700/50 bg-slate-800/60 px-4 gap-1">
        {TABS.map(({ id, label, icon: Icon, active }) => (
          <button
            key={id}
            onClick={() => setActiveTab(id)}
            className={`flex items-center gap-1.5 px-3 py-2.5 text-xs font-medium border-b-2 transition-colors ${
              activeTab === id ? active : 'border-transparent text-slate-500 hover:text-slate-300'
            }`}
          >
            <Icon size={13} />
            {label}
          </button>
        ))}
      </div>

      {/* Kept mounted so a reply keeps streaming while another tab is open */}
      <div className={activeTab === 'chat' ? 'flex flex-1 overflow-hidden' : 'hidden'}>
        <ChatTab />
      </div>
      {activeTab === 'mentions' && <MentionsTab />}
      {activeTab === 'briefing' && <BriefingTab />}
    </div>
  )
}
