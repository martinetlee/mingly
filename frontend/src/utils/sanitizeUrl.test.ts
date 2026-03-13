import { sanitizeLinkedInUrl, sanitizeUrl } from './sanitizeUrl'

describe('sanitizeLinkedInUrl', () => {
  it('returns null for null/undefined/empty', () => {
    expect(sanitizeLinkedInUrl(null)).toBeNull()
    expect(sanitizeLinkedInUrl(undefined)).toBeNull()
    expect(sanitizeLinkedInUrl('')).toBeNull()
    expect(sanitizeLinkedInUrl('  ')).toBeNull()
  })

  it('accepts valid linkedin.com URLs', () => {
    expect(sanitizeLinkedInUrl('https://www.linkedin.com/in/johndoe')).toBe(
      'https://www.linkedin.com/in/johndoe',
    )
    expect(sanitizeLinkedInUrl('https://linkedin.com/in/johndoe')).toBe(
      'https://linkedin.com/in/johndoe',
    )
    expect(sanitizeLinkedInUrl('http://www.linkedin.com/in/johndoe')).toBe(
      'http://www.linkedin.com/in/johndoe',
    )
  })

  it('prepends https:// for bare domains', () => {
    expect(sanitizeLinkedInUrl('linkedin.com/in/johndoe')).toBe(
      'https://linkedin.com/in/johndoe',
    )
    expect(sanitizeLinkedInUrl('www.linkedin.com/in/johndoe')).toBe(
      'https://www.linkedin.com/in/johndoe',
    )
  })

  it('accepts subdomain variants', () => {
    expect(sanitizeLinkedInUrl('https://uk.linkedin.com/in/johndoe')).toBe(
      'https://uk.linkedin.com/in/johndoe',
    )
  })

  it('rejects javascript: scheme', () => {
    expect(sanitizeLinkedInUrl('javascript:alert(1)')).toBeNull()
  })

  it('rejects data: scheme', () => {
    expect(sanitizeLinkedInUrl('data:text/html,<script>alert(1)</script>')).toBeNull()
  })

  it('rejects non-linkedin domains', () => {
    expect(sanitizeLinkedInUrl('https://evil.com/in/johndoe')).toBeNull()
  })

  it('rejects lookalike domains', () => {
    expect(sanitizeLinkedInUrl('https://notlinkedin.com/in/johndoe')).toBeNull()
    expect(sanitizeLinkedInUrl('https://linkedin.com.evil.com/in/johndoe')).toBeNull()
  })
})

describe('sanitizeUrl', () => {
  it('returns null for null/undefined/empty', () => {
    expect(sanitizeUrl(null)).toBeNull()
    expect(sanitizeUrl(undefined)).toBeNull()
    expect(sanitizeUrl('')).toBeNull()
  })

  it('accepts valid http(s) URLs', () => {
    expect(sanitizeUrl('https://example.com')).toBe('https://example.com/')
    expect(sanitizeUrl('http://example.com')).toBe('http://example.com/')
  })

  it('prepends https:// for bare domains', () => {
    expect(sanitizeUrl('example.com')).toBe('https://example.com/')
  })

  it('rejects javascript: scheme', () => {
    expect(sanitizeUrl('javascript:alert(1)')).toBeNull()
  })

  it('rejects data: scheme', () => {
    expect(sanitizeUrl('data:text/html,test')).toBeNull()
  })
})
