import { describe, it, expect, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import type { Workspace } from '../../api/types'

function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((res) => { resolve = res })
  return { promise, resolve }
}

const getWorkspaces = vi.hoisted(() => vi.fn())
vi.mock('../../api/client', () => ({ getWorkspaces: (...a: unknown[]) => getWorkspaces(...a) }))
vi.mock('../../hooks/useWebSocket', () => ({ useWsEvent: () => {}, useWebSocket: () => {} }))

import { WorkspaceProvider, useWorkspaces } from '../WorkspaceContext'

const workspace = (name: string): Workspace => ({
  id: 1, name, emoji: null, color: 'indigo', match_mode: 'any', view_mode: 'list', position: 0,
  tag_ids: [1], member_count: 1, unallocated_count: 0,
})

describe('WorkspaceContext', () => {
  it('only the most recently started refresh applies', async () => {
    const first = deferred<Workspace[]>()
    const second = deferred<Workspace[]>()
    getWorkspaces.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const wrapper = ({ children }: { children: ReactNode }) => <WorkspaceProvider>{children}</WorkspaceProvider>
    const { result } = renderHook(() => useWorkspaces(), { wrapper })
    act(() => { void result.current.refreshWorkspaces() })
    await act(async () => { second.resolve([workspace('fresh')]) })
    await act(async () => { first.resolve([workspace('stale')]) })
    expect(result.current.workspaces.map((w) => w.name)).toEqual(['fresh'])
    expect(result.current.loaded).toBe(true)
  })
})
