/**
 * Ids saved before the v2 database reset name other rows: they are dropped,
 * and the active workspace is only restored from a v2 key.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'

vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  getWorkspaces: vi.fn(),
}))
vi.mock('../../hooks/useWebSocket', () => ({ useWsEvent: () => {}, useWebSocket: () => {} }))

import { getWorkspaces } from '../../api/client'
import { WorkspaceProvider, useWorkspaces } from '../../context/WorkspaceContext'
import { dropLegacyIdKeys, idKey, readStoredId, storeId } from '../idStorage'

function Active() {
  const { activeWorkspace, loaded } = useWorkspaces()
  return <div data-testid="active">{loaded ? activeWorkspace?.name ?? 'none' : 'loading'}</div>
}

beforeEach(() => {
  localStorage.clear()
  vi.mocked(getWorkspaces).mockResolvedValue([{ id: 1, name: 'Work' } as never])
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('id storage', () => {
  it('drops pre-v2 id keys and keeps everything else', () => {
    localStorage.setItem('activeWorkspaceId', '1')
    localStorage.setItem('activeBoard:1', '4')
    localStorage.setItem('mingly_chats_channel', 'signal')
    dropLegacyIdKeys()
    expect(localStorage.getItem('activeWorkspaceId')).toBeNull()
    expect(localStorage.getItem('activeBoard:1')).toBeNull()
    expect(localStorage.getItem('mingly_chats_channel')).toBe('signal')
  })

  it('round-trips ids under a generation key', () => {
    storeId('activeBoard:3', 9)
    expect(localStorage.getItem(idKey('activeBoard:3'))).toBe('9')
    expect(readStoredId('activeBoard:3')).toBe(9)
    storeId('activeBoard:3', null)
    expect(readStoredId('activeBoard:3')).toBeNull()
  })

  it('a pre-v2 active workspace id does not select the new workspace with that id', async () => {
    localStorage.setItem('activeWorkspaceId', '1')
    render(<WorkspaceProvider><Active /></WorkspaceProvider>)
    await waitFor(() => expect(screen.getByTestId('active').textContent).toBe('none'))
  })

  it('restores a v2 active workspace', async () => {
    storeId('activeWorkspaceId', 1)
    render(<WorkspaceProvider><Active /></WorkspaceProvider>)
    await waitFor(() => expect(screen.getByTestId('active').textContent).toBe('Work'))
  })
})
