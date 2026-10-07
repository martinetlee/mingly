/**
 * Date/time helpers. The backend stores naive UTC and some endpoints emit
 * ISO strings without an offset, which `new Date()` would read as *local*
 * time — always go through parseServerDate().
 */

type DateInput = string | number | Date | null | undefined

const HAS_OFFSET = /(Z|[+-]\d{2}:?\d{2})$/i
const ISO_DATETIME = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)(\.\d+)?(.*)$/

/**
 * Parse a server timestamp. Offset-less ISO datetimes are treated as UTC;
 * fractional seconds are truncated to milliseconds (WebKit rejects more).
 * Returns null for empty/invalid input.
 */
export function parseServerDate(input: DateInput): Date | null {
  if (input == null || input === '') return null
  if (input instanceof Date) return Number.isNaN(input.getTime()) ? null : input
  if (typeof input === 'number') {
    const d = new Date(input)
    return Number.isNaN(d.getTime()) ? null : d
  }
  const s = input.trim()
  const m = ISO_DATETIME.exec(s)
  let normalized = s
  if (m) {
    const [, date, time, frac = '', rest] = m
    const ms = frac ? frac.slice(0, 4).padEnd(4, '0') : ''
    const offset = rest.trim()
    normalized = `${date}T${time}${ms}${offset && HAS_OFFSET.test(offset) ? offset.toUpperCase() : 'Z'}`
  }
  const d = new Date(normalized)
  return Number.isNaN(d.getTime()) ? null : d
}

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
}

/** Whole calendar days between `d` and `now` in local time (0 = today). */
export function calendarDaysAgo(d: Date, now: Date = new Date()): number {
  return Math.round((startOfDay(now) - startOfDay(d)) / 86400000)
}

/** "Just now" · "5m ago" · "3h ago" · "2d ago" · "Mar 4" */
export function fmtRelative(input: DateInput, now: Date = new Date()): string {
  const d = parseServerDate(input)
  if (!d) return ''
  const mins = Math.floor((now.getTime() - d.getTime()) / 60000)
  if (mins < 1) return 'Just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 7) return `${days}d ago`
  return fmtShortDate(d, now)
}

/** Coarse age: "Today" · "Yesterday" · "3d ago" · "2w ago" · "4mo ago" · "1y ago" */
export function fmtRelativeDays(input: DateInput, now: Date = new Date()): string {
  const d = parseServerDate(input)
  if (!d) return ''
  const days = calendarDaysAgo(d, now)
  if (days <= 0) return 'Today'
  if (days === 1) return 'Yesterday'
  if (days < 7) return `${days}d ago`
  if (days < 30) return `${Math.floor(days / 7)}w ago`
  if (days < 365) return `${Math.floor(days / 30)}mo ago`
  return `${Math.floor(days / 365)}y ago`
}

/** List timestamp: time today · "Yesterday" · weekday within a week · "Mar 4" */
export function fmtDate(input: DateInput, now: Date = new Date()): string {
  const d = parseServerDate(input)
  if (!d) return ''
  const days = calendarDaysAgo(d, now)
  if (days <= 0) return fmtTime(d)
  if (days === 1) return 'Yesterday'
  if (days < 7) return d.toLocaleDateString([], { weekday: 'short' })
  return fmtShortDate(d, now)
}

/** "Mar 4", or "Mar 4, 2024" when not in the current year */
export function fmtShortDate(input: DateInput, now: Date = new Date()): string {
  const d = parseServerDate(input)
  if (!d) return ''
  return d.getFullYear() === now.getFullYear()
    ? d.toLocaleDateString([], { month: 'short', day: 'numeric' })
    : d.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' })
}

/** "14:05" / "2:05 PM" (locale) */
export function fmtTime(input: DateInput): string {
  const d = parseServerDate(input)
  if (!d) return ''
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

/** "Mar 4, 14:05" (year added when not the current year) */
export function fmtDateTime(input: DateInput, now: Date = new Date()): string {
  const d = parseServerDate(input)
  if (!d) return ''
  return `${fmtShortDate(d, now)}, ${fmtTime(d)}`
}

/** Media/elapsed duration: "0:07" · "4:32" · "1:02:03" */
export function formatDuration(totalSeconds: number | null | undefined): string {
  if (totalSeconds == null || !Number.isFinite(totalSeconds) || totalSeconds < 0) return '0:00'
  const s = Math.floor(totalSeconds)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  const ss = sec.toString().padStart(2, '0')
  return h > 0 ? `${h}:${m.toString().padStart(2, '0')}:${ss}` : `${m}:${ss}`
}

/** Whole days since `input` (by elapsed time), or null if missing */
export function ageDays(input: DateInput, now: Date = new Date()): number | null {
  const d = parseServerDate(input)
  if (!d) return null
  return Math.floor((now.getTime() - d.getTime()) / 86400000)
}
