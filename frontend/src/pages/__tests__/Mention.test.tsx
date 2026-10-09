import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'

const getPersonForTelegramUser = vi.fn()
const resolveTelegramUsername = vi.fn()
vi.mock('../../api/client', async (orig) => ({
  ...(await orig<typeof import('../../api/client')>()),
  getPersonForTelegramUser: (...a: unknown[]) => getPersonForTelegramUser(...a),
  resolveTelegramUsername: (...a: unknown[]) => resolveTelegramUsername(...a),
}))

import Mention from '../Mention'
import { ApiError } from '../../lib/http'

function Where() {
  const l = useLocation()
  return <div data-testid="where">{l.pathname + l.search}</div>
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/mention/:channel/:externalId" element={<Mention />} />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  getPersonForTelegramUser.mockReset()
  resolveTelegramUsername.mockReset()
})
afterEach(() => cleanup())

describe('/mention/:channel/:externalId', () => {
  it('resolves a Telegram user id to the person and redirects', async () => {
    getPersonForTelegramUser.mockResolvedValue({ person_id: 7, identity_id: 70 })
    renderAt('/mention/telegram/12345')
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/people/7'))
    expect(getPersonForTelegramUser.mock.calls[0][0]).toBe('12345')
  })

  it('resolves @username to a person, or to a conversation for groups/channels', async () => {
    resolveTelegramUsername.mockResolvedValueOnce({ person_id: 8, identity_id: 80, conversation_id: null })
    renderAt('/mention/telegram/%40alice')
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/people/8'))
    expect(resolveTelegramUsername.mock.calls[0][0]).toBe('alice')
    cleanup()
    resolveTelegramUsername.mockResolvedValueOnce({ person_id: null, identity_id: null, conversation_id: 55 })
    renderAt('/mention/telegram/%40news')
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/chats?conversationId=55'))
  })

  it('says so when the handle is unknown', async () => {
    getPersonForTelegramUser.mockRejectedValue(new ApiError('Not found', { status: 404, code: 'http', path: '/x' }))
    renderAt('/mention/telegram/999')
    await waitFor(() => expect(screen.getByText(/isn’t known yet/)).toBeTruthy())
  })
})
