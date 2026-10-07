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

// Backend default (backend/api/settings.py get_notif_pref)
let cachedPref: NotifPref = 'tracked'

export function setCachedNotifPref(pref: string | null | undefined): void {
  if (pref === 'all' || pref === 'tracked' || pref === 'off') cachedPref = pref
}

export function getCachedNotifPref(): NotifPref {
  return cachedPref
}

type NewMessageEvent = Extract<WSEvent, { type: 'new_message' }>

/** Pure policy: should this event produce a desktop notification? */
export function shouldNotify(event: NewMessageEvent, pref: NotifPref = cachedPref): boolean {
  if (event.is_outgoing) return false
  return pref === 'all' || (pref === 'tracked' && !!event.is_tracked)
}

export function notificationContent(event: NewMessageEvent): { title: string; body: string } {
  const title = event.chat_name ?? 'New message'
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
 * `onClick` receives the chat id (used to navigate to the chat).
 */
export function notifyNewMessage(event: NewMessageEvent, onClick?: (chatId: number) => void): boolean {
  if (!shouldNotify(event) || !webNotificationsAvailable()) return false
  const { title, body } = notificationContent(event)
  const n = new Notification(title, { body, silent: false })
  if (onClick) {
    n.onclick = () => {
      window.focus()
      onClick(event.chat_id)
      n.close()
    }
  }
  return true
}
