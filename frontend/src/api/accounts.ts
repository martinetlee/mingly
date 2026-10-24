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
  /**
   * The step's background work (a setup download) keeps going when the flow
   * is left; this labels the button that stops it (cancel with `stop`).
   */
  stop_label?: string | null
  /** Secondary button of a `fields` step ("Cancel" when absent), e.g. "Skip" */
  cancel_label?: string | null
}

/** Something more the user can do with an account (Settings › Connections): an auth flow started with `params` */
export interface AccountAction {
  id: string
  label: string
  params: Record<string, unknown>
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
  /** Extra actions for this account (e.g. Telegram's API credentials, Signal's Desktop import) */
  actions?: AccountAction[]
}

/** A channel's one-time setup before its first sign-in (Signal's signal-cli download) */
export interface ChannelSetup {
  state: 'running' | 'cancelling' | 'ready' | 'failed' | (string & {})
  /** 0–1 while running or cancelling */
  progress: number | null
  message: string | null
}

/** A channel that can be connected */
export interface ConnectableChannel {
  channel: ChannelName
  display_name: string
  multi_account: boolean
  /** Setup running or just ended; null when none */
  setup?: ChannelSetup | null
}

export interface AccountsResponse {
  accounts: Account[]
  channels: ConnectableChannel[]
}

/** How often a polling sign-in step, and a running channel setup, is re-read */
export const SIGN_IN_POLL_MS = 2000

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

/**
 * Abandon a sign-in flow (the user left it): the connector stops what the
 * flow holds open — e.g. Signal's `signal-cli link` process. Background work
 * the flow started (a setup download) keeps going unless `stop`.
 */
export const cancelAccountAuth = (key: string, flowId: string, { stop = false }: { stop?: boolean } = {}) =>
  request<void>(`/api/accounts/${enc(key)}/auth/${enc(flowId)}${stop ? '?stop=true' : ''}`, { method: 'DELETE' })

export const logoutAccount = (key: string) =>
  request<{ success: boolean }>(`/api/accounts/${enc(key)}/logout`, { method: 'POST' })

/** Disconnect an account of a multi-account channel and delete what was synced from it. */
export const removeAccount = (key: string) =>
  request<{ success: boolean }>(`/api/accounts/${enc(key)}`, { method: 'DELETE' })

/** An account that holds credentials — the app has something to show. */
export const hasConnectedAccount = (accounts: Account[]) => accounts.some((a) => a.configured)
