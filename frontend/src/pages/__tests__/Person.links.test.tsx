import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, act, waitFor, fireEvent, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { Person as PersonT } from '../../api/types'
import type { MergeSuggestion, PersonMergeRecord } from '../../api/peopleLinks'
import { makePerson } from '../chats/__tests__/fixtures'
import { WebSocketClient } from '../../lib/ws'
import { WebSocketProvider } from '../../context/WebSocketContext'
import { ConfirmProvider } from '../../components/ui'
import { resetToasts } from '../../lib/toast'

vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  getTelegramProfile: vi.fn(),
  getPerson: vi.fn(),
  getPositions: vi.fn(async () => []),
  getPersonNotes: vi.fn(async () => []),
  getOrganizations: vi.fn(async () => []),
}))
vi.mock('../../api/peopleLinks', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/peopleLinks')>()),
  getPersonMergeSuggestions: vi.fn(),
  getPersonMerges: vi.fn(),
  mergePerson: vi.fn(),
  dismissMergeSuggestion: vi.fn(),
  undoPersonMerge: vi.fn(),
  detachIdentity: vi.fn(),
}))

import { getTelegramProfile, getPerson } from '../../api/client'
import {
  getPersonMergeSuggestions, getPersonMerges, mergePerson, dismissMergeSuggestion, undoPersonMerge, detachIdentity,
} from '../../api/peopleLinks'
import Person from '../Person'

const signalIdentity = {
  id: 2001, person_id: 42, channel: 'signal', external_id: 'aci', display_name: 'Alice S',
  username: null, phone: '+15551234567', avatar_url: null, is_contact: false,
}
const alice = (over: Partial<PersonT> = {}) => makePerson(42, 'Alice', over)
const twoAccounts = () => alice({ identities: [...alice().identities, signalIdentity] })
const bob = makePerson(7, 'Alice Work')
const suggestion: MergeSuggestion = {
  id: 5, reason: 'same_name_shared_group', score: 0.6, created_at: null, person_a: bob, person_b: alice(),
}
const mergeRecord: PersonMergeRecord = {
  id: 9, into_person_id: 42, from_person_id: 43, from_name: 'Alice S',
  identities: [{ id: 2001, channel: 'signal', label: 'Alice S' }], automatic: true, created_at: null, undone_at: null,
}

beforeEach(() => {
  vi.clearAllMocks()
  resetToasts()
  vi.mocked(getTelegramProfile).mockRejectedValue(new Error('offline'))
  vi.mocked(getPersonMergeSuggestions).mockResolvedValue([])
  vi.mocked(getPersonMerges).mockResolvedValue([])
})
afterEach(() => cleanup())

function setup() {
  const client = new WebSocketClient({ url: () => null })
  render(
    <WebSocketProvider client={client}>
      <ConfirmProvider>
        <MemoryRouter initialEntries={['/people/42']}>
          <Routes><Route path="/people/:personId" element={<Person />} /></Routes>
        </MemoryRouter>
      </ConfirmProvider>
    </WebSocketProvider>,
  )
  return client
}

const confirmWith = async (label: string) => {
  const dialog = await screen.findByRole('dialog')
  fireEvent.click(within(dialog).getByRole('button', { name: label }))
}

describe('Person page — linked accounts', () => {
  it('lists linked accounts; a single account cannot be detached', async () => {
    vi.mocked(getPerson).mockResolvedValue(alice())
    setup()
    const section = await screen.findByTestId('linked-accounts')
    expect(within(section).getAllByTestId('identity')).toHaveLength(1)
    expect(within(section).queryByRole('button', { name: /Detach/ })).toBeNull()
    expect(screen.queryByTestId('merge-suggestions')).toBeNull()
  })

  it('merges a suggested duplicate after confirming', async () => {
    vi.mocked(getPerson).mockResolvedValue(alice())
    vi.mocked(getPersonMergeSuggestions).mockResolvedValue([suggestion])
    vi.mocked(mergePerson).mockResolvedValue({ ...twoAccounts(), merge_id: 11 })
    setup()
    const list = await screen.findByTestId('merge-suggestions')
    expect(within(list).getByText('Alice Work')).toBeTruthy()
    expect(within(list).getByText('Same name in a shared group')).toBeTruthy()

    vi.mocked(getPersonMergeSuggestions).mockResolvedValue([])
    fireEvent.click(within(list).getByRole('button', { name: 'Merge' }))
    await confirmWith('Merge')
    await waitFor(() => expect(mergePerson).toHaveBeenCalledWith(42, 7))
    await waitFor(() => expect(screen.getAllByTestId('identity')).toHaveLength(2))
    await waitFor(() => expect(screen.queryByTestId('merge-suggestions')).toBeNull())
  })

  it('dismisses a suggestion', async () => {
    vi.mocked(getPerson).mockResolvedValue(alice())
    vi.mocked(getPersonMergeSuggestions).mockResolvedValue([suggestion])
    vi.mocked(dismissMergeSuggestion).mockResolvedValue({ success: true })
    setup()
    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss Alice Work' }))
    await waitFor(() => expect(screen.queryByTestId('merge-suggestions')).toBeNull())
    expect(dismissMergeSuggestion).toHaveBeenCalledWith(5)
  })

  it('detaches an account and reloads the person', async () => {
    vi.mocked(getPerson).mockResolvedValue(twoAccounts())
    vi.mocked(detachIdentity).mockResolvedValue(makePerson(50, 'Alice S'))
    setup()
    fireEvent.click(await screen.findByRole('button', { name: 'Detach Alice S' }))
    vi.mocked(getPerson).mockResolvedValue(alice())
    await confirmWith('Detach')
    await waitFor(() => expect(detachIdentity).toHaveBeenCalledWith(42, 2001))
    await waitFor(() => expect(screen.getAllByTestId('identity')).toHaveLength(1))
  })

  it('undoes a recent merge and refreshes when the person changes', async () => {
    vi.mocked(getPerson).mockResolvedValue(twoAccounts())
    vi.mocked(getPersonMerges).mockResolvedValue([mergeRecord])
    vi.mocked(undoPersonMerge).mockResolvedValue(makePerson(60, 'Alice S'))
    const client = setup()
    const merges = await screen.findByTestId('recent-merges')
    expect(within(merges).getByText(/linked automatically/)).toBeTruthy()

    vi.mocked(getPerson).mockResolvedValue(alice())
    vi.mocked(getPersonMerges).mockResolvedValue([])
    fireEvent.click(within(merges).getByRole('button', { name: /Undo/ }))
    await confirmWith('Undo merge')
    await waitFor(() => expect(undoPersonMerge).toHaveBeenCalledWith(9))
    await waitFor(() => expect(screen.queryByTestId('recent-merges')).toBeNull())
    expect(screen.getAllByTestId('identity')).toHaveLength(1)

    const calls = vi.mocked(getPersonMergeSuggestions).mock.calls.length
    vi.mocked(getPersonMergeSuggestions).mockResolvedValue([suggestion])
    act(() => client.dispatch({ type: 'person_changed', person_id: 42 }))
    await waitFor(() => expect(vi.mocked(getPersonMergeSuggestions).mock.calls.length).toBeGreaterThan(calls))
    expect(await screen.findByTestId('merge-suggestions')).toBeTruthy()
  })
})
