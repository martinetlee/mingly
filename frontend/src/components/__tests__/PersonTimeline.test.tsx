import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { TimelineMeetingItem, TimelineMessageItem, TimelinePage } from '../../api/timeline'
import type { CalendarEvent } from '../../api/calendar'
import { WebSocketClient } from '../../lib/ws'
import { WebSocketProvider } from '../../context/WebSocketContext'
import { msg } from '../../pages/chats/__tests__/fixtures'

vi.mock('../../api/timeline', async (orig) => ({
  ...(await orig<typeof import('../../api/timeline')>()),
  getPersonTimeline: vi.fn(),
}))

import { getPersonTimeline } from '../../api/timeline'
import PersonTimeline, { timelineSnippet } from '../PersonTimeline'

const dm = { id: 10, title: 'Alice', kind: 'direct' as const, channel: 'telegram' }
const group = { id: 20, title: 'Team', kind: 'group' as const, channel: 'signal' }
const thread = { id: 30, title: 'Quarterly plan', kind: 'email_thread' as const, channel: 'gmail' }

const item = (id: number, conversation: TimelineMessageItem['conversation'], over: Partial<TimelineMessageItem['message']> = {}): TimelineMessageItem => ({
  kind: 'message',
  message: msg(id, { conversation_id: conversation?.id ?? 0, sender: { identity_id: 1, person_id: 42, name: 'Alice', avatar_url: null }, ...over }),
  conversation,
})

const meeting = (id: number, title: string): TimelineMeetingItem => ({
  kind: 'meeting',
  event: {
    id, account_id: 1, calendar_id: 'primary', title, description: null, location: 'Room 4',
    start_at: '2026-10-01T09:00:00Z', end_at: '2026-10-01T10:00:00Z', all_day: false, status: 'confirmed',
    html_link: 'https://calendar.google.com/x', organizer: null,
    attendees: [
      { identity_id: 1, person_id: 1, name: 'Me', email: 'me@x.com', response_status: 'accepted', is_organizer: true, is_self: true },
      { identity_id: 2, person_id: 42, name: 'Alice', email: 'a@x.com', response_status: 'accepted', is_organizer: false, is_self: false },
    ],
  } satisfies CalendarEvent,
})

const firstPage: TimelinePage = {
  items: [
    item(5, thread, { subject: 'Plan for Q4', text: 'long body' }),
    item(4, group, { text: 'hello team' }),
    item(3, dm, { text: 'see you', is_outgoing: true, sender: null }),
  ],
  has_more: true,
}
const secondPage: TimelinePage = { items: [item(3, dm, { text: 'see you' }), item(2, dm, { text: 'hi there' })], has_more: false }

function setup() {
  const ws = new WebSocketClient({ url: () => null })
  return render(
    <WebSocketProvider client={ws}>
      <MemoryRouter><PersonTimeline personId={42} /></MemoryRouter>
    </WebSocketProvider>,
  )
}

const rows = () => screen.queryAllByTestId('timeline-row')

beforeEach(() => {
  vi.mocked(getPersonTimeline).mockReset()
})
afterEach(() => cleanup())

describe('PersonTimeline', () => {
  it('renders compact rows linking to the message where it lives', async () => {
    vi.mocked(getPersonTimeline).mockResolvedValueOnce(firstPage)
    setup()
    await waitFor(() => expect(rows()).toHaveLength(3))
    const [email, grp, mine] = rows()
    // Email: subject, not body
    expect(email.textContent).toContain('Quarterly plan')
    expect(email.textContent).toContain('Plan for Q4')
    expect(email.textContent).not.toContain('long body')
    expect(email.querySelector('[aria-label="Gmail"]')).not.toBeNull()
    expect(grp.textContent).toContain('Team')
    expect(grp.textContent).toContain('Alice')
    expect(grp.textContent).toContain('hello team')
    expect(grp.querySelector('[aria-label="Signal"]')).not.toBeNull()
    expect(mine.textContent).toContain('You')
    // Each row opens its own space: the email in Mail, chats in Chats
    expect(email.getAttribute('href')).toBe('/mail?conversationId=30&messageId=5')
    expect(grp.getAttribute('href')).toBe('/chats?conversationId=20&messageId=4')
    expect(vi.mocked(getPersonTimeline).mock.calls[0][0]).toBe(42)
  })

  it('"Load more" pages back from the oldest item and appends without duplicates', async () => {
    vi.mocked(getPersonTimeline).mockResolvedValueOnce(firstPage).mockResolvedValueOnce(secondPage)
    setup()
    await waitFor(() => expect(rows()).toHaveLength(3))
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }))
    await waitFor(() => expect(rows()).toHaveLength(4))
    const opts = vi.mocked(getPersonTimeline).mock.calls[1][1]
    expect(opts?.before).toMatchObject({ id: 3, date: (firstPage.items[2] as TimelineMessageItem).message.date })
    expect(rows()[3].textContent).toContain('hi there')
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
  })

  it('lists meetings between messages, linking to their day in Calendar', async () => {
    vi.mocked(getPersonTimeline).mockResolvedValueOnce({ items: [item(5, dm, { text: 'see you there' }), meeting(7, 'Design review'), item(4, dm, { text: 'agenda?' })], has_more: false })
    setup()
    await waitFor(() => expect(rows()).toHaveLength(2))
    const [m] = screen.getAllByTestId('timeline-meeting')
    expect(m.textContent).toContain('Design review')
    expect(m.textContent).toContain('Meeting')
    expect(m.textContent).toContain('Room 4')
    expect(m.textContent).toContain('1 person') // you aren't counted
    expect(m.getAttribute('href')).toMatch(/^\/calendar\?event=7&date=\d{4}-\d{2}-\d{2}$/)
    // Newest first, as the server sent them
    const order = screen.getAllByTestId(/timeline-(row|meeting)/).map((el) => el.getAttribute('data-testid'))
    expect(order).toEqual(['timeline-row', 'timeline-meeting', 'timeline-row'])
  })

  it('"Load more" continues before the oldest message even when a meeting is last', async () => {
    vi.mocked(getPersonTimeline)
      .mockResolvedValueOnce({ items: [item(5, dm, { text: 'x' }), meeting(7, 'Sync')], has_more: true })
      .mockResolvedValueOnce({ items: [meeting(7, 'Sync'), item(2, dm, { text: 'older' })], has_more: false })
    setup()
    await waitFor(() => expect(screen.getAllByTestId('timeline-meeting')).toHaveLength(1))
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }))
    await waitFor(() => expect(rows()).toHaveLength(2))
    expect(vi.mocked(getPersonTimeline).mock.calls[1][1]?.before).toMatchObject({ id: 5 })
    expect(screen.getAllByTestId('timeline-meeting')).toHaveLength(1) // not duplicated
  })

  it('shows an empty state', async () => {
    vi.mocked(getPersonTimeline).mockResolvedValueOnce({ items: [], has_more: false })
    setup()
    await waitFor(() => expect(screen.getByText('No messages yet')).toBeTruthy())
  })

  it('snippet falls back to the attachment kind, and to "(no subject)" for email', () => {
    expect(timelineSnippet(item(1, dm, { text: null, attachments: [{ kind: 'photo' } as never] }))).toBe('[photo]')
    expect(timelineSnippet(item(1, thread, { subject: null, text: null }))).toBe('(no subject)')
    expect(timelineSnippet(item(1, thread, { subject: null, text: 'body\n text' }))).toBe('body text')
  })
})
