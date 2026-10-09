/**
 * Capability gating (schema v2). A conversation lists what its connector
 * supports; every feature that only some channels/conversations support
 * (forum topics, moderation, reactions, edit, typing, presence, sending…)
 * renders only when the flag is present. Gate through `can()` everywhere so
 * the same feature behaves the same in every place it appears.
 */
import type { Capability, Conversation } from '../api/types'

export function can(conversation: Pick<Conversation, 'capabilities'> | null | undefined, cap: Capability): boolean {
  return !!conversation?.capabilities?.includes(cap)
}
