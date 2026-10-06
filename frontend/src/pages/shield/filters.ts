import type { ScannedMessageFilters } from '../../api/client'
import type { ScamVerdictFilter } from '../../api/types'

export interface DevFilters { senderId: string; chatId: string; minMembers: string }
export const NO_DEV_FILTERS: DevFilters = { senderId: '', chatId: '', minMembers: '' }

/** The one filter builder: applied dev filters + verdict tab → API filters. */
export function buildScannedFilters(dev: DevFilters, verdict: ScamVerdictFilter): ScannedMessageFilters {
  const f: ScannedMessageFilters = {}
  const num = (s: string) => { const n = parseInt(s.trim(), 10); return Number.isFinite(n) ? n : undefined }
  const sender = num(dev.senderId)
  const chat = num(dev.chatId)
  const minMembers = num(dev.minMembers)
  if (sender != null) f.sender_id = sender
  if (chat != null) f.chat_id = chat
  if (minMembers != null) f.min_members = minMembers
  if (verdict !== 'all') f.verdict_filter = verdict
  return f
}
