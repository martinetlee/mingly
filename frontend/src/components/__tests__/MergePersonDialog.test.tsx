import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import type { Person } from '../../api/types'

vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  getPeoplePage: vi.fn(),
}))
vi.mock('../../api/peopleLinks', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/peopleLinks')>()),
  mergePerson: vi.fn(),
}))

import { getPeoplePage, ApiError } from '../../api/client'
import { mergePerson } from '../../api/peopleLinks'
import MergePersonDialog from '../MergePersonDialog'

const person = (id: number, name: string, over: Partial<Person> = {}): Person => ({
  id, name, display_name: null, is_contact: false, is_important: false, is_self: false,
  identities: [{
    id: id * 10, person_id: id, channel: 'telegram', external_id: String(id), display_name: name,
    username: null, phone: null, avatar_url: null, is_contact: false,
  }],
  twitter_handle: null, linkedin_url: null, notes: null, relationship_status: null,
  relationship_status_manual: false, last_interaction_at: null, last_outgoing_at: null, last_incoming_at: null,
  ...over,
})

const page = (items: Person[]) => ({ items, total: items.length, limit: 20, offset: 0, status_counts: {} })

beforeEach(() => vi.clearAllMocks())
afterEach(() => cleanup())

describe('MergePersonDialog', () => {
  const ann = person(1, 'Ann')

  it('searches people, hides this person and the user, and merges the pick into this person', async () => {
    vi.mocked(getPeoplePage).mockResolvedValue(page([ann, person(2, 'Ann Signal'), person(3, 'Me', { is_self: true })]))
    vi.mocked(mergePerson).mockResolvedValue({ ...ann, merge_id: 7 })
    const onMerged = vi.fn()
    render(<MergePersonDialog person={ann} onClose={() => {}} onMerged={onMerged} />)

    expect((screen.getByRole('button', { name: 'Merge' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Search people'), { target: { value: 'ann' } })
    const option = await screen.findByRole('option', { name: /Ann Signal/ })
    expect(screen.getAllByRole('option')).toHaveLength(1)  // not Ann herself, not the user
    expect(vi.mocked(getPeoplePage).mock.calls[0][0]).toEqual({ search: 'ann', limit: 20 })

    fireEvent.click(option)
    expect(screen.getByText(/into/, { selector: 'p' }).textContent).toBe('Merge Ann Signal (1 linked account) into Ann.')
    fireEvent.click(screen.getByRole('button', { name: 'Merge' }))
    await waitFor(() => expect(onMerged).toHaveBeenCalledTimes(1))
    expect(mergePerson).toHaveBeenCalledWith(1, 2)
    expect(onMerged.mock.calls[0][0]).toMatchObject({ id: 1, merge_id: 7 })
    expect(onMerged.mock.calls[0][1]).toMatchObject({ id: 2, name: 'Ann Signal' })
  })

  it('shows a refused merge inline', async () => {
    vi.mocked(getPeoplePage).mockResolvedValue(page([person(2, 'Bob')]))
    vi.mocked(mergePerson).mockRejectedValue(new ApiError('Your own profile can\'t be merged with someone else', { status: 400, code: 'http', path: '/api/people/1/merge' }))
    const onMerged = vi.fn()
    render(<MergePersonDialog person={ann} onClose={() => {}} onMerged={onMerged} />)
    fireEvent.change(screen.getByLabelText('Search people'), { target: { value: 'bob' } })
    fireEvent.click(await screen.findByRole('option', { name: /Bob/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Merge' }))
    expect((await screen.findByRole('alert')).textContent).toContain('can\'t be merged')
    expect(onMerged).not.toHaveBeenCalled()
  })
})
