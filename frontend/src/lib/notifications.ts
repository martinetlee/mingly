/**
 * Desktop notification policy for incoming messages.
 *
 * The packaged app (pywebview) gets native macOS notifications from the
 * backend (`sync.py` → `_send_native_notification`, same `notif_pref` rule),
 * so web notifications here only fire in a plain browser (dev mode) and are
 * skipped under pywebview to avoid duplicates.
 *
 * Fired from exactly one place: <NotificationBridge> in the provider tree.
 */
import type { WSEvent } from '../api/types'

export type NotifPref = 'all' | 'tracked' | 'off'

const isPref = (v: unknown): v is NotifPref => v === 'all' || v === 'tracked' || v === 'off'

// Backend defaults (backend/api/settings.py get_notif_pref)
let cachedPref: NotifPref = 'tracked'
let cachedChannelPrefs: Record<string, NotifPref> = {}
/** Channels whose notifications are off until set per channel (NOTIF_DEFAULT_OFF_CHANNELS) */
export const NOTIF_DEFAULT_OFF_CHANNELS: ReadonlySet<string> = new Set(['gmail'])

/** The global pref, and (when given) every per-channel pref (`notif_pref_channels`) */
export function setCachedNotifPref(pref: string | null | undefined, channels?: Record<string, string> | null): void {
  if (isPref(pref)) cachedPref = pref
  if (channels !== undefined) {
    cachedChannelPrefs = {}
    for (const [ch, p] of Object.entries(channels ?? {})) if (isPref(p)) cachedChannelPrefs[ch] = p
  }
}

/** One channel's pref changed (Settings → Notifications) */
export function setCachedChannelNotifPref(channel: string, pref: string): void {
  if (isPref(pref)) cachedChannelPrefs = { ...cachedChannelPrefs, [channel]: pref }
}

export function getCachedNotifPref(): NotifPref {
  return cachedPref
}

/**
 * The pref that applies to a channel — the same rule as the backend's
 * get_notif_pref(channel): its own setting, else off for email channels,
 * else the global one.
 */
export function notifPrefFor(
  channel: string | null | undefined,
  global: NotifPref = cachedPref,
  perChannel: Record<string, NotifPref> = cachedChannelPrefs,
): NotifPref {
  if (channel) {
    const own = perChannel[channel]
    if (own) return own
    if (NOTIF_DEFAULT_OFF_CHANNELS.has(channel)) return 'off'
  }
  return global
}

type NewMessageEvent = Extract<WSEvent, { type: 'new_message' }>

/** Pure policy: should this event produce a desktop notification? `pref` = the global pref */
export function shouldNotify(event: NewMessageEvent, pref: NotifPref = cachedPref): boolean {
  if (event.is_outgoing) return false
  const effective = notifPrefFor(event.channel, pref)
  return effective === 'all' || (effective === 'tracked' && !!event.is_tracked)
}

export function notificationContent(event: NewMessageEvent): { title: string; body: string } {
  const title = event.conversation_title ?? 'New message'
  const body = event.sender_name ? event.sender_name + ': ' + (event.text ?? '') : (event.text ?? '')
  return { title, body }
}

function webNotificationsAvailable(): boolean {
  if (typeof window === 'undefined') return false
  // Native notifications come from the backend inside the desktop shell
  if ((window as unknown as { pywebview?: unknown }).pywebview) return false
  return typeof Notification !== 'undefined' && Notification.permission === 'granted'
}

/**
 * Show a web notification for a new_message event if policy allows.
 * `onClick` receives the conversation id (used to navigate to it).
 */
export function notifyNewMessage(event: NewMessageEvent, onClick?: (conversationId: number) => void): boolean {
  if (!shouldNotify(event) || !webNotificationsAvailable()) return false
  const { title, body } = notificationContent(event)
  const n = new Notification(title, { body, silent: false })
  if (onClick) {
    n.onclick = () => {
      window.focus()
      onClick(event.conversation_id)
      n.close()
    }
  }
  return true
}
