import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useState } from 'react'
import { render, screen, fireEvent, act, cleanup, waitFor } from '@testing-library/react'
import type { ContactPosition, OrgSummary, Person, Workspace } from '../../api/types'
import { makePerson } from '../../pages/chats/__tests__/fixtures'
import { ConfirmProvider } from '../ui/ConfirmDialog'
import { resetToasts } from '../../lib/toast'

function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const api = vi.hoisted(() => ({
  getPositions: vi.fn(),
  addPosition: vi.fn(),
  removePosition: vi.fn(),
  updatePosition: vi.fn(),
  updatePerson: vi.fn(),
  getOrganizations: vi.fn(),
  getTags: vi.fn(),
  updateWorkspace: vi.fn(),
  createWorkspace: vi.fn(),
  deleteWorkspace: vi.fn(),
  addBoardColumn: vi.fn(),
  getPerson: vi.fn(),
}))

vi.mock('../../api/client', () => {
  const fwd = (k: keyof typeof api) => (...a: unknown[]) => api[k](...a)
  return {
    getPositions: fwd('getPositions'),
    addPosition: fwd('addPosition'),
    removePosition: fwd('removePosition'),
    updatePosition: fwd('updatePosition'),
    updatePerson: fwd('updatePerson'),
    getOrganizations: fwd('getOrganizations'),
    getTags: fwd('getTags'),
    updateWorkspace: fwd('updateWorkspace'),
    createWorkspace: fwd('createWorkspace'),
    deleteWorkspace: fwd('deleteWorkspace'),
    addBoardColumn: fwd('addBoardColumn'),
    getPerson: fwd('getPerson'),
  }
})

import PositionsEditor from '../PositionsEditor'
import ContactInfoSection, { EditableField } from '../ContactInfoSection'
import ComboInput from '../ComboInput'
import WorkspaceDialog from '../WorkspaceDialog'

const pos = (id: number, organization: string, extra: Partial<ContactPosition> = {}): ContactPosition => ({
  id, organization, organization_id: id, title: null, is_active: true, ended_at: null, ...extra,
})

const person = (extra: Partial<Person> = {}): Person => makePerson(100, 'Ann', extra)

/** Click the Confirm/Remove button of the shared confirm dialog. */
async function acceptConfirm(label: string) {
  const btn = await screen.findByRole('button', { name: label })
  await act(async () => { fireEvent.click(btn) })
}

beforeEach(() => {
  vi.clearAllMocks()
  resetToasts()
  api.getPerson.mockImplementation(async (id: number) => makePerson(id, `P${id}`))
})
afterEach(() => cleanup())

