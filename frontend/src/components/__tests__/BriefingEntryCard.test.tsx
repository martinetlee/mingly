import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import type { DashboardBriefingEntry } from '../../api/types'
import { BriefingEntryList } from '../BriefingEntryCard'

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
    expect(screen.getByTestId('where').textContent).toBe('/chats?conversationId=123&messageId=4242')
  })
})
