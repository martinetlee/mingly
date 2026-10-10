/** Preferences. */

import { request } from '../lib/http'
import { setCachedNotifPref } from '../lib/notifications'
import type { Prefs } from './types'

export const getSessionTimeout = () =>
  request<{ minutes: number }>('/api/settings/prefs/session-timeout')

/** Idle minutes before a session token expires: 0 (never) or 5..10080 */
export const setSessionTimeout = (minutes: number) =>
  request<{ success: boolean; minutes: number }>('/api/settings/prefs/session-timeout', {
    method: 'POST',
    body: JSON.stringify({ minutes }),
  })

export const getPrefs = () =>
  request<Prefs>('/api/settings/prefs')

export const setNotifPref = async (pref: 'all' | 'tracked' | 'off') => {
  const result = await request<{ success: boolean; pref: string }>('/api/settings/prefs/notifications', {
    method: 'POST',
    body: JSON.stringify({ pref }),
  })
  // Keep the global NotificationBridge in step with the saved value
  setCachedNotifPref(result?.pref ?? pref)
  return result
}
