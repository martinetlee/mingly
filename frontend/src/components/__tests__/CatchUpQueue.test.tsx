import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { CatchUpItem, DashboardData, DashboardNeedsReply } from '../../api/types'

vi.mock('../../api/client', () => ({}))

import { applyTriageUpdate, NoteEditor, TriageCard } from '../CatchUpQueue'
import { makeConversation } from '../../test/fixtures'

const item = (id: number, date: string, triage: CatchUpItem | null = null): DashboardNeedsReply => ({
  conversation: makeConversation(id, `Chat ${id}`, { kind: 'direct' }),
  last_message: { sender_name: 'X', text: 'hi', date },
  triage,
  context: null,
})
const triage = (id: number, status: CatchUpItem['status'], extra: Partial<CatchUpItem> = {}): CatchUpItem => ({
  id, conversation_id: id, person_id: null, status, note: null, snoozed_until: null,
  handled_at: null, created_at: null, updated_at: null, ...extra,
})
const dash = (needs: DashboardNeedsReply[], handled: DashboardNeedsReply[] = []): DashboardData => ({
  stats: { unread_today: 0 }, needs_reply: needs, recently_handled: handled, attention_people: [], latest_briefing: null,
})

describe('applyTriageUpdate', () => {
  const now = new Date('2026-10-06T12:00:00Z')

  it('moves a handled chat to recently_handled and back on reopen (ordered by last message)', () => {
    const d = dash([item(1, '2026-10-06T10:00:00Z'), item(2, '2026-10-06T11:00:00Z')])
    const handled = applyTriageUpdate(d, triage(1, 'handled'), now)
    expect(handled.needs_reply.map((i) => i.conversation.id)).toEqual([2])
    expect(handled.recently_handled?.map((i) => i.conversation.id)).toEqual([1])

    const reopened = applyTriageUpdate(handled, triage(1, 'pending'), now)
    expect(reopened.needs_reply.map((i) => i.conversation.id)).toEqual([2, 1])
    expect(reopened.recently_handled).toEqual([])
  })

  it('drops a future snooze but keeps an expired one', () => {
    const d = dash([item(1, '2026-10-06T10:00:00Z')])
    expect(applyTriageUpdate(d, triage(1, 'snoozed', { snoozed_until: '2026-10-07T00:00:00Z' }), now).needs_reply).toEqual([])
    expect(applyTriageUpdate(d, triage(1, 'snoozed', { snoozed_until: '2026-10-06T11:00:00Z' }), now).needs_reply).toHaveLength(1)
  })

  it('keeps the note on the item', () => {
    const d = dash([item(1, '2026-10-06T10:00:00Z')])
    const out = applyTriageUpdate(d, triage(1, 'pending', { note: 'call back' }), now)
    expect(out.needs_reply[0].triage?.note).toBe('call back')
  })
})

describe('NoteEditor', () => {
  it('opens pre-filled with the saved note, so saving never erases it', () => {
    render(<NoteEditor savedNote="call back Friday" onSave={vi.fn()} onClear={vi.fn()} onClose={vi.fn()} />)
    expect((screen.getByLabelText('Note') as HTMLTextAreaElement).value).toBe('call back Friday')
    // Unchanged → Save disabled
    expect((screen.getByText('Save note') as HTMLButtonElement).disabled).toBe(true)
  })

  it('does not save a blank note; deleting is an explicit action', async () => {
    const onSave = vi.fn(async () => true)
    const onClear = vi.fn(async () => true)
    const onClose = vi.fn()
    render(<NoteEditor savedNote="old" onSave={onSave} onClear={onClear} onClose={onClose} />)
    fireEvent.change(screen.getByLabelText('Note'), { target: { value: '   ' } })
    expect((screen.getByText('Save note') as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByText('Delete note'))
    await waitFor(() => expect(onClear).toHaveBeenCalled())
    expect(onSave).not.toHaveBeenCalled()
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('saves the trimmed note', async () => {
    const onSave = vi.fn(async () => true)
    render(<NoteEditor savedNote={null} onSave={onSave} onClear={vi.fn()} onClose={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Note'), { target: { value: '  ping  ' } })
    fireEvent.click(screen.getByText('Save note'))
    await waitFor(() => expect(onSave).toHaveBeenCalledWith('ping'))
  })
})

describe('TriageCard degraded summary', () => {
  const ctx = (degraded: boolean) => ({
    summary: 'Alice asked about the invoice', summary_messages: [], recent_messages: [],
    last_activity_at: null, generated_at: '2026-10-06T10:00:00Z', new_messages_since_summary: 0, degraded,
  })
  const noop = () => {}
  const renderCard = (degraded: boolean) => {
    const it = { ...item(1, '2026-10-06T10:00:00Z'), context: ctx(degraded) }
    const r = render(
      <TriageCard item={it} selected={false} onSnooze={noop} onHandled={noop}
        onSaveNote={async () => true} onClearNote={async () => true} onRelationship={noop} onReply={noop} />,
    )
    fireEvent.click(screen.getByText('Chat 1'))
    return r
  }

  it('labels a fallback summary as basic', () => {
    renderCard(true)
    expect(screen.getByText('Alice asked about the invoice')).toBeTruthy()
    expect(screen.getByText('Basic summary — AI unavailable')).toBeTruthy()
  })

  it('shows no label on an AI summary', () => {
    renderCard(false)
    expect(screen.queryByText('Basic summary — AI unavailable')).toBeNull()
  })
})
