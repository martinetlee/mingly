import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, cleanup } from '@testing-library/react'
import { useKeyboardShortcuts } from '../useKeyboardShortcuts'
import type { ShortcutMap } from '../useKeyboardShortcuts'
import { NAV, SETTINGS_NAV, navShortcuts, visibleNav } from '../../lib/nav'

afterEach(() => {
  cleanup()
  localStorage.clear()
})

function Harness({ map, children }: { map: ShortcutMap; children?: React.ReactNode }) {
  useKeyboardShortcuts(map)
  return <>{children}</>
}

function press(target: Element | Window, key: string, init: KeyboardEventInit = {}) {
  const ev = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init })
  target.dispatchEvent(ev)
  return ev
}

describe('useKeyboardShortcuts', () => {
  it('fires and preventDefaults only when the handler handled the key', () => {
    const j = vi.fn()
    const esc = vi.fn(() => false)
    render(<Harness map={{ j, escape: esc }} />)
    const ev = press(document.body, 'j')
    expect(j).toHaveBeenCalledTimes(1)
    expect(ev.defaultPrevented).toBe(true)
    const ev2 = press(document.body, 'Escape')
    expect(esc).toHaveBeenCalledTimes(1)
    expect(ev2.defaultPrevented).toBe(false)
  })

  it('lets Enter/Space activate a focused button instead of a shortcut', () => {
    const enter = vi.fn()
    render(<Harness map={{ enter, j: () => {} }}><button data-testid="b">Send</button></Harness>)
    const btn = document.querySelector('[data-testid="b"]')!
    const ev = press(btn, 'Enter')
    expect(enter).not.toHaveBeenCalled()
    expect(ev.defaultPrevented).toBe(false)
    press(document.body, 'Enter')
    expect(enter).toHaveBeenCalledTimes(1)
  })

  it('ignores plain keys in text fields but allows mod combos and Escape', () => {
    const j = vi.fn()
    const modK = vi.fn()
    const esc = vi.fn()
    render(<Harness map={{ j, 'mod+k': modK, escape: esc }}><input data-testid="i" /><div contentEditable data-testid="ce" /></Harness>)
    const input = document.querySelector('[data-testid="i"]')!
    press(input, 'j')
    press(document.querySelector('[data-testid="ce"]')!, 'j')
    press(input, 'k', { metaKey: true })
    press(input, 'Escape')
    expect(j).not.toHaveBeenCalled()
    expect(modK).toHaveBeenCalledTimes(1)
    expect(esc).toHaveBeenCalledTimes(1)
  })

  it('yields arrow keys to a scrollable region the user clicked into', () => {
    const down = vi.fn()
    render(<Harness map={{ arrowdown: down }}><div data-testid="pane" style={{ overflowY: 'auto' }}><p data-testid="msg">hi</p></div></Harness>)
    const pane = document.querySelector('[data-testid="pane"]') as HTMLElement
    Object.defineProperty(pane, 'scrollHeight', { value: 1000, configurable: true })
    Object.defineProperty(pane, 'clientHeight', { value: 100, configurable: true })
    fireEvent.pointerDown(document.querySelector('[data-testid="msg"]')!)
    const ev = press(document.body, 'ArrowDown')
    expect(down).not.toHaveBeenCalled()
    expect(ev.defaultPrevented).toBe(false) // native scrolling proceeds

    fireEvent.pointerDown(document.body)
    press(document.body, 'ArrowDown')
    expect(down).toHaveBeenCalledTimes(1)
  })

  it('registers one listener for the component lifetime and reads the latest map', () => {
    const add = vi.spyOn(window, 'addEventListener')
    const first = vi.fn()
    const second = vi.fn()
    const { rerender } = render(<Harness map={{ j: first }} />)
    rerender(<Harness map={{ j: second }} />)
    rerender(<Harness map={{ j: second }} />)
    expect(add.mock.calls.filter(([t]) => t === 'keydown')).toHaveLength(1)
    press(document.body, 'j')
    expect(first).not.toHaveBeenCalled()
    expect(second).toHaveBeenCalledTimes(1)
    add.mockRestore()
  })

  it('respects the global shortcuts toggle', () => {
    const j = vi.fn()
    localStorage.setItem('shortcuts_enabled', 'false')
    render(<Harness map={{ j }} />)
    press(document.body, 'j')
    expect(j).not.toHaveBeenCalled()
  })
})

describe('nav shortcuts', () => {
  it('are derived from the visible sidebar NAV in order, Settings last', () => {
    for (const features of [{ shield: false }, { shield: true }]) {
      const shortcuts = navShortcuts(features)
      expect(shortcuts.map((s) => s.item.to)).toEqual([...visibleNav(features), SETTINGS_NAV].map((n) => n.to))
      expect(shortcuts[0]).toMatchObject({ combo: 'mod+1', keys: '⌘1' })
      expect(shortcuts.length).toBeLessThanOrEqual(9)
    }
  })

  it('hide Shield and renumber when the feature is off', () => {
    const off = navShortcuts({ shield: false }).map((s) => s.item.to)
    const on = navShortcuts({ shield: true }).map((s) => s.item.to)
    expect(off).not.toContain('/shield')
    expect(on).toContain('/shield')
    expect(off.length).toBe(on.length - 1)
    expect(NAV.some((n) => n.to === '/shield')).toBe(true) // still defined, only hidden
  })
})
