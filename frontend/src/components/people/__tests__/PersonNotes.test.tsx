import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, act, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import type { PersonNote } from '../../../api/types'
import { WebSocketClient } from '../../../lib/ws'
import { WebSocketProvider } from '../../../context/WebSocketContext'
import { ConfirmProvider } from '../../ui'
import { resetToasts } from '../../../lib/toast'

vi.mock('../../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../api/client')>()),
  getPersonNotes: vi.fn(),
  createPersonNote: vi.fn(),
  updatePersonNote: vi.fn(),
  deletePersonNote: vi.fn(),
}))

import { getPersonNotes, createPersonNote, updatePersonNote, deletePersonNote } from '../../../api/client'
import PersonNotes, { sortNotes } from '../PersonNotes'

const note = (id: number, over: Partial<PersonNote> = {}): PersonNote => ({
  id, person_id: 42, kind: 'context', title: `Note ${id}`, body: `Body **${id}**`, citations: [],
  author: 'user', agent_label: null,
  created_at: `2026-10-0${id}T10:00:00`, updated_at: `2026-10-0${id}T10:00:00`, ...over,
})

function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((res) => { resolve = res })
  return { promise, resolve }
}

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="location">{loc.pathname}{loc.search}</div>
}

function setup(personId = 42) {
  const client = new WebSocketClient({ url: () => null })
  const ui = (pid: number) => (
    <WebSocketProvider client={client}>
      <ConfirmProvider>
        <MemoryRouter initialEntries={['/people/42']}>
          <Routes>
            <Route path="/people/:id" element={<PersonNotes personId={pid} />} />
            <Route path="/chats" element={<LocationProbe />} />
            <Route path="/open/:conversationId" element={<LocationProbe />} />
          </Routes>
        </MemoryRouter>
      </ConfirmProvider>
    </WebSocketProvider>
  )
  const r = render(ui(personId))
  return { client, rerender: (pid: number) => r.rerender(ui(pid)) }
}

beforeEach(() => {
  vi.clearAllMocks()
  resetToasts()
})
afterEach(() => cleanup())

