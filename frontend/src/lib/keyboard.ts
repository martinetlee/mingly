/**
 * Keyboard plumbing shared by useKeyboardShortcuts and <Modal>.
 *
 * Modal scope: every open <Modal> pushes a scope; while any scope is active,
 * page/global shortcuts are suspended (unless registered with allowInModal).
 * The top-most scope owns Escape.
 */

type ScopeId = number
let nextScope = 1
const scopeStack: ScopeId[] = []

export function pushModalScope(): ScopeId {
  const id = nextScope++
  scopeStack.push(id)
  return id
}

export function popModalScope(id: ScopeId): void {
  const i = scopeStack.lastIndexOf(id)
  if (i !== -1) scopeStack.splice(i, 1)
}

export function isModalOpen(): boolean {
  return scopeStack.length > 0
}

export function isTopModalScope(id: ScopeId): boolean {
  return scopeStack[scopeStack.length - 1] === id
}

/** Normalised combo string: "mod+shift+k", "escape", "arrowdown", "shift+?" */
export function buildCombo(e: KeyboardEvent): string {
  const parts: string[] = []
  if (e.metaKey || e.ctrlKey) parts.push('mod')
  if (e.shiftKey) parts.push('shift')
  if (e.altKey) parts.push('alt')
  parts.push(e.key.toLowerCase())
  return parts.join('+')
}

/** Elements that consume typed characters. */
export function isTextEntry(el: Element | null): boolean {
  if (!el || !(el instanceof HTMLElement)) return false
  if (el.isContentEditable || el.closest('[contenteditable=""],[contenteditable="true"]')) return true
  const tag = el.tagName
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true
  if (tag === 'INPUT') {
    const type = (el as HTMLInputElement).type
    return !['checkbox', 'radio', 'button', 'submit', 'reset', 'range', 'color', 'file'].includes(type)
  }
  return false
}

/** Elements that activate on Enter/Space (their own default action must win). */
export function isActivatable(el: Element | null): boolean {
  if (!el || !(el instanceof HTMLElement)) return false
  return !!el.closest('button, a[href], [role="button"], [role="link"], [role="menuitem"], [role="option"], [role="tab"], [role="checkbox"], [role="switch"], summary, input, select, textarea')
}

function isScrollableY(el: HTMLElement): boolean {
  if (el.scrollHeight <= el.clientHeight + 1) return false
  const oy = getComputedStyle(el).overflowY
  return oy === 'auto' || oy === 'scroll' || oy === 'overlay'
}

/** True if `el` or an ancestor (below <body>) is a vertically scrollable region. */
export function inScrollableRegion(el: Element | null): boolean {
  let cur = el instanceof HTMLElement ? el : null
  while (cur && cur !== document.body && cur !== document.documentElement) {
    if (isScrollableY(cur)) return true
    cur = cur.parentElement
  }
  return false
}

// Browsers scroll the region the user last clicked when arrow keys are
// pressed even though focus stays on <body>; track it so arrow shortcuts
// can yield to native scrolling.
let lastPointerTarget: Element | null = null
if (typeof document !== 'undefined') {
  document.addEventListener('pointerdown', (e) => {
    lastPointerTarget = e.target instanceof Element ? e.target : null
  }, true)
}

export function getLastPointerTarget(): Element | null {
  return lastPointerTarget && lastPointerTarget.isConnected ? lastPointerTarget : null
}

const ARROWS = new Set(['arrowup', 'arrowdown', 'arrowleft', 'arrowright', 'pageup', 'pagedown', 'home', 'end', ' '])
export function isScrollKey(e: KeyboardEvent): boolean {
  return ARROWS.has(e.key.toLowerCase())
}

export const SHORTCUTS_ENABLED_KEY = 'shortcuts_enabled'

export function isShortcutsEnabled(): boolean {
  try {
    return localStorage.getItem(SHORTCUTS_ENABLED_KEY) !== 'false'
  } catch {
    return true
  }
}
