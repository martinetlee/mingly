/**
 * The Chats space's channel chips: "All" + one per connected messenger.
 * Email channels have their own space (Mail) and never get a chip. The chosen
 * chip is remembered per browser (storage may be unavailable: then it lasts
 * the session).
 */
import type { Account } from '../../api/accounts'
import type { ChannelName } from '../../api/types'

/** Channels shown in Mail, not Chats */
export const EMAIL_CHANNELS: ReadonlySet<string> = new Set(['gmail'])

const STORAGE_KEY = 'mingly_chats_channel'

/** Connected messenger channels, once each, in account order */
export function messengerChannels(accounts: Pick<Account, 'channel' | 'configured'>[]): ChannelName[] {
  const out: ChannelName[] = []
  for (const a of accounts) {
    if (a.configured && !EMAIL_CHANNELS.has(a.channel) && !out.includes(a.channel)) out.push(a.channel)
  }
  return out
}

export function loadChannelFilter(): string | null {
  try { return localStorage.getItem(STORAGE_KEY) || null } catch { return null }
}

export function saveChannelFilter(channel: string | null): void {
  try {
    if (channel) localStorage.setItem(STORAGE_KEY, channel)
    else localStorage.removeItem(STORAGE_KEY)
  } catch { /* storage unavailable: the choice lasts this session */ }
}
