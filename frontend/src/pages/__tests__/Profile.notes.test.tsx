import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, act, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { Contact, PersonDetail } from '../../api/types'
import { WebSocketClient } from '../../lib/ws'
import { WebSocketProvider } from '../../context/WebSocketContext'
import { ConfirmProvider } from '../../components/ui'

vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  getProfile: vi.fn(),
  getPerson: vi.fn(),
  getPositions: vi.fn(),
  getPersonNotes: vi.fn(),
  getOrganizations: vi.fn(async () => []),
}))

import { getProfile, getPerson, getPositions, getPersonNotes } from '../../api/client'
import Profile from '../Profile'

const person = (positions: PersonDetail['positions'], contact: Contact | null = null): PersonDetail => ({ telegram_id: 42, name: 'Alice', contact, positions })
const contact = (over: Partial<Contact> = {}): Contact => ({
  id: 7, telegram_id: 42, first_name: 'Alice', last_name: null, name: 'Alice', username: null, phone: null,
  twitter_handle: 'alice_old', linkedin_url: null, notes: null, is_important: false, relationship_status: null,
  relationship_status_manual: false, last_interaction_at: null, last_outgoing_at: null, last_incoming_at: null,
  synced_at: null, positions: [], ...over,
})
const pos = (id: number, organization: string) => ({ id, telegram_id: 42, organization, organization_id: id, title: 'CEO', is_active: true } as unknown as PersonDetail['positions'][number])

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getProfile).mockRejectedValue(new Error('offline'))
  vi.mocked(getPersonNotes).mockResolvedValue([])
})
afterEach(() => cleanup())

function setup() {
  const client = new WebSocketClient({ url: () => null })
  render(
    <WebSocketProvider client={client}>
      <ConfirmProvider>
        <MemoryRouter initialEntries={['/profile/42']}>
          <Routes><Route path="/profile/:userId" element={<Profile />} /></Routes>
        </MemoryRouter>
      </ConfirmProvider>
    </WebSocketProvider>,
  )
  return client
}

describe('Profile', () => {
  it('shows the notes timeline for a non-contact person', async () => {
    vi.mocked(getPerson).mockResolvedValue(person([]))
    vi.mocked(getPositions).mockResolvedValue([])
    setup()
    expect(await screen.findByTestId('person-notes')).toBeTruthy()
    // The fetch starts in an effect after the first paint
    await waitFor(() => expect(getPersonNotes).toHaveBeenCalledWith(42, expect.anything()))
  })

  it('reloads local data and positions when an agent changes this person', async () => {
    vi.mocked(getPerson).mockResolvedValue(person([]))
    vi.mocked(getPositions).mockResolvedValue([])
    const client = setup()
    await screen.findByTestId('person-notes')
    const personCalls = vi.mocked(getPerson).mock.calls.length

    act(() => client.dispatch({ type: 'person_changed', telegram_id: 99 }))
    expect(vi.mocked(getPerson).mock.calls.length).toBe(personCalls)

    vi.mocked(getPerson).mockResolvedValue(person([pos(1, 'Acme')]))
    vi.mocked(getPositions).mockResolvedValue([pos(1, 'Acme')])
    act(() => client.dispatch({ type: 'person_changed', telegram_id: 42 }))
    await waitFor(() => expect(vi.mocked(getPerson).mock.calls.length).toBeGreaterThan(personCalls))
    expect((await screen.findAllByText(/Acme/)).length).toBeGreaterThan(0)
  })

  it('an open field edit survives an agent change; the new value shows once the edit closes', async () => {
    vi.mocked(getPerson).mockResolvedValue(person([], contact()))
    vi.mocked(getPositions).mockResolvedValue([])
    const client = setup()
    expect(await screen.findByText('alice_old')).toBeTruthy()

    fireEvent.click(screen.getByTitle('Edit twitter'))
    const input = screen.getByLabelText('Twitter') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'my_draft' } })

    vi.mocked(getPerson).mockResolvedValue(person([], contact({ twitter_handle: 'agent_set' })))
    await act(async () => { client.dispatch({ type: 'person_changed', telegram_id: 42 }) })
    await waitFor(() => expect(vi.mocked(getPerson).mock.calls.length).toBeGreaterThan(1))
    // Same input element, draft untouched (no remount)
    expect(screen.getByLabelText('Twitter')).toBe(input)
    expect(input.value).toBe('my_draft')

    fireEvent.keyDown(input, { key: 'Escape' })
    expect(await screen.findByText('agent_set')).toBeTruthy()
  })

  it('an open add-position form survives an agent change while the list refreshes in place', async () => {
    vi.mocked(getPerson).mockResolvedValue(person([]))
    vi.mocked(getPositions).mockResolvedValue([])
    const client = setup()
    await screen.findByText('No positions added')
    fireEvent.click(screen.getByRole('button', { name: /^add$/i }))
    const title = screen.getByPlaceholderText('Title (optional)...') as HTMLInputElement
    fireEvent.change(title, { target: { value: 'CTO draft' } })

    vi.mocked(getPositions).mockResolvedValue([pos(1, 'Acme')])
    vi.mocked(getPerson).mockResolvedValue(person([pos(1, 'Acme')]))
    await act(async () => { client.dispatch({ type: 'person_changed', telegram_id: 42 }) })
    expect((await screen.findAllByText(/Acme/)).length).toBeGreaterThan(0)
    expect(screen.getByPlaceholderText('Title (optional)...')).toBe(title)
    expect(title.value).toBe('CTO draft')
  })
})
