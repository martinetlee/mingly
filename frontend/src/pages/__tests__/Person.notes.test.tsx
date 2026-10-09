import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, act, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { ContactPosition, Person as PersonT } from '../../api/types'
import { makePerson } from '../chats/__tests__/fixtures'
import { WebSocketClient } from '../../lib/ws'
import { WebSocketProvider } from '../../context/WebSocketContext'
import { ConfirmProvider } from '../../components/ui'

vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  getTelegramProfile: vi.fn(),
  getPerson: vi.fn(),
  getPositions: vi.fn(),
  getPersonNotes: vi.fn(),
  getOrganizations: vi.fn(async () => []),
}))

import { getTelegramProfile, getPerson, getPositions, getPersonNotes } from '../../api/client'
import Person from '../Person'

const person = (positions: ContactPosition[], over: Partial<PersonT> = {}): PersonT =>
  makePerson(42, 'Alice', { is_contact: false, positions, ...over })
const pos = (id: number, organization: string): ContactPosition => ({ id, person_id: 42, organization, organization_id: id, title: 'CEO', is_active: true, ended_at: null })

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getTelegramProfile).mockRejectedValue(new Error('offline'))
  vi.mocked(getPersonNotes).mockResolvedValue([])
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

describe('Person page', () => {
  it('shows the notes timeline for a non-contact person', async () => {
    vi.mocked(getPerson).mockResolvedValue(person([]))
    vi.mocked(getPositions).mockResolvedValue([])
    setup()
    expect(await screen.findByTestId('person-notes')).toBeTruthy()
    // The fetch starts in an effect after the first paint
    await waitFor(() => expect(getPersonNotes).toHaveBeenCalledWith(42, expect.anything()))
  })

  it('lists every identity and keeps local data when the live Telegram profile is offline', async () => {
    vi.mocked(getPerson).mockResolvedValue(person([], { twitter_handle: 'alice_old' }))
    vi.mocked(getPositions).mockResolvedValue([])
    setup()
    expect(await screen.findByText('alice_old')).toBeTruthy()
    expect(screen.getAllByTestId('identity')).toHaveLength(1)
    // Live profile is fetched for the Telegram identity (makePerson: id + 1000)
    await waitFor(() => expect(getTelegramProfile).toHaveBeenCalledWith(1042, expect.anything()))
    expect(await screen.findByText(/Telegram offline/)).toBeTruthy()
  })

  it('reloads local data and positions when an agent changes this person', async () => {
    vi.mocked(getPerson).mockResolvedValue(person([]))
    vi.mocked(getPositions).mockResolvedValue([])
    const client = setup()
    await screen.findByTestId('person-notes')
    const personCalls = vi.mocked(getPerson).mock.calls.length

    act(() => client.dispatch({ type: 'person_changed', person_id: 99 }))
    expect(vi.mocked(getPerson).mock.calls.length).toBe(personCalls)

    vi.mocked(getPerson).mockResolvedValue(person([pos(1, 'Acme')]))
    vi.mocked(getPositions).mockResolvedValue([pos(1, 'Acme')])
    act(() => client.dispatch({ type: 'person_changed', person_id: 42 }))
    await waitFor(() => expect(vi.mocked(getPerson).mock.calls.length).toBeGreaterThan(personCalls))
    expect((await screen.findAllByText(/Acme/)).length).toBeGreaterThan(0)
  })

  it('an open field edit survives an agent change; the new value shows once the edit closes', async () => {
    vi.mocked(getPerson).mockResolvedValue(person([], { twitter_handle: 'alice_old' }))
    vi.mocked(getPositions).mockResolvedValue([])
    const client = setup()
    expect(await screen.findByText('alice_old')).toBeTruthy()

    fireEvent.click(screen.getByTitle('Edit twitter'))
    const input = screen.getByLabelText('Twitter') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'my_draft' } })

    vi.mocked(getPerson).mockResolvedValue(person([], { twitter_handle: 'agent_set' }))
    await act(async () => { client.dispatch({ type: 'person_changed', person_id: 42 }) })
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
    await act(async () => { client.dispatch({ type: 'person_changed', person_id: 42 }) })
    expect((await screen.findAllByText(/Acme/)).length).toBeGreaterThan(0)
    expect(screen.getByPlaceholderText('Title (optional)...')).toBe(title)
    expect(title.value).toBe('CTO draft')
  })
})
