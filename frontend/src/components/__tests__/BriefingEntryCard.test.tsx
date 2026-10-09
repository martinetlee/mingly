import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import type { BriefingHistoryEntry, DashboardBriefingEntry } from '../../api/types'
import { BriefingEntryList, type RegenerateControls } from '../BriefingEntryCard'

function Where() {
  const loc = useLocation()
  return <p data-testid="where">{loc.pathname + loc.search}</p>
}

describe('BriefingEntryList (Home → Latest Briefing)', () => {
  it('renders dashboard entries with clickable [n] citations that open the source message', () => {
    const entries: DashboardBriefingEntry[] = [{
      id: 1, conversation_id: 123, conversation_title: 'Team', category: 'tracked',
      summary: '**Launch** moved to Friday [1]', sources: [{ conversation_id: 123, message_id: 4242 }],
    }]
    render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route path="*" element={<><BriefingEntryList entries={entries} /><Where /></>} />
        </Routes>
      </MemoryRouter>,
    )
    expect(screen.getByText('Launch').tagName).toBe('STRONG')
    fireEvent.click(screen.getByTitle('Jump to source message'))
    expect(screen.getByTestId('where').textContent).toBe('/open/123?messageId=4242') // resolved to Chats or Mail by kind
  })

  it('puts today\'s meetings first, linked to Calendar, with no Regenerate (not written by the LLM)', () => {
    const entries: BriefingHistoryEntry[] = [
      { id: 2, conversation_id: 9, conversation_title: 'Team', category: 'tracked', summary: 'All quiet' },
      { id: 1, conversation_id: null, person_id: null, conversation_title: 'Today\'s meetings', category: 'meetings',
        summary: '- **09:00–10:00** Sync — with Ann' },
    ]
    const regen: RegenerateControls = {
      activeEntryId: null, busy: false, reasoning: {}, tokens: {}, flowOpen: {},
      onRegenerate: vi.fn(), onStop: vi.fn(), onToggleFlow: vi.fn(),
    }
    render(<MemoryRouter><BriefingEntryList entries={entries} regen={regen} /></MemoryRouter>)
    const headings = screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent)
    expect(headings).toEqual(['Today’s Meetings', 'Tracked Chats'])
    expect(screen.getByText('Today\'s meetings').getAttribute('href')).toBe('/calendar')
    expect(screen.getByText('Sync', { exact: false })).toBeTruthy()
    expect(screen.getAllByText('Regenerate')).toHaveLength(1) // only the tracked chat's
  })
})
