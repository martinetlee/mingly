import { describe, it, expect } from 'vitest'
import { parseServerDate, fmtRelative, fmtRelativeDays, fmtDate, formatDuration, ageDays, calendarDaysAgo } from './time'

describe('parseServerDate', () => {
  it('treats offset-less ISO as UTC (backend naive UTC)', () => {
    expect(parseServerDate('2026-10-06T08:00:00')?.toISOString()).toBe('2026-10-06T08:00:00.000Z')
    expect(parseServerDate('2026-10-06 08:00:00')?.toISOString()).toBe('2026-10-06T08:00:00.000Z')
  })

  it('keeps explicit offsets', () => {
    expect(parseServerDate('2026-10-06T08:00:00Z')?.toISOString()).toBe('2026-10-06T08:00:00.000Z')
    expect(parseServerDate('2026-10-06T08:00:00+08:00')?.toISOString()).toBe('2026-10-06T00:00:00.000Z')
    expect(parseServerDate('2026-10-06T08:00:00.5-0130')?.toISOString()).toBe('2026-10-06T09:30:00.500Z')
  })

  it('truncates microseconds (Python isoformat) to milliseconds', () => {
    expect(parseServerDate('2026-10-06T08:00:00.123456')?.toISOString()).toBe('2026-10-06T08:00:00.123Z')
  })

  it('returns null for empty/invalid input', () => {
    expect(parseServerDate(null)).toBeNull()
    expect(parseServerDate('')).toBeNull()
    expect(parseServerDate('not a date')).toBeNull()
  })
})

describe('formatters', () => {
  const now = new Date('2026-10-06T12:00:00Z')

  it('fmtRelative', () => {
    expect(fmtRelative('2026-10-06T11:59:40', now)).toBe('Just now')
    expect(fmtRelative('2026-10-06T11:55:00', now)).toBe('5m ago')
    // The UTC+8 bug: a naive UTC string from 3h ago must not read as "8h"-shifted
    expect(fmtRelative('2026-10-06T09:00:00', now)).toBe('3h ago')
    expect(fmtRelative('2026-10-04T12:00:00', now)).toBe('2d ago')
    expect(fmtRelative(null, now)).toBe('')
  })

  it('fmtRelativeDays uses calendar days', () => {
    const local = new Date(2026, 9, 6, 9, 0)
    expect(fmtRelativeDays(new Date(2026, 9, 6, 1, 0), local)).toBe('Today')
    expect(fmtRelativeDays(new Date(2026, 9, 5, 23, 0), local)).toBe('Yesterday')
    expect(fmtRelativeDays(new Date(2026, 8, 20), local)).toBe('2w ago')
    expect(fmtRelativeDays(new Date(2025, 9, 1), local)).toBe('1y ago')
  })

  it('fmtDate: time today, Yesterday, weekday, then short date', () => {
    const local = new Date(2026, 9, 6, 9, 0)
    expect(fmtDate(new Date(2026, 9, 6, 8, 5), local)).toMatch(/08|8/)
    expect(fmtDate(new Date(2026, 9, 5, 22, 0), local)).toBe('Yesterday')
    expect(calendarDaysAgo(new Date(2026, 9, 2), local)).toBe(4)
    expect(fmtDate(new Date(2026, 9, 2), local)).toBe(new Date(2026, 9, 2).toLocaleDateString([], { weekday: 'short' }))
  })

  it('formatDuration', () => {
    expect(formatDuration(7)).toBe('0:07')
    expect(formatDuration(272.9)).toBe('4:32')
    expect(formatDuration(3723)).toBe('1:02:03')
    expect(formatDuration(NaN)).toBe('0:00')
  })

  it('ageDays', () => {
    expect(ageDays('2026-10-01T12:00:00', now)).toBe(5)
    expect(ageDays(undefined, now)).toBeNull()
  })
})
