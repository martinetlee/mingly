import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { Tag } from '../../api/types'
import { ApiError } from '../../lib/http'

const createTag = vi.fn()
const getTags = vi.fn()
vi.mock('../../api/client', async (orig) => ({
  ...(await orig<typeof import('../../api/client')>()),
  createTag: (...a: unknown[]) => createTag(...a),
  getTags: (...a: unknown[]) => getTags(...a),
}))

import { createOrFindTag, tagErrorMessage } from '../chatTags'

const tag = (id: number, name: string): Tag => ({ id, name, color: 'slate', category: null, is_default: false })
const conflict = (body: Record<string, unknown> = {}) =>
  new ApiError('A tag named "X" already exists', { status: 409, code: 'http', path: '/api/tags', body })

beforeEach(() => { createTag.mockReset(); getTags.mockReset() })

describe('createOrFindTag', () => {
  it('creates a new tag', async () => {
    createTag.mockResolvedValue(tag(1, 'Investors'))
    expect(await createOrFindTag('Investors', 'slate', [])).toEqual({ tag: tag(1, 'Investors'), created: true })
  })

  it('reuses the existing tag on a duplicate name (case/whitespace-insensitive)', async () => {
    createTag.mockRejectedValue(conflict())
    expect(await createOrFindTag('  investors ', 'slate', [tag(7, 'Investors')])).toEqual({ tag: tag(7, 'Investors'), created: false })
  })

  it('looks the duplicate up on the server when it is not known locally', async () => {
    createTag.mockRejectedValue(conflict({ existing_id: 9 }))
    getTags.mockResolvedValue([tag(9, 'VC')])
    expect((await createOrFindTag('vc', 'slate', [])).tag.id).toBe(9)
  })

  it('rethrows any other failure (no silent swallow)', async () => {
    createTag.mockRejectedValue(new ApiError('name: Field required', { status: 422, code: 'validation', path: '/api/tags' }))
    await expect(createOrFindTag('', 'slate', [])).rejects.toThrow(/Field required/)
  })
})

describe('tagErrorMessage', () => {
  it('explains locked stage tags', () => {
    const e = new ApiError('locked', { status: 409, code: 'http', path: '/api/chats/1/tags', serverCode: 'stage_tag_locked' })
    expect(tagErrorMessage(e, 'Done')).toMatch(/board column tag/)
  })
})
