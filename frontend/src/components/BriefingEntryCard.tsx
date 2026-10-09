import { useCallback } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { CalendarDays, ChevronDown, ChevronRight, Newspaper, Radio, RefreshCw, Star, X } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import type { BriefingHistoryEntry, BriefingSource, BriefingSummary } from '../api/types'
import Markdown from './Markdown'
import { openConversationUrl, personUrl } from '../lib/routes'

type Category = BriefingSummary['category']

interface Accent {
  label: string
  icon: LucideIcon
  iconClass: string
  heading: string
  badge: string
  card: string
  name: string
  button: string
  flow: string
  flowBox: string
}

const ACCENTS: Record<Category, Accent> = {
  meetings: {
    label: 'Today’s Meetings', icon: CalendarDays, iconClass: 'text-sky-400',
    heading: 'text-sky-400', badge: 'bg-sky-500/20 text-sky-400',
    card: 'border-sky-500/20 bg-sky-900/10', name: 'text-sky-300',
    button: 'border-sky-500/30 text-sky-300 hover:bg-sky-900/40',
    flow: 'border-sky-500/20 text-sky-200 hover:bg-sky-900/30', flowBox: 'border-sky-500/20',
  },
  important_contact: {
    label: 'Important Contacts', icon: Star, iconClass: 'fill-amber-400 text-amber-400',
    heading: 'text-amber-400', badge: 'bg-amber-500/20 text-amber-400',
    card: 'border-amber-500/20 bg-amber-900/10', name: 'text-amber-300',
    button: 'border-amber-500/30 text-amber-300 hover:bg-amber-900/40',
    flow: 'border-amber-500/20 text-amber-200 hover:bg-amber-900/30', flowBox: 'border-amber-500/20',
  },
  tracked: {
    label: 'Tracked Chats', icon: Radio, iconClass: 'text-violet-400',
    heading: 'text-violet-400', badge: 'bg-violet-500/20 text-violet-400',
    card: 'border-violet-500/20 bg-violet-900/10', name: 'text-violet-300',
    button: 'border-violet-500/30 text-violet-300 hover:bg-violet-900/40',
    flow: 'border-violet-500/20 text-violet-200 hover:bg-violet-900/30', flowBox: 'border-violet-500/20',
  },
  briefing: {
    label: 'Briefing Chats', icon: Newspaper, iconClass: 'text-emerald-400',
    heading: 'text-emerald-400', badge: 'bg-emerald-500/20 text-emerald-400',
    card: 'border-emerald-500/20 bg-emerald-900/10', name: 'text-emerald-300',
    button: 'border-emerald-500/30 text-emerald-300 hover:bg-emerald-900/40',
    flow: 'border-emerald-500/20 text-emerald-200 hover:bg-emerald-900/30', flowBox: 'border-emerald-500/20',
  },
}

const NO_SOURCES: BriefingSource[] = []

const CATEGORY_ORDER: Category[] = ['meetings', 'important_contact', 'tracked', 'briefing']

/** Categories the LLM writes (regeneratable); meetings are listed from the calendar */
const GENERATED: ReadonlySet<Category> = new Set(['important_contact', 'tracked', 'briefing'])

/** Regenerate controls for entries of a persisted run */
export interface RegenerateControls {
  activeEntryId: number | null
  /** Disable Regenerate everywhere (another regeneration is running) */
  busy: boolean
  reasoning: Record<number, string>
  tokens: Record<number, number>
  flowOpen: Record<number, boolean>
  onRegenerate: (entry: BriefingHistoryEntry) => void
  onStop: () => void
  onToggleFlow: (entryId: number) => void
}

const isHistoryEntry = (e: BriefingSummary | BriefingHistoryEntry): e is BriefingHistoryEntry =>
  typeof (e as BriefingHistoryEntry).id === 'number'

/** Navigate to a cited source message */
export function useCiteNavigation() {
  const navigate = useNavigate()
  return useCallback((src: BriefingSource) => navigate(openConversationUrl(src.conversation_id, src.message_id)), [navigate])
}

interface CardProps {
  entry: BriefingSummary | BriefingHistoryEntry
  onCite: (src: BriefingSource) => void
  regen?: RegenerateControls
}

