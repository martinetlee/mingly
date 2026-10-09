import { describe, it, expect, vi, beforeEach } from 'vitest'
import { makeConversation } from '../../pages/chats/__tests__/fixtures'

const getPersonConversations = vi.fn()
const startPrivateChat = vi.fn()
vi.mock('../../api/client', async (orig) => ({
  ...(await orig<typeof import('../../api/client')>()),
  getPersonConversations: (...a: unknown[]) => getPersonConversations(...a),
  startPrivateChat: (...a: unknown[]) => startPrivateChat(...a),
}))

import { canStartConversation, NoConversationError, openDirectConversation } from '../directConversation'

beforeEach(() => {
  getPersonConversations.mockReset()
  startPrivateChat.mockReset()
})

describe('openDirectConversation ("message this person")', () => {
  it('opens the most recent existing one-to-one conversation on any channel', async () => {
    const email = makeConversation(9, 'Ann', { kind: 'email_thread', channel: 'gmail' })
    getPersonConversations.mockResolvedValue([email, makeConversation(3, 'Ann', { kind: 'direct' })])
    await expect(openDirectConversation(7, { canStart: true })).resolves.toBe(email)
    expect(startPrivateChat).not.toHaveBeenCalled()
  })

  it('messengersOnly (chat-only entry points) skips email threads', async () => {
    const email = makeConversation(9, 'Ann', { kind: 'email_thread', channel: 'gmail' })
    const dm = makeConversation(3, 'Ann', { kind: 'direct' })
    getPersonConversations.mockResolvedValue([email, dm])
    await expect(openDirectConversation(7, { canStart: true, messengersOnly: true })).resolves.toBe(dm)
    getPersonConversations.mockResolvedValue([email])
    startPrivateChat.mockResolvedValue(dm)
    await expect(openDirectConversation(7, { canStart: true, messengersOnly: true })).resolves.toBe(dm)
    expect(startPrivateChat).toHaveBeenCalledWith(7)
  })

  it('starts a Telegram chat by person id only when there is none and the person is on Telegram', async () => {
    getPersonConversations.mockResolvedValue([])
    startPrivateChat.mockResolvedValue(makeConversation(4, 'Ann', { kind: 'direct' }))
    await openDirectConversation(7, { canStart: true })
    expect(startPrivateChat).toHaveBeenCalledWith(7)
    startPrivateChat.mockClear()
    await expect(openDirectConversation(8, { canStart: false })).rejects.toBeInstanceOf(NoConversationError)
    expect(startPrivateChat).not.toHaveBeenCalled()
  })

  it('canStartConversation needs a Telegram identity', () => {
    expect(canStartConversation([{ channel: 'gmail' }])).toBe(false)
    expect(canStartConversation([{ channel: 'gmail' }, { channel: 'telegram' }])).toBe(true)
  })
})
