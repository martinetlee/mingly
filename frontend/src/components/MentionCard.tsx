import { useState } from 'react'
import { AtSign, Bookmark, CheckCheck, ChevronDown, ChevronRight } from 'lucide-react'
import type { MentionGroup, Message } from '../api/types'
import { authenticatedMediaUrl } from '../api/session'
import { fmtDate, fmtTime } from '../utils/time'
import { conversationTitle, kindColor, kindLabel } from '../lib/conversation'
import ChannelIcon from './ChannelIcon'

/** Shared by Home → Mentions and Intelligence → Mentions. */

export type MentionAction = 'not_acted' | 'acknowledged' | 'replied'
export type MentionFilter = 'all' | MentionAction

export function mentionAction(g: MentionGroup): MentionAction {
  if (g.replied) return 'replied'
  if (g.mention_msg.my_reaction) return 'acknowledged'
  return 'not_acted'
}

export function filterMentions(groups: MentionGroup[], filter: MentionFilter): MentionGroup[] {
  return filter === 'all' ? groups : groups.filter((g) => mentionAction(g) === filter)
}

export function countMentions(groups: MentionGroup[]): Record<MentionFilter, number> {
  const counts: Record<MentionFilter, number> = { all: groups.length, not_acted: 0, acknowledged: 0, replied: 0 }
  for (const g of groups) counts[mentionAction(g)]++
  return counts
}

export const MENTION_FILTER_OPTIONS: { label: string; value: MentionFilter }[] = [
  { label: 'Not acted', value: 'not_acted' },
  { label: 'All', value: 'all' },
  { label: 'Acknowledged', value: 'acknowledged' },
  { label: 'Replied', value: 'replied' },
]

export function mentionFilterClass(value: MentionFilter, active: boolean): string {
  if (!active) return 'text-slate-500 hover:text-slate-300 hover:bg-slate-700/50'
  switch (value) {
    case 'not_acted': return 'bg-slate-500/30 text-slate-200'
    case 'acknowledged': return 'bg-amber-500/25 text-amber-300'
    case 'replied': return 'bg-emerald-500/25 text-emerald-300'
    default: return 'bg-slate-600 text-slate-200'
  }
}

export const mentionKey = (g: MentionGroup) => String(g.mention_msg.id)

// Tag color palette (same as Tags page)
const TAG_COLORS: Record<string, string> = {
  slate:  'bg-slate-500/20 text-slate-400',
  red:    'bg-red-500/20 text-red-400',
  orange: 'bg-orange-500/20 text-orange-400',
  amber:  'bg-amber-500/20 text-amber-400',
  yellow: 'bg-yellow-500/20 text-yellow-400',
  green:  'bg-green-500/20 text-green-400',
  teal:   'bg-teal-500/20 text-teal-400',
  cyan:   'bg-cyan-500/20 text-cyan-400',
  blue:   'bg-blue-500/20 text-blue-400',
  indigo: 'bg-indigo-500/20 text-indigo-400',
  violet: 'bg-violet-500/20 text-violet-400',
  purple: 'bg-purple-500/20 text-purple-400',
  pink:   'bg-pink-500/20 text-pink-400',
}

/** "14:05" today, else "Yesterday 14:05" / "Mon 14:05" / "Mar 4 14:05" */
function fmtMsgTime(iso: string): string {
  const day = fmtDate(iso)
  const time = fmtTime(iso)
  return day === time ? time : `${day} ${time}`
}

/** "[photo]" etc. for a message whose only content is attachments */
function attachmentLabel(msg: Pick<Message, 'attachments'>): string | null {
  const first = msg.attachments[0]
  return first ? `[${first.kind}]` : null
}

export function MentionMsgRow({ msg, highlighted }: { msg: Message; highlighted: boolean }) {
  const photo = msg.attachments.find((a) => a.kind === 'photo' && a.url)
  const label = attachmentLabel(msg)
  return (
    <div
      className={`rounded-lg px-3 py-2 text-xs leading-relaxed transition-colors ${
        highlighted ? 'bg-amber-500/10 border border-amber-500/30' : 'bg-slate-700/30'
      }`}
    >
      <div className="flex items-baseline justify-between gap-2 mb-0.5">
        <span className={`font-medium truncate ${highlighted ? 'text-amber-300' : 'text-slate-300'}`}>
          {msg.sender?.name || 'Unknown'}
        </span>
        <span className="shrink-0 text-[10px] text-slate-500">{fmtMsgTime(msg.date)}</span>
      </div>
      {msg.text && <p className="text-slate-400 leading-snug line-clamp-3">{msg.text}</p>}
      {photo?.url && (
        <img
          src={authenticatedMediaUrl(photo.url)}
          alt="photo"
          className="mt-1 rounded-md object-cover"
          style={{ maxWidth: 220, maxHeight: 180 }}
        />
      )}
      {label && !photo && !msg.text && <span className="italic text-slate-500">{label}</span>}
      {!msg.text && !label && <span className="italic text-slate-500">[no text]</span>}
    </div>
  )
}

