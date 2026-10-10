/**
 * The members section shows only where the channel can list members
 * (capability 'members': Telegram groups and channels) — a Signal group
 * never asks the Telegram-only members endpoint and never shows its 409.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { makeConversation } from '../../../test/fixtures'

const getConversationMembers = vi.fn(async () => [])
vi.mock('../../../api/client', async (orig) => {
  const resolve = (v: unknown) => async () => v
  return {
    ...(await orig<typeof import('../../../api/client')>()),
    getTags: resolve([]),
    getConversationTags: resolve([]),
    getConversationMembers: () => getConversationMembers(),
    getSharedMedia: resolve([]),
    getPerson: resolve(null),
  }
})
vi.mock('../ModerationPanel', () => ({ default: () => null }))

import ChatInfoPanel from '../ChatInfoPanel'
import { ConfirmProvider } from '../../ui'

afterEach(() => { cleanup(); getConversationMembers.mockClear() })

const renderPanel = (chat: ReturnType<typeof makeConversation>) => render(
  <ConfirmProvider><MemoryRouter><ChatInfoPanel chat={chat} onClose={() => {}} /></MemoryRouter></ConfirmProvider>,
)

describe('ChatInfoPanel members section', () => {
  it('a Telegram group (capability members) lists its members', async () => {
    renderPanel(makeConversation(1, 'TG group'))
    await waitFor(() => expect(getConversationMembers).toHaveBeenCalled())
    expect(screen.getByText(/^Members/)).toBeTruthy()
  })

  it('a Signal group (no members capability) neither fetches nor shows members', async () => {
    renderPanel(makeConversation(2, 'Signal group', { channel: 'signal', capabilities: ['send', 'reply', 'react', 'media_send', 'read_receipts'] }))
    await act(async () => { await Promise.resolve() })
    expect(getConversationMembers).not.toHaveBeenCalled()
    expect(screen.queryByText(/^Members/)).toBeNull()
  })
})
