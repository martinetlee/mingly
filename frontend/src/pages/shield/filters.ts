import type { ScannedMessageFilters } from '../../api/client'
import type { ScamVerdictFilter } from '../../api/types'

/** Internal ids: sender identity id, conversation id */
export interface DevFilters { senderId: string; conversationId: string; minMembers: string }
export const NO_DEV_FILTERS: DevFilters = { senderId: '', conversationId: '', minMembers: '' }

/** The one filter builder: applied dev filters + verdict tab → API filters. */
export function buildScannedFilters(dev: DevFilters, verdict: ScamVerdictFilter): ScannedMessageFilters {
  const f: ScannedMessageFilters = {}
  const num = (s: string) => { const n = parseInt(s.trim(), 10); return Number.isFinite(n) ? n : undefined }
  const sender = num(dev.senderId)
  const conversation = num(dev.conversationId)
  const minMembers = num(dev.minMembers)
  if (sender != null) f.sender_identity_id = sender
  if (conversation != null) f.conversation_id = conversation
  if (minMembers != null) f.min_members = minMembers
  if (verdict !== 'all') f.verdict_filter = verdict
  return f
}
