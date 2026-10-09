/** Who a reply goes to — the same rules the Gmail connector applies when `to` is left empty. */
import type { Message } from '../../api/types'

function addressOf(header: string | null | undefined): string | null {
  if (!header) return null
  const addr = (header.match(/<([^>]+)>/)?.[1] ?? header).trim().toLowerCase()
  return addr.includes('@') ? addr : null
}

/** Reply: the sender (or their Reply-To); answering yourself goes to your original recipients */
export function replyRecipients(target: Message | null | undefined): string[] {
  if (!target) return []
  const parts = target.participants ?? []
  if (target.is_outgoing) return parts.filter((p) => p.role === 'to').map((p) => p.external_id)
  const replyTo = addressOf(typeof target.meta?.reply_to === 'string' ? target.meta.reply_to : null)
  if (replyTo) return [replyTo]
  const from = parts.find((p) => p.role === 'from')
  return from ? [from.external_id] : []
}

/** Reply all: the reply recipients plus everyone else on To, the original Cc on Cc — never yourself */
export function replyAllRecipients(target: Message | null | undefined, me: string | null): { to: string[]; cc: string[] } {
  const self = me?.toLowerCase() ?? null
  const to: string[] = []
  const cc: string[] = []
  const add = (list: string[], addr: string) => {
    if (addr && addr !== self && !to.includes(addr) && !cc.includes(addr)) list.push(addr)
  }
  replyRecipients(target).forEach((a) => add(to, a))
  for (const p of target?.participants ?? []) if (p.role === 'to') add(to, p.external_id)
  for (const p of target?.participants ?? []) if (p.role === 'cc') add(cc, p.external_id)
  return { to, cc }
}

export function replySubject(subject: string | null | undefined): string {
  const base = (subject ?? '').trim()
  return /^re:/i.test(base) ? base : `Re: ${base}`.trim()
}