interface MentionCardProps {
  group: MentionGroup
  onNavigate: (conversationId: number, messageId: number) => void
  selected?: boolean
  onToggleExpand?: (expanded: boolean) => void
}

export default function MentionCard({ group, onNavigate, selected = false, onToggleExpand }: MentionCardProps) {
  const [expanded, setExpanded] = useState(false)
  const toggle = () => {
    const next = !expanded
    setExpanded(next)
    onToggleExpand?.(next)
  }

  const conversationId = group.conversation.id
  const kindBadge = kindColor(group.conversation.kind)
  const allMsgs = [
    ...group.context_before.map((m) => ({ msg: m, highlighted: false })),
    { msg: group.mention_msg, highlighted: true },
    ...group.context_after.map((m) => ({ msg: m, highlighted: false })),
  ]
  const trackedBorder = group.conversation.is_tracked ? 'border-violet-500/40 bg-violet-900/10' : 'border-slate-700/60 bg-slate-800/60'
  const mentionText = group.mention_msg.text || ''
  const senderName = group.mention_msg.sender?.name || 'Unknown'
  const jumpToMention = () => onNavigate(conversationId, group.mention_msg.id)

  return (
    <div className={`rounded-xl border ${trackedBorder} overflow-hidden transition-colors${selected ? ' ring-1 ring-amber-500/50' : ''}`}>
      {/* Header row — always visible, click to expand */}
      <div
        role="button"
        tabIndex={0}
        onClick={toggle}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle() } }}
        className="flex items-center gap-2 px-3 py-2.5 cursor-pointer hover:bg-slate-700/30 transition-colors select-none"
      >
        {expanded ? <ChevronDown size={12} className="shrink-0 text-slate-500" /> : <ChevronRight size={12} className="shrink-0 text-slate-500" />}
        {group.conversation.is_tracked && <Bookmark size={11} className="shrink-0 text-violet-400" />}
        <ChannelIcon channel={group.conversation.channel} />
        <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium shrink-0 ${kindBadge}`}>{kindLabel(group.conversation.kind)}</span>
        <span className="flex-1 min-w-0 truncate text-xs font-semibold text-slate-200">{conversationTitle(group.conversation)}</span>
        {group.conversation.tags.slice(0, 2).map((t) => (
          <span key={t.id} className={`rounded px-1.5 py-0.5 text-[10px] font-medium shrink-0 ${TAG_COLORS[t.color] ?? TAG_COLORS.slate}`}>
            {t.name}
          </span>
        ))}
        {group.replied && (
          <span className="flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] font-medium bg-emerald-500/15 text-emerald-400 shrink-0">
            <CheckCheck size={9} />
            replied
          </span>
        )}
        {group.mention_msg.my_reaction && !group.replied && (
          <span className="flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] font-medium bg-amber-500/15 text-amber-400 shrink-0">
            <span className="text-[10px] leading-none">{group.mention_msg.my_reaction}</span>
            acknowledged
          </span>
        )}
        <AtSign size={11} className="shrink-0 text-amber-400" />
        <span className="shrink-0 text-[10px] text-slate-500">{fmtMsgTime(group.mention_msg.date)}</span>
      </div>

      {/* Collapsed preview: the mention one-liner */}
      {!expanded && (
        <div
          role="button"
          tabIndex={0}
          onClick={jumpToMention}
          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); jumpToMention() } }}
          className="px-3 pb-2.5 cursor-pointer"
        >
          <p className="text-xs text-slate-500 truncate">
            <span className="text-amber-300/70 font-medium">{senderName}: </span>
            {mentionText || <span className="italic">{attachmentLabel(group.mention_msg) ?? '[no text]'}</span>}
          </p>
        </div>
      )}

      {/* Expanded: context, the user's reply, jump buttons */}
      {expanded && (
        <div className="border-t border-slate-700/50">
          <div className="px-3 py-2.5 space-y-1.5">
            {allMsgs.map(({ msg, highlighted }, i) => (
              <MentionMsgRow key={`${msg.id}-${i}`} msg={msg} highlighted={highlighted} />
            ))}
          </div>

          {group.user_reply_msg && (
            <div className="px-3 pb-2.5 space-y-1.5">
              {group.reply_has_gap && (
                <div className="text-center text-[11px] text-slate-500 py-0.5 select-none">
                  <span className="rounded bg-slate-700/40 px-2 py-0.5">···</span>
                </div>
              )}
              <div className="relative">
                <MentionMsgRow msg={group.user_reply_msg} highlighted={false} />
                <button
                  onClick={() => onNavigate(conversationId, group.user_reply_msg!.id)}
                  className="absolute right-1 top-1 rounded px-1.5 py-0.5 text-[10px] text-indigo-400 hover:bg-indigo-900/30 hover:text-indigo-300 transition-colors"
                  title="Jump to your reply in chat"
                >
                  → jump
                </button>
              </div>
            </div>
          )}

          <div className="px-3 pb-3">
            <button
              onClick={jumpToMention}
              className="w-full rounded-lg bg-slate-700 hover:bg-slate-600 px-3 py-1.5 text-xs text-slate-300 transition-colors"
            >
              Jump to mention →
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
