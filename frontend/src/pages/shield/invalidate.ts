import { useEffect, useRef } from 'react'

/**
 * Page-local "server scam data changed" signal. Every verdict / dismiss /
 * flag mutation calls invalidateShieldData(); every Shield list (scanned,
 * unmarked, flags) refetches on it, so no view keeps showing pre-mutation
 * rows.
 */
const listeners = new Set<() => void>()

export function invalidateShieldData() {
  listeners.forEach((l) => l())
}

export function useShieldInvalidation(onInvalidate: () => void) {
  const ref = useRef(onInvalidate)
  ref.current = onInvalidate
  useEffect(() => {
    const l = () => ref.current()
    listeners.add(l)
    return () => { listeners.delete(l) }
  }, [])
}
