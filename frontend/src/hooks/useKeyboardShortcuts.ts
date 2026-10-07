import { useEffect, useRef } from 'react'
import {
  buildCombo, getLastPointerTarget, inScrollableRegion, isActivatable, isModalOpen,
  isScrollKey, isShortcutsEnabled, isTextEntry,
} from '../lib/keyboard'

export { isShortcutsEnabled }

/**
 * Return `false` when the shortcut didn't apply (the event then keeps its
 * default behaviour and other listeners may handle it). Returning nothing
 * counts as handled → preventDefault.
 */
export type ShortcutHandler = (e: KeyboardEvent) => boolean | void
export type ShortcutMap = Record<string, ShortcutHandler>

export interface ShortcutOptions {
  enabled?: boolean
  /** Keep firing while a <Modal> is open (default false: modals suspend shortcuts) */
  allowInModal?: boolean
  /**
   * Arrow/Page/Home/End/Space yield to native scrolling when focus or the last
   * click is inside a scrollable region (default true).
   */
  yieldScrollKeys?: boolean
}

function eventTarget(e: KeyboardEvent): Element | null {
  const t = e.target
  if (t instanceof Element && t !== document.body) return t
  return document.activeElement && document.activeElement !== document.body ? document.activeElement : null
}

/** Decide whether `e` should reach a shortcut handler (exported for tests). */
export function shouldHandle(e: KeyboardEvent, combo: string, opts: ShortcutOptions): boolean {
  if (isModalOpen() && !opts.allowInModal) return false
  const isMod = combo.startsWith('mod+')
  const isEscape = e.key === 'Escape'
  // Mod combos and Escape work everywhere, including text fields
  if (isMod || isEscape) return true
  const target = eventTarget(e)
  if (isTextEntry(target)) return false
  // Enter/Space on a button or link must activate it, not trigger a shortcut
  if ((e.key === 'Enter' || e.key === ' ') && isActivatable(target)) return false
  if ((opts.yieldScrollKeys ?? true) && isScrollKey(e)) {
    if (inScrollableRegion(target) || inScrollableRegion(getLastPointerTarget())) return false
  }
  return true
}

/**
 * Window-level keyboard shortcuts. Keys: "mod+k", "shift+?", "escape", "j",
 * "arrowdown"… (`mod` = ⌘ or Ctrl). The listener is registered once; the map
 * and options are read through refs, so passing a fresh object every render
 * is fine.
 */
export function useKeyboardShortcuts(shortcuts: ShortcutMap, enabledOrOptions: boolean | ShortcutOptions = true) {
  const opts: ShortcutOptions = typeof enabledOrOptions === 'boolean' ? { enabled: enabledOrOptions } : enabledOrOptions
  const shortcutsRef = useRef(shortcuts)
  shortcutsRef.current = shortcuts
  const optsRef = useRef(opts)
  optsRef.current = opts

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      // IME composition keystrokes belong to the input method
      if (e.isComposing) return
      const o = optsRef.current
      if (o.enabled === false || !isShortcutsEnabled()) return
      const combo = buildCombo(e)
      const action = shortcutsRef.current[combo]
      if (!action || !shouldHandle(e, combo, o)) return
      if (action(e) === false) return
      e.preventDefault()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])
}
