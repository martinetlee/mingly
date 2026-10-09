/**
 * Connected accounts across channels (backend/api/accounts.py) and the
 * generic sign-in step machine every channel's sign-in runs through
 * (backend/connectors/base.py: AuthStep / AuthField).
 */
import { request } from '../lib/http'
import type { ChannelName, ConnectorState } from './types'

export interface AuthField {
  name: string
  label: string
  kind: 'text' | 'password' | 'phone' | 'code' | (string & {})
  required: boolean
  placeholder: string | null
}

export interface AuthStep {
  flow_id: string
  kind: 'fields' | 'qr' | 'oauth_browser' | 'progress' | 'done' | 'error' | (string & {})
  title: string | null
  message: string | null
  fields: AuthField[]
  /** QR code content to scan (e.g. Signal's sgnl://linkdevice?…) */
  qr_payload: string | null
  /** Consent page to open in the system browser */
  auth_url: string | null
  /** Answer with {} every few seconds until the step changes */
  poll: boolean
  /** 0–1 completion of a `progress` step (e.g. a one-time setup download) */
  progress?: number | null
  /** Button label of a `fields` step (default "Continue") */
  submit_label?: string | null
}

/** One connector's describe() */
export interface Account {
  account_key: string
  account_id: number | null
  channel: ChannelName
  display_name: string
  /** Account handle (`ann@gmail.com`, `+1555…`) when the connector reports one */
  label?: string | null
  capabilities: string[]
  configured: boolean
  status: { state: ConnectorState; error: string | null }
}

/** A channel that can be connected */
export interface ConnectableChannel {
  channel: ChannelName
  display_name: string
  multi_account: boolean
}

export interface AccountsResponse {
  accounts: Account[]
  channels: ConnectableChannel[]
}

export const getAccounts = (signal?: AbortSignal) =>
  request<AccountsResponse>('/api/accounts', { signal })

const enc = encodeURIComponent

/**
 * Start a sign-in. `key` is an account key to re-authorize, a single-account
 * channel, or a multi-account channel name to add a new account.
 */
export const beginAccountAuth = (key: string, params: Record<string, unknown> = {}) =>
  request<AuthStep>(`/api/accounts/${enc(key)}/auth`, {
    method: 'POST',
    body: JSON.stringify(params),
  })

/** Answer the current step; resolves to the next one. */
export const continueAccountAuth = (key: string, flowId: string, data: Record<string, string> = {}) =>
  request<AuthStep>(`/api/accounts/${enc(key)}/auth/${enc(flowId)}`, {
    method: 'POST',
    body: JSON.stringify(data),
  })

export const logoutAccount = (key: string) =>
  request<{ success: boolean }>(`/api/accounts/${enc(key)}/logout`, { method: 'POST' })

/** Disconnect an account of a multi-account channel and delete what was synced from it. */
export const removeAccount = (key: string) =>
  request<{ success: boolean }>(`/api/accounts/${enc(key)}`, { method: 'DELETE' })

/** An account that holds credentials — the app has something to show. */
export const hasConnectedAccount = (accounts: Account[]) => accounts.some((a) => a.configured)
