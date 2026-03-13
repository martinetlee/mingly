import { useEffect } from 'react'

type ShortcutMap = Record<string, () => void>

function isInputFocused(): boolean {
  const el = document.activeElement
  if (!el) return false
  const tag = el.tagName
  if (tag === 'SELECT') return true
  if ((el as HTMLElement).isContentEditable) return true
  if (tag === 'INPUT' || tag === 'TEXTAREA') return true
  return false
}

function buildCombo(e: KeyboardEvent): string {
  const parts: string[] = []
  if (e.metaKey || e.ctrlKey) parts.push('mod')
  if (e.shiftKey) parts.push('shift')
  if (e.altKey) parts.push('alt')
  const key = e.key.toLowerCase()
  parts.push(key)
  return parts.join('+')
}

export function isShortcutsEnabled(): boolean {
  return localStorage.getItem('shortcuts_enabled') !== 'false'
}

export function useKeyboardShortcuts(shortcuts: ShortcutMap, enabled = true) {
  useEffect(() => {
    if (!enabled) return

    const handler = (e: KeyboardEvent) => {
      if (!isShortcutsEnabled()) return
      const combo = buildCombo(e)
      const action = shortcuts[combo]
      if (!action) return

      // Always allow mod+ shortcuts and escape, even in inputs
      const isMod = combo.startsWith('mod+')
      const isEscape = e.key === 'Escape'

      if (!isMod && !isEscape && isInputFocused()) return

      e.preventDefault()
      action()
    }

    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [shortcuts, enabled])
}
