import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { renderHook, act, waitFor } from '@testing-library/react'
import { WebSocketClient } from '../../lib/ws'
import { WebSocketProvider } from '../../context/WebSocketContext'
import { FeaturesProvider } from '../../context/FeaturesContext'

const getScamFlags = vi.fn()
vi.mock('../../api/client', () => ({
  getScamFlags: (...a: unknown[]) => getScamFlags(...a),
}))

import { __resetFlaggedSendersForTests, applyFlagChanges, useFlaggedSenders } from '../useFlaggedSenders'

const SHIELD_ON = { shield: true }

function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((res) => { resolve = res })
  return { promise, resolve }
}

const flagsResponse = (ids: number[]) => ({ flags: [], flagged_identity_ids: ids, flagged_conversation_ids: [] })

function setup() {
  const client = new WebSocketClient({ url: () => null })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <WebSocketProvider client={client}>
      <FeaturesProvider value={SHIELD_ON}>{children}</FeaturesProvider>
    </WebSocketProvider>
  )
  return { client, wrapper }
}

beforeEach(() => {
  __resetFlaggedSendersForTests()
  getScamFlags.mockReset()
})

describe('useFlaggedSenders store', () => {
  it('seeds from GET /flags flagged_identity_ids and is shared between consumers', async () => {
    getScamFlags.mockResolvedValue(flagsResponse([1, 2]))
    const { wrapper } = setup()
    const a = renderHook(() => useFlaggedSenders(), { wrapper })
    const b = renderHook(() => useFlaggedSenders(), { wrapper })
    await waitFor(() => expect([...a.result.current.ids].sort()).toEqual([1, 2]))
    expect([...b.result.current.ids].sort()).toEqual([1, 2])
    // Only the first consumer triggers the initial load
    expect(getScamFlags).toHaveBeenCalledTimes(1)

    act(() => a.result.current.markDismissed(1))
    expect(b.result.current.ids.has(1)).toBe(false)
  })

  it('follows WS scam_users_flagged (add) and scam_verdict_changed (active/dismissed/reset)', async () => {
    getScamFlags.mockResolvedValue(flagsResponse([1]))
    const { client, wrapper } = setup()
    const { result } = renderHook(() => useFlaggedSenders(), { wrapper })
    await waitFor(() => expect(result.current.ids.has(1)).toBe(true))

    act(() => client.dispatch({ type: 'scam_users_flagged', identity_ids: [5, 6] }))
    expect([...result.current.ids].sort()).toEqual([1, 5, 6])

    act(() => client.dispatch({
      type: 'scam_verdict_changed', scope: 'sender', verdict: 'not_scam', count: 0, messages: [],
      flags: [{ identity_id: 5, status: 'dismissed' }, { identity_id: 6, status: null }, { identity_id: 9, status: 'active' }],
    }))
    expect([...result.current.ids].sort()).toEqual([1, 9])
  })

  it('refetches on WS resync', async () => {
    getScamFlags.mockResolvedValueOnce(flagsResponse([1])).mockResolvedValueOnce(flagsResponse([1, 3]))
    const { client, wrapper } = setup()
    const { result } = renderHook(() => useFlaggedSenders(), { wrapper })
    await waitFor(() => expect(result.current.ids.has(1)).toBe(true))
    act(() => client.dispatch({ type: 'resync' }))
    await waitFor(() => expect(result.current.ids.has(3)).toBe(true))
  })

  it('a slow refresh cannot resurrect a sender dismissed while it was in flight', async () => {
    const slow = deferred<ReturnType<typeof flagsResponse>>()
    getScamFlags.mockReturnValueOnce(slow.promise)
    const { wrapper } = setup()
    const { result } = renderHook(() => useFlaggedSenders(), { wrapper })
    expect(result.current.loading).toBe(true)

    // Dismissed (from a verdict response) after the GET was issued
    act(() => applyFlagChanges([{ identity_id: 7, status: 'dismissed' }]))
    await act(async () => { slow.resolve(flagsResponse([7, 8])) })
    expect([...result.current.ids]).toEqual([8])
    expect(result.current.loading).toBe(false)
  })

  it('does not double-apply events with several consumers mounted', async () => {
    getScamFlags.mockResolvedValue(flagsResponse([]))
    const { client, wrapper } = setup()
    const a = renderHook(() => useFlaggedSenders(), { wrapper })
    renderHook(() => useFlaggedSenders(), { wrapper })
    await waitFor(() => expect(a.result.current.loading).toBe(false))
    expect(client.handlerCount).toBeGreaterThan(0)
    const before = client.handlerCount
    renderHook(() => useFlaggedSenders(), { wrapper })
    // The store subscribes once per client, not once per consumer
    expect(client.handlerCount).toBe(before)
  })
})
