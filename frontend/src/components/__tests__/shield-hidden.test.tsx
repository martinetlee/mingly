import { describe, it, expect, vi, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, cleanup, renderHook } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { msg } from '../../pages/chats/__tests__/fixtures'

vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  getScamFlags: vi.fn(() => Promise.resolve({ flags: [], flagged_sender_ids: [], flagged_chat_ids: [] })),
}))

import { getScamFlags } from '../../api/client'
import MessageBubble from '../MessageBubble'
import { FeaturesProvider } from '../../context/FeaturesContext'
import { useFlaggedSenders, __resetFlaggedSendersForTests } from '../../hooks/useFlaggedSenders'
import { ShieldRoute } from '../../App'

afterEach(() => {
  cleanup()
  __resetFlaggedSendersForTests()
  vi.mocked(getScamFlags).mockClear()
})

const scamMessage = msg(1, {
  effective_is_scam: true, scam_status: 'completed', scam_score: 0.95, scam_reason: 'Phishing link',
})

function renderBubble(wrap: (n: ReactNode) => ReactNode) {
  return render(
    <MemoryRouter>
      {wrap(
        <MessageBubble message={scamMessage} showSender isFlaggedSender onReportScam={() => {}} onScamVerdict={() => {}} />,
      )}
    </MemoryRouter>,
  )
}

describe('Shield hidden (feature off)', () => {
  it('a message the server marked as scam shows no scam UI when Shield is off', () => {
    renderBubble((n) => n) // no provider: defaults to off
    expect(screen.queryByText(/scam/i)).toBeNull()
  })

  it('shows the scam banner when Shield is on', () => {
    renderBubble((n) => <FeaturesProvider value={{ shield: true }}>{n}</FeaturesProvider>)
    expect(screen.getByText('Potential scam detected')).toBeTruthy()
  })

  it('the flagged-senders store fetches nothing while Shield is off', () => {
    const { result } = renderHook(() => useFlaggedSenders())
    expect(getScamFlags).not.toHaveBeenCalled()
    expect(result.current.ids.size).toBe(0)
  })

  it('/shield redirects home when off, renders nothing until features load', () => {
    const tree = (features?: { shield: boolean }) => (
      <MemoryRouter initialEntries={['/shield']}>
        <FeaturesProvider value={features}>
          <Routes>
            <Route path="/shield" element={<ShieldRoute />} />
            <Route path="/home" element={<div>home page</div>} />
          </Routes>
        </FeaturesProvider>
      </MemoryRouter>
    )
    render(tree({ shield: false }))
    expect(screen.getByText('home page')).toBeTruthy()
  })
})
