/**
 * The backend API, one module per domain (api/<domain>.ts). This barrel is the
 * single import site for callers (and for `vi.mock('…/api/client')` in tests);
 * domain modules import each other directly, never through here.
 */

// The transport lives in lib/http.ts; re-exported so callers have one import site
export { request, stream, upload, requestBlob, ApiError, isAbortError, isSessionExpiredError, formatErrorDetail } from '../lib/http'
export type { SSEEvent, StreamOptions, UploadOptions, UploadHandle, RequestOptions } from '../lib/http'

export * from './admin'
export * from './app'
export * from './archive'
export * from './auth'
export * from './base'
export * from './briefing'
export * from './catchup'
export * from './compose'
export * from './conversations'
export * from './dashboard'
export * from './intelligence'
export * from './llm'
export * from './mcp'
export * from './media'
export * from './messages'
export * from './organizations'
export * from './people'
export * from './search'
export * from './settings'
export * from './shield'
export * from './tags'
export * from './telegram'
export * from './translate'
export * from './workspaces'