describe('PersonNotes', () => {
  it('lists notes newest first with kind/author badges and markdown bodies', async () => {
    vi.mocked(getPersonNotes).mockResolvedValue([
      note(1),
      note(3, { kind: 'risk', author: 'agent', agent_label: 'Claude Code' }),
      note(2, { kind: 'due_diligence' }),
    ])
    setup()
    const items = await screen.findAllByTestId('person-note')
    expect(items.map((li) => within(li).getByText(/^Note \d$/).textContent)).toEqual(['Note 3', 'Note 2', 'Note 1'])
    expect(within(items[0]).getByText('Risk')).toBeTruthy()
    expect(within(items[0]).getByText('Agent — Claude Code')).toBeTruthy()
    expect(within(items[1]).getByText('Due diligence')).toBeTruthy()
    expect(within(items[1]).getByText('You')).toBeTruthy()
    // Markdown rendered, not raw
    expect(within(items[0]).getByText('3').tagName).toBe('STRONG')
  })

  it('sortNotes breaks created_at ties by id', () => {
    const a = note(1, { created_at: '2026-10-01T10:00:00' })
    const b = note(2, { created_at: '2026-10-01T10:00:00' })
    expect(sortNotes([a, b]).map((n) => n.id)).toEqual([2, 1])
  })

  it('adds a note', async () => {
    vi.mocked(getPersonNotes).mockResolvedValue([])
    setup()
    fireEvent.click(await screen.findByRole('button', { name: /add note/i }))
    const form = screen.getByRole('form', { name: 'New note' })
    fireEvent.change(within(form).getByLabelText('Kind'), { target: { value: 'todo' } })
    fireEvent.change(within(form).getByLabelText('Title'), { target: { value: 'Follow up' } })
    fireEvent.change(within(form).getByLabelText('Body'), { target: { value: 'Send the deck' } })
    vi.mocked(createPersonNote).mockResolvedValue(note(5, { kind: 'todo', title: 'Follow up', body: 'Send the deck' }))
    vi.mocked(getPersonNotes).mockResolvedValue([note(5, { kind: 'todo', title: 'Follow up', body: 'Send the deck' })])
    fireEvent.click(within(form).getByRole('button', { name: 'Add note' }))
    await waitFor(() => expect(createPersonNote).toHaveBeenCalledWith(42, { kind: 'todo', title: 'Follow up', body: 'Send the deck' }))
    expect(await screen.findByText('Follow up')).toBeTruthy()
    expect(screen.queryByRole('form', { name: 'New note' })).toBeNull()
  })

  it('edits an agent note', async () => {
    vi.mocked(getPersonNotes).mockResolvedValue([note(1, { author: 'agent', agent_label: 'bot' })])
    setup()
    fireEvent.click(await screen.findByRole('button', { name: 'Edit note Note 1' }))
    const form = screen.getByRole('form', { name: 'Edit note' })
    fireEvent.change(within(form).getByLabelText('Title'), { target: { value: 'Renamed' } })
    const saved = note(1, { author: 'agent', agent_label: 'bot', title: 'Renamed', updated_at: '2026-10-05T00:00:00' })
    vi.mocked(updatePersonNote).mockResolvedValue(saved)
    vi.mocked(getPersonNotes).mockResolvedValue([saved])
    fireEvent.click(within(form).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(updatePersonNote).toHaveBeenCalledWith(42, 1, { kind: 'context', title: 'Renamed', body: 'Body **1**' }))
    expect(await screen.findByText('Renamed')).toBeTruthy()
  })

  it('deletes only after confirming', async () => {
    vi.mocked(getPersonNotes).mockResolvedValue([note(1)])
    setup()
    fireEvent.click(await screen.findByRole('button', { name: 'Delete note Note 1' }))
    expect(deletePersonNote).not.toHaveBeenCalled()
    expect(screen.getByText('Delete this note?')).toBeTruthy()
    vi.mocked(deletePersonNote).mockResolvedValue({ success: true })
    vi.mocked(getPersonNotes).mockResolvedValue([])
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(deletePersonNote).toHaveBeenCalledWith(42, 1))
    await waitFor(() => expect(screen.queryByText('Note 1')).toBeNull())
  })

  it('citation chips open the cited message in its conversation', async () => {
    vi.mocked(getPersonNotes).mockResolvedValue([note(1, { citations: [{ conversation_id: 42, message_id: 900 }] })])
    setup()
    fireEvent.click(await screen.findByRole('button', { name: /source 1/i }))
    expect(screen.getByTestId('location').textContent).toBe('/open/42?messageId=900') // resolved to Chats or Mail by kind
  })

  it('refetches on person_notes_changed for this person only', async () => {
    vi.mocked(getPersonNotes).mockResolvedValue([note(1)])
    const { client } = setup()
    await screen.findByText('Note 1')
    expect(getPersonNotes).toHaveBeenCalledTimes(1)
    act(() => client.dispatch({ type: 'person_notes_changed', person_id: 99 }))
    expect(getPersonNotes).toHaveBeenCalledTimes(1)
    vi.mocked(getPersonNotes).mockResolvedValue([note(1), note(2, { author: 'agent', title: 'From agent' })])
    act(() => client.dispatch({ type: 'person_notes_changed', person_id: 42 }))
    expect(await screen.findByText('From agent')).toBeTruthy()
  })

  it('switching person shows only the latest person’s notes, even if the old fetch resolves last', async () => {
    const first = deferred<PersonNote[]>()
    const second = deferred<PersonNote[]>()
    vi.mocked(getPersonNotes).mockImplementation((pid) => (pid === 42 ? first.promise : second.promise))
    const { rerender } = setup(42)
    rerender(77)
    await act(async () => { second.resolve([note(1, { person_id: 77, title: 'Bob note' })]) })
    await act(async () => { first.resolve([note(1, { title: 'Alice note' })]) })
    expect(screen.getByText('Bob note')).toBeTruthy()
    expect(screen.queryByText('Alice note')).toBeNull()
  })
})
