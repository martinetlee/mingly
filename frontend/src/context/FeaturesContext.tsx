import { createContext, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { getFeatures } from '../api/client'
import type { Features } from '../api/types'
import { navShortcuts, visibleNav } from '../lib/nav'

/**
 * Backend feature switches (GET /api/features). Everything defaults to OFF
 * until the backend says otherwise, so a disabled feature never flashes on
 * screen while loading or when the request fails.
 */
export const DEFAULT_FEATURES: Features = { shield: false }

interface FeaturesState { features: Features; loaded: boolean }

const FeaturesContext = createContext<FeaturesState>({ features: DEFAULT_FEATURES, loaded: false })

/** Mount once inside the authenticated tree. Pass `value` to pin features (tests). */
export function FeaturesProvider({ children, value }: { children: ReactNode; value?: Features }) {
  const [state, setState] = useState<FeaturesState>(
    value ? { features: value, loaded: true } : { features: DEFAULT_FEATURES, loaded: false },
  )

  useEffect(() => {
    if (value) {
      setState({ features: value, loaded: true })
      return
    }
    const controller = new AbortController()
    getFeatures(controller.signal)
      .then((f) => setState({ features: { ...DEFAULT_FEATURES, ...f }, loaded: true }))
      // Keep defaults (all off) — never surface a disabled feature on error
      .catch(() => { if (!controller.signal.aborted) setState({ features: DEFAULT_FEATURES, loaded: true }) })
    return () => controller.abort()
  }, [value])

  return <FeaturesContext.Provider value={state}>{children}</FeaturesContext.Provider>
}

export function useFeatures(): Features {
  return useContext(FeaturesContext).features
}

/** False until the backend answered (or failed) — for redirects that must not fire early */
export function useFeaturesLoaded(): boolean {
  return useContext(FeaturesContext).loaded
}

/** Sidebar items and ⌘-number shortcuts for the current features */
export function useNav() {
  const features = useFeatures()
  return useMemo(() => ({ nav: visibleNav(features), shortcuts: navShortcuts(features) }), [features])
}
