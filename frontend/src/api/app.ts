/** App-level calls: feature switches and opening external links. */

import { request } from '../lib/http'
import type { Features } from './types'

export const openExternalUrl = (url: string) => {
  const pywebview = (window as any).pywebview
  if (pywebview?.api?.open_url) {
    return pywebview.api.open_url(url)
  }
  return request<{ status: string }>('/api/open-url', {
    method: 'POST',
    body: JSON.stringify({ url }),
  }).catch(() => { window.open(url, '_blank') })
}

export function getFeatures(signal?: AbortSignal): Promise<Features> {
  return request<Features>('/api/features', { signal })
}
