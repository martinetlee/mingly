/** useChatList: a failed first page is an inline `error` (no toast); a good page or a new key clears it. */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { makeConversation } from '../../../test/fixtures'

vi.mock('../../../api/client', async (orig) => ({
  ...(await orig<typeof import('../../../api/client')>()),
  getConversations: vi.fn(),
}))
vi.mock('../../../lib/toast', async (orig) => ({
  ...(await orig<typeof import('../../../lib/toast')>()),
  reportError: vi.fn(),
}))

import { getConversations } from '../../../api/client'
import { reportError } from '../../../lib/toast'
import { useChatList } from '../useChatList'
import type { ChatListKey } from '../useChatList'

const key = (workspaceId: number | null): ChatListKey => ({ sortBy: 'recent', tagId: null, workspaceId })

beforeEach(() => { vi.mocked(getConversations).mockReset(); vi.mocked(reportError).mockReset() })

describe('useChatList error', () => {
  it('first page failure → error string, no toast; retry and key change clear it', async () => {
    vi.mocked(getConversations).mockRejectedValueOnce(new Error('backend down'))
    const { result, rerender } = renderHook(({ k }) => useChatList(k), { initialProps: { k: key(null) } })
    await waitFor(() => expect(result.current.error).toBe('backend down'))
    expect(result.current.loading).toBe(false)
    expect(reportError).not.toHaveBeenCalled()

    vi.mocked(getConversations).mockResolvedValueOnce([makeConversation(1, 'A')])
    await act(async () => { await result.current.reload() })
    expect(result.current.error).toBeNull()
    expect(result.current.chats.map((c) => c.id)).toEqual([1])

    vi.mocked(getConversations).mockRejectedValueOnce(new Error('again'))
    await act(async () => { await result.current.reload() })
    expect(result.current.error).toBe('again')
    expect(reportError).toHaveBeenCalledTimes(1) // a failed refresh keeps the rows and toasts

    vi.mocked(getConversations).mockResolvedValueOnce([])
    rerender({ k: key(5) })
    expect(result.current.error).toBeNull()
  })
})
