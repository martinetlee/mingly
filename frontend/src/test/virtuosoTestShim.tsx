/**
 * Test-only wrapper around react-virtuoso for jsdom.
 *
 * Use with `vi.mock('react-virtuoso', () => import('…/virtuosoTestShim'))` and
 * render inside <VirtuosoMockContext.Provider> (fixed item/viewport sizes).
 * Everything is the real Virtuoso except `initialTopMostItemIndex`, which never
 * settles under jsdom: Virtuoso waits for scroll/measurement frames that jsdom
 * doesn't produce, so the list stays empty. (Verified: same list renders with
 * firstItemIndex alone, and renders nothing once initialTopMostItemIndex is set.)
 */
import { forwardRef } from 'react'
import * as real from 'react-virtuoso'
import type { VirtuosoHandle, VirtuosoProps } from 'react-virtuoso'

export * from 'react-virtuoso'

export const Virtuoso = forwardRef<VirtuosoHandle, VirtuosoProps<unknown, unknown>>(function VirtuosoShim(props, ref) {
  return <real.Virtuoso {...props} initialTopMostItemIndex={undefined} ref={ref} />
}) as typeof real.Virtuoso
