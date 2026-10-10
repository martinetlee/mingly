/** Channels and what a connector can do. */

/** Messaging channels a connector can provide (string-typed: never assume Telegram) */
export type ChannelName = 'telegram' | 'gmail' | 'signal' | 'sms' | (string & {})

/** What a conversation's connector supports (backend connectors/base.py Capability) */
export type Capability =
  | 'send'
  | 'edit'
  | 'delete'
  | 'react'
  | 'reply'
  | 'media_send'
  | 'pin'
  | 'forward'
  | 'read_receipts'
  | 'forum_topics'
  | 'admin_moderation'
  | 'history_backfill'
  | 'subject'
  | 'html_body'
  | 'labels'
  | 'typing'
  | 'presence'
  /** Listing a group's / channel's members */
  | 'members'

/** connector_status `state` (backend ConnectorStatus) */
export type ConnectorState = 'starting' | 'syncing' | 'ready' | 'reconnecting' | 'unauthorized' | 'error' | 'stopped'
