/** MCP server settings. */

import { request } from '../lib/http'
import type { McpSettings } from './types'

export const getMcpSettings = () =>
  request<import('./types').McpSettings>('/api/settings/prefs/mcp')

export const setMcpEnabled = (enabled: boolean) =>
  request<{ success: boolean; enabled: boolean; key_set: boolean }>('/api/settings/prefs/mcp', {
    method: 'POST',
    body: JSON.stringify({ enabled }),
  })

export const generateMcpKey = () =>
  request<{ key: string }>('/api/settings/prefs/mcp/generate-key', { method: 'POST' })

export const revealMcpKey = () =>
  request<{ key: string }>('/api/settings/prefs/mcp/reveal-key', { method: 'POST' })

export const setMcpWriteEnabled = (enabled: boolean) =>
  request<{ write_enabled: boolean }>('/api/settings/prefs/mcp/write', {
    method: 'POST',
    body: JSON.stringify({ enabled }),
  })
