import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, within } from '@testing-library/react'
import { ApiError } from '../../lib/http'
import { ConfirmProvider } from '../../components/ui'
import type { Tag } from '../../api/types'

vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  getTags: vi.fn(),
  createTag: vi.fn(),
  updateTag: vi.fn(),
  deleteTag: vi.fn(),
}))

import { getTags, createTag, deleteTag } from '../../api/client'
import TagsManager, { isStageTag } from '../settings/TagsManager'

const tag = (id: number, name: string, category: string | null = null): Tag =>
  ({ id, name, color: 'slate', category, is_default: false } as Tag)

const TAGS = [tag(1, 'VIP'), tag(2, 'Done', 'stage'), tag(3, 'In Progress', 'Stage '), tag(4, 'Investor', 'Work')]

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getTags).mockResolvedValue(TAGS)
})
afterEach(cleanup)

const renderManager = () => render(<ConfirmProvider><TagsManager /></ConfirmProvider>)

describe('TagsManager', () => {
  it('detects stage tags case- and whitespace-insensitively', () => {
    expect(isStageTag({ category: 'stage' })).toBe(true)
    expect(isStageTag({ category: ' Stage ' })).toBe(true)
    expect(isStageTag({ category: 'Work' })).toBe(false)
    expect(isStageTag({ category: null })).toBe(false)
  })

  it('never lists board-column (stage) tags', async () => {
    renderManager()
    expect(await screen.findByText('VIP')).toBeTruthy()
    expect(screen.getByText('Investor')).toBeTruthy()
    expect(screen.queryByText('Done')).toBeNull()
    expect(screen.queryByText('In Progress')).toBeNull()
    expect(screen.queryByText(/^stage$/i)).toBeNull()
    // Two visible tags → two Delete buttons
    expect(screen.getAllByRole('button', { name: 'Delete' })).toHaveLength(2)
  })

  it('a 409 on delete shows the server message in the dialog and resyncs the list', async () => {
    vi.mocked(deleteTag).mockRejectedValue(new ApiError('This tag belongs to a board column; change it from the board', {
      status: 409, code: 'http', path: '/api/tags/1', serverCode: 'stage_tag_locked',
    }))
    renderManager()
    await screen.findByText('VIP')
    // After the conflict the server reports VIP as a stage tag
    vi.mocked(getTags).mockResolvedValue([tag(1, 'VIP', 'stage'), tag(4, 'Investor', 'Work')])

    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0])
    const dialog = await screen.findByRole('dialog')
    const buttons = within(dialog).getAllByRole('button', { name: 'Delete' })
    fireEvent.click(buttons[buttons.length - 1])

    expect((await within(dialog).findByRole('alert')).textContent).toContain('This tag belongs to a board column; change it from the board')
    await waitFor(() => expect(getTags).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByText('VIP')).toBeNull())
  })

  it('refuses the reserved "stage" category when creating a tag', async () => {
    renderManager()
    await screen.findByText('VIP')
    fireEvent.change(screen.getByPlaceholderText('Tag name…'), { target: { value: 'Lead' } })
    fireEvent.change(screen.getByPlaceholderText('e.g. Work, Personal…'), { target: { value: 'Stage' } })
    expect(screen.getByText(/reserved for board columns/)).toBeTruthy()
    const create = screen.getByRole('button', { name: 'Create' }) as HTMLButtonElement
    expect(create.disabled).toBe(true)
    fireEvent.keyDown(screen.getByPlaceholderText('Tag name…'), { key: 'Enter' })
    expect(createTag).not.toHaveBeenCalled()
  })
})
