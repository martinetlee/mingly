/**
 * Sanitize a URL for safe rendering in anchor tags.
 *
 * Only allows http: and https: schemes, and for LinkedIn URLs specifically
 * ensures the hostname ends with linkedin.com.  Returns null for anything
 * that looks dangerous (javascript:, data:, etc.) so the caller can fall
 * back to rendering plain text instead of a clickable link.
 */
export function sanitizeLinkedInUrl(raw: string | null | undefined): string | null {
  if (!raw) return null

  let url = raw.trim()
  if (!url) return null

  // Prepend https:// if no scheme is present
  if (!/^https?:\/\//i.test(url)) {
    url = `https://${url}`
  }

  try {
    const parsed = new URL(url)

    // Only allow http(s) schemes — blocks javascript:, data:, etc.
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      return null
    }

    // Must be a linkedin.com hostname
    if (
      parsed.hostname !== 'linkedin.com' &&
      !parsed.hostname.endsWith('.linkedin.com')
    ) {
      return null
    }

    return parsed.href
  } catch {
    return null
  }
}

/**
 * Generic URL sanitizer that only allows http(s) schemes.
 * Use this for non-LinkedIn URLs where domain restriction is not needed.
 */
export function sanitizeUrl(raw: string | null | undefined): string | null {
  if (!raw) return null

  let url = raw.trim()
  if (!url) return null

  if (!/^https?:\/\//i.test(url)) {
    url = `https://${url}`
  }

  try {
    const parsed = new URL(url)
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      return null
    }
    return parsed.href
  } catch {
    return null
  }
}
