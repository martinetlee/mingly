import { describe, it, expect } from 'vitest'
import { KIND_COLOR, kindColor, kindLabel } from '../conversation'

describe('conversation kind labels and badges (one wording everywhere)', () => {
  it('labels every kind in plain words, never the raw enum', () => {
    expect(kindLabel('direct')).toBe('direct message')
    expect(kindLabel('group')).toBe('group')
    expect(kindLabel('channel')).toBe('channel')
    expect(kindLabel('email_thread')).toBe('email thread')
    expect(kindLabel('some_new_kind')).toBe('some new kind')
  })
  it('has a badge colour per kind and a neutral one for unknown kinds', () => {
    expect(kindColor('email_thread')).toBe(KIND_COLOR.email_thread)
    expect(kindColor('mystery')).toBe('bg-slate-600 text-slate-400')
  })
})
