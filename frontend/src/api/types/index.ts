/**
 * API payload shapes — schema v2 (docs/multichannel/SCHEMA_V2.md).
 *
 * Every id is an internal id (`conversation_id`, `message_id`, `person_id`,
 * `identity_id`, `account_id`, `attachment_id`). A channel's own ids
 * (Telegram peer/user/message ids) are data (`external_id`, `peer_id`), never
 * keys. Conversation / Message / Person / Identity match backend/serializers.py.
 *
 * One module per domain; import from `api/types` (this barrel).
 */

export type * from './archive'
export type * from './catchup'
export type * from './channels'
export type * from './conversations'
export type * from './dashboard'
export type * from './events'
export type * from './intelligence'
export type * from './messages'
export type * from './organizations'
export type * from './people'
export type * from './search'
export type * from './settings'
export type * from './shield'
export type * from './telegram'
export type * from './workspaces'