describe('PositionsEditor', () => {
  it('two quick deletes both stick — the first completion cannot resurrect the second', async () => {
    api.getPositions.mockResolvedValue([pos(1, 'Acme'), pos(2, 'Beta')])
    const delA = deferred<{ success: boolean }>()
    const delB = deferred<{ success: boolean }>()
    api.removePosition.mockImplementation((_t: number, id: number) => (id === 1 ? delA.promise : delB.promise))
    const onChange = vi.fn()
    render(<ConfirmProvider><PositionsEditor personId={100} onChange={onChange} /></ConfirmProvider>)
    await screen.findByText('Acme')

    fireEvent.click(screen.getAllByTitle('Remove position')[0]) // Acme
    await acceptConfirm('Remove')
    // Acme's row is busy (spinner): the only remove button left is Beta's
    const remaining = screen.getAllByTitle('Remove position')
    expect(remaining).toHaveLength(1)
    fireEvent.click(remaining[0])
    await acceptConfirm('Remove')

    // B completes first, then A
    await act(async () => { delB.resolve({ success: true }) })
    await act(async () => { delA.resolve({ success: true }) })
    expect(screen.queryByText('Acme')).toBeNull()
    expect(screen.queryByText('Beta')).toBeNull()
    expect(onChange).toHaveBeenLastCalledWith([])
  })

  it('a failed add keeps the form open with the error and what was typed', async () => {
    api.getPositions.mockResolvedValue([])
    api.getOrganizations.mockResolvedValue([])
    api.addPosition.mockRejectedValue(new Error('This person already has a position at "Acme"'))
    render(<ConfirmProvider><PositionsEditor personId={100} /></ConfirmProvider>)
    await screen.findByText('No positions added')
    fireEvent.click(screen.getByRole('button', { name: /Add/ }))
    fireEvent.change(screen.getByPlaceholderText('Organization name...'), { target: { value: 'Acme' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Add Position' })) })
    expect((await screen.findByRole('alert')).textContent).toContain('already has a position')
    expect((screen.getByPlaceholderText('Organization name...') as HTMLInputElement).value).toBe('Acme')
  })

  it('switching person drops a completion for the previous person', async () => {
    api.getPositions.mockImplementation(async (pid: number) => (pid === 100 ? [] : [pos(9, 'Zed')]))
    api.getOrganizations.mockResolvedValue([])
    const add = deferred<ContactPosition>()
    api.addPosition.mockReturnValue(add.promise)
    const onChange = vi.fn()
    const { rerender } = render(<ConfirmProvider><PositionsEditor personId={100} onChange={onChange} /></ConfirmProvider>)
    await screen.findByText('No positions added')
    fireEvent.click(screen.getByRole('button', { name: /Add/ }))
    fireEvent.change(screen.getByPlaceholderText('Organization name...'), { target: { value: 'Acme' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add Position' }))
    rerender(<ConfirmProvider><PositionsEditor personId={200} onChange={onChange} /></ConfirmProvider>)
    await screen.findByText('Zed')
    await act(async () => { add.resolve(pos(5, 'Acme')) })
    expect(screen.queryByText('Acme')).toBeNull()
    expect(onChange).not.toHaveBeenCalled()
  })
})

describe('ContactInfoSection', () => {
  it('a position change merges into the latest person — it never reverts a concurrent notes edit', async () => {
    api.getPositions.mockResolvedValue([])
    api.getOrganizations.mockResolvedValue([])
    const add = deferred<ContactPosition>()
    api.addPosition.mockReturnValue(add.promise)
    const changes: Person[] = []
    function Host() {
      const [c, setC] = useState(person())
      return (
        <ConfirmProvider>
          <button onClick={() => setC((p) => ({ ...p, notes: 'new notes' }))}>external-notes-edit</button>
          <ContactInfoSection person={c} onPersonChange={(u) => { changes.push(u); setC(u) }} />
        </ConfirmProvider>
      )
    }
    render(<Host />)
    await screen.findByText('No positions added')
    fireEvent.click(screen.getByRole('button', { name: /^Add$/ }))
    fireEvent.change(screen.getByPlaceholderText('Organization name...'), { target: { value: 'Acme' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add Position' }))
    // Notes saved while the add is in flight
    fireEvent.click(screen.getByText('external-notes-edit'))
    await act(async () => { add.resolve(pos(5, 'Acme')) })
    const last = changes[changes.length - 1]
    expect(last.notes).toBe('new notes')
    expect((last.positions ?? []).map((p: ContactPosition) => p.organization)).toEqual(['Acme'])
  })
})

describe('EditableField', () => {
  it('stays in edit mode with the draft and an error when the save fails', async () => {
    const onSave = vi.fn().mockRejectedValueOnce(new Error('phone: invalid')).mockResolvedValueOnce(undefined)
    render(<EditableField icon={null} label="Phone" value={null} placeholder="Add phone..." onSave={onSave} />)
    fireEvent.click(screen.getByTitle('Edit phone'))
    const input = screen.getByLabelText('Phone')
    fireEvent.change(input, { target: { value: '+1 555' } })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })
    expect((screen.getByRole('alert')).textContent).toContain('phone: invalid')
    expect((screen.getByLabelText('Phone') as HTMLInputElement).value).toBe('+1 555')
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Retry' })) })
    expect(onSave).toHaveBeenCalledTimes(2)
    expect(screen.queryByLabelText('Phone')).toBeNull()
  })
})

describe('ComboInput', () => {
  it('does not reopen after picking a suggestion, and ignores out-of-order results', async () => {
    const slowA = deferred<OrgSummary[]>()
    api.getOrganizations.mockImplementation((q: string) =>
      q === 'A' ? slowA.promise : Promise.resolve([{ id: 1, name: 'Acme', context: null, member_count: 2, former_member_count: 0 }]))
    function Host() {
      const [v, setV] = useState('')
      return <ComboInput value={v} onChange={setV} />
    }
    render(<Host />)
    const input = screen.getByPlaceholderText('Organization name...')
    fireEvent.change(input, { target: { value: 'A' } })
    await new Promise((r) => setTimeout(r, 200))
    fireEvent.change(input, { target: { value: 'Ac' } })
    const option = await screen.findByText('Acme')
    // The stale "A" response lands after "Ac"
    await act(async () => { slowA.resolve([{ id: 2, name: 'Alpha', context: null, member_count: 0, former_member_count: 0 }]) })
    expect(screen.queryByText('Alpha')).toBeNull()

    fireEvent.mouseDown(option)
    expect((input as HTMLInputElement).value).toBe('Acme')
    await new Promise((r) => setTimeout(r, 250))
    expect(screen.queryByText('Acme')).toBeNull() // dropdown stayed closed
  })
})

describe('WorkspaceDialog', () => {
  const ws = (extra: Partial<Workspace> = {}): Workspace => ({
    id: 3, name: 'ETHTaipei', emoji: '🎪', color: 'indigo', match_mode: 'any', view_mode: 'kanban',
    position: 0, tag_ids: [1], member_count: 4, unallocated_count: 0, ...extra,
  })

  it('a refreshed workspace object (new badge counts) does not reset the draft', async () => {
    api.getTags.mockResolvedValue([{ id: 1, name: 'eth', color: 'slate', category: null, is_default: false }])
    const props = { open: true, onClose: () => {}, onSaved: () => {} }
    const { rerender } = render(<ConfirmProvider><WorkspaceDialog {...props} workspace={ws()} /></ConfirmProvider>)
    const name = screen.getByPlaceholderText('Workspace name (e.g. ETHTaipei)')
    fireEvent.change(name, { target: { value: 'Renamed' } })
    rerender(<ConfirmProvider><WorkspaceDialog {...props} workspace={ws({ member_count: 5, unallocated_count: 1 })} /></ConfirmProvider>)
    expect((screen.getByPlaceholderText('Workspace name (e.g. ETHTaipei)') as HTMLInputElement).value).toBe('Renamed')
  })

  it('clearing the emoji sends emoji: null', async () => {
    api.getTags.mockResolvedValue([{ id: 1, name: 'eth', color: 'slate', category: null, is_default: false }])
    api.updateWorkspace.mockResolvedValue({ status: 'ok' })
    const onSaved = vi.fn()
    render(<ConfirmProvider><WorkspaceDialog open onClose={() => {}} onSaved={onSaved} workspace={ws()} /></ConfirmProvider>)
    fireEvent.change(screen.getByLabelText('Emoji'), { target: { value: '' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save' })) })
    expect(api.updateWorkspace).toHaveBeenCalledWith(3, expect.objectContaining({ emoji: null }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(3, 'kanban'))
  })
})