export default function BriefingEntryCard({ entry, onCite, regen }: CardProps) {
  const accent = ACCENTS[entry.category] ?? ACCENTS.briefing
  const id = isHistoryEntry(entry) ? entry.id : null
  const active = id != null && regen?.activeEntryId === id
  const reasoning = id != null ? regen?.reasoning[id] ?? '' : ''
  const flowOpen = id != null && (regen?.flowOpen[id] ?? false)

  return (
    <div className={`rounded-lg border px-3 py-2 ${accent.card}`}>
      <div className="mb-1 flex items-center justify-between gap-2">
        {(() => {
          // Important-person entries link to the person; meetings to the Calendar; the rest to their conversation
          const to = entry.category === 'meetings' ? '/calendar'
            : entry.person_id != null && entry.conversation_id == null ? personUrl(entry.person_id)
            : entry.conversation_id != null ? openConversationUrl(entry.conversation_id) : null
          return to
            ? <Link to={to} className={`text-[11px] font-medium hover:underline ${accent.name}`}>{entry.conversation_title}</Link>
            : <p className={`text-[11px] font-medium ${accent.name}`}>{entry.conversation_title}</p>
        })()}
        {regen && id != null && isHistoryEntry(entry) && GENERATED.has(entry.category) && (
          <div className="inline-flex items-center gap-1">
            {active ? (
              <button
                onClick={regen.onStop}
                className="inline-flex items-center gap-1 rounded border border-rose-500/30 px-1.5 py-0.5 text-[10px] text-rose-300 hover:bg-rose-900/40"
                title="Stop regenerating"
              >
                <X size={10} />
                Stop
              </button>
            ) : (
              <button
                onClick={() => regen.onRegenerate(entry)}
                disabled={regen.busy}
                className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] disabled:cursor-not-allowed disabled:opacity-40 ${accent.button}`}
                title={regen.busy ? 'Another summary is being regenerated' : 'Regenerate this summary only'}
              >
                <RefreshCw size={10} />
                Regenerate
              </button>
            )}
            {(active || reasoning.length > 0) && (
              <button
                onClick={() => regen.onToggleFlow(id)}
                className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] ${accent.flow}`}
                title="Show or hide reasoning flow"
              >
                {flowOpen ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
                Flow
                {active && typeof regen.tokens[id] === 'number' && (
                  <span className="text-[9px] opacity-80">{regen.tokens[id]}</span>
                )}
              </button>
            )}
          </div>
        )}
      </div>
      <Markdown
        content={entry.summary}
        size="xs"
        className={`text-slate-300 ${active ? 'opacity-60' : ''}`}
        sources={entry.sources ?? NO_SOURCES}
        onCite={onCite}
      />
      {flowOpen && (
        <div className={`mt-2 rounded border bg-slate-900/60 p-2 ${accent.flowBox}`}>
          <pre className="whitespace-pre-wrap break-words text-[11px] leading-relaxed text-slate-400">
            {reasoning || 'Waiting for reasoning stream…'}
          </pre>
        </div>
      )}
    </div>
  )
}

/** Entries of one run grouped by category (today's meetings, important contacts, tracked, briefing). */
export function BriefingEntryList({
  entries,
  regen,
}: {
  entries: (BriefingSummary | BriefingHistoryEntry)[]
  regen?: RegenerateControls
}) {
  const onCite = useCiteNavigation()
  return (
    <div className="space-y-4">
      {CATEGORY_ORDER.map((category) => {
        const items = entries.filter((e) => e.category === category)
        if (items.length === 0) return null
        const accent = ACCENTS[category]
        const Icon = accent.icon
        return (
          <section key={category}>
            <div className="mb-2 flex items-center gap-2">
              <Icon size={12} className={accent.iconClass} />
              <h3 className={`text-[11px] font-semibold uppercase tracking-wider ${accent.heading}`}>{accent.label}</h3>
              <span className={`rounded-full px-1.5 py-0.5 text-[10px] ${accent.badge}`}>{items.length}</span>
            </div>
            <div className="space-y-2">
              {items.map((e, i) => (
                <BriefingEntryCard
                  key={isHistoryEntry(e) ? e.id : `${e.conversation_id}-${i}`}
                  entry={e}
                  onCite={onCite}
                  regen={regen}
                />
              ))}
            </div>
          </section>
        )
      })}
    </div>
  )
}
