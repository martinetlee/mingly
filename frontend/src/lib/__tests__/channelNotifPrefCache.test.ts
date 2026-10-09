/**
 * Saving a channel's notification pref updates the cache NotificationBridge
 * reads, without a reload — the same as the global setNotifPref.
 */
import { describe, it, expect, vi } from 'vitest'

vi.mock('../http', async (orig) => ({
  ...(await orig<typeof import('../http')>()),
  request: vi.fn(async (_path: string, init: { body: string }) => ({ success: true, ...JSON.parse(init.body) })),
}))

import { setChannelNotifPref } from '../../api/email'
import { notifPrefFor, setCachedNotifPref } from '../notifications'

describe('per-channel notification pref cache', () => {
  it('is off for Gmail until set, then follows the saved value', async () => {
    setCachedNotifPref('all', {})
    expect(notifPrefFor('gmail')).toBe('off')
    await setChannelNotifPref('gmail', 'tracked')
    expect(notifPrefFor('gmail')).toBe('tracked')
    expect(notifPrefFor('telegram')).toBe('all')
  })
})
