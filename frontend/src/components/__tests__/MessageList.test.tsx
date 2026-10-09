import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { VirtuosoMockContext } from 'react-virtuoso'
import { msg } from '../../pages/chats/__tests__/fixtures'

vi.mock('react-virtuoso', () => import('../../pages/chats/__tests__/virtuosoTestShim'))

import MessageList, { BASE_INDEX, buildItems, rebaseFirstIndex } from '../MessageList'

describe('rebaseFirstIndex (Virtuoso prepend anchoring)', () => {
  it('lowers the base by the number of prepended items', () => {
    expect(rebaseFirstIndex(['m5', 'm6'], 1000, ['m3', 'm4', 'm5', 'm6'])).toEqual({ base: 998, replaced: false })
  })
  it('raises the base when the top is trimmed', () => {
    expect(rebaseFirstIndex(['m1', 'm2', 'm3'], 1000, ['m3', 'm4'])).toEqual({ base: 1002, replaced: false })
  })
  it('keeps the base on appends and in-place updates', () => {
    expect(rebaseFirstIndex(['m1', 'm2'], 1000, ['m1', 'm2', 'm3'])).toEqual({ base: 1000, replaced: false })
  })
  it('treats a non-overlapping window as a replacement (remount at the intent)', () => {
    expect(rebaseFirstIndex(['m1', 'm2'], 990, ['m50', 'm51'])).toEqual({ base: BASE_INDEX, replaced: true })
    expect(rebaseFirstIndex([], 990, ['m1'])).toEqual({ base: BASE_INDEX, replaced: true })
  })
})

describe('buildItems', () => {
  it('keys albums by grouped_id so loading more of an album keeps the key', () => {
    const a = buildItems([msg(2, { grouped_id: 77 }), msg(3, { grouped_id: 77 }), msg(4)], true)
    const b = buildItems([msg(1, { grouped_id: 77 }), msg(2, { grouped_id: 77 }), msg(3, { grouped_id: 77 }), msg(4)], true)
    expect(a.map((i) => i.key)).toEqual(['a77', 'm4'])
    expect(b.map((i) => i.key)).toEqual(['a77', 'm4'])
    expect(b[0].ids).toEqual([1, 2, 3])
  })
  it('the album bubble carries the captioned item; sender shown once per run', () => {
    const items = buildItems([msg(1, { grouped_id: 5, text: null }), msg(2, { grouped_id: 5, text: 'caption' }), msg(3)], true)
    expect(items[0].message.id).toBe(2)
    expect(items[0].showSender).toBe(true)
    expect(items[1].showSender).toBe(false)
  })
})

describe('MessageList virtualization', () => {
  it('mounts only the rows in view (plus overscan), not the whole window', () => {
    const messages = Array.from({ length: 400 }, (_, i) => msg(i + 1))
    const { container } = render(
      <MemoryRouter>
        <VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 60 }}>
          <MessageList messages={messages} multiParty />
        </VirtuosoMockContext.Provider>
      </MemoryRouter>,
    )
    const rows = container.querySelectorAll('[data-message-ids]').length
    expect(rows).toBeGreaterThan(0)
    expect(rows).toBeLessThan(60)
  })
})
