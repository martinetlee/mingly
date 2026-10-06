import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { pruneSelection, rangeIds, useSelection } from '../useSelection'

type Row = { id: string }
const rows = (...ids: string[]): Row[] => ids.map((id) => ({ id }))
const getId = (r: Row) => r.id

describe('pruneSelection / rangeIds', () => {
  it('drops ids no longer present and keeps identity when nothing changed', () => {
    const sel = new Set(['a', 'b'])
    expect(pruneSelection(sel, new Set(['a', 'b', 'c']))).toBe(sel)
    expect([...pruneSelection(sel, new Set(['b', 'c']))]).toEqual(['b'])
  })

  it('ranges by id in the current order, either direction', () => {
    expect(rangeIds(['a', 'b', 'c', 'd'], 'b', 'd')).toEqual(['b', 'c', 'd'])
    expect(rangeIds(['a', 'b', 'c', 'd'], 'd', 'b')).toEqual(['b', 'c', 'd'])
  })

  it('a vanished anchor selects only the target', () => {
    expect(rangeIds(['a', 'b', 'c'], 'zzz', 'c')).toEqual(['c'])
    expect(rangeIds(['a', 'b', 'c'], null, 'b')).toEqual(['b'])
  })
})

describe('useSelection', () => {
  it('prunes against each newly loaded list so the count equals what a bulk action affects', () => {
    const { result, rerender } = renderHook(({ items }) => useSelection(items, getId), {
      initialProps: { items: rows('a', 'b', 'c') },
    })
    act(() => { result.current.toggle('a'); result.current.toggle('b'); result.current.toggle('c') })
    expect(result.current.count).toBe(3)

    // A WS-triggered reload no longer contains 'b'
    rerender({ items: rows('new', 'a', 'c') })
    expect(result.current.count).toBe(2)
    expect(result.current.selectedItems.map(getId)).toEqual(['a', 'c'])
    expect([...result.current.selectedIds].sort()).toEqual(['a', 'c'])

    // 'b' coming back later must not resurrect its old selection
    rerender({ items: rows('a', 'b', 'c') })
    expect(result.current.isSelected('b')).toBe(false)
    expect(result.current.count).toBe(2)
  })

  it('keeps the shift-range anchor by id across inserts at the top', () => {
    const { result, rerender } = renderHook(({ items }) => useSelection(items, getId), {
      initialProps: { items: rows('a', 'b', 'c', 'd', 'e') },
    })
    act(() => result.current.click('b'))
    // Two live rows inserted above: indices shift by 2, ids don't
    rerender({ items: rows('x', 'y', 'a', 'b', 'c', 'd', 'e') })
    act(() => result.current.click('d', { shift: true }))
    expect(result.current.selectedItems.map(getId)).toEqual(['b', 'c', 'd'])
  })

  it('shift-click with a pruned anchor degrades to a single toggle', () => {
    const { result, rerender } = renderHook(({ items }) => useSelection(items, getId), {
      initialProps: { items: rows('a', 'b', 'c', 'd') },
    })
    act(() => result.current.click('a'))
    rerender({ items: rows('b', 'c', 'd') }) // anchor 'a' removed
    act(() => result.current.click('d', { shift: true }))
    expect(result.current.selectedItems.map(getId)).toEqual(['d'])
  })

  it('toggleAll selects exactly the loaded rows, then clears', () => {
    const { result } = renderHook(() => useSelection(rows('a', 'b'), getId))
    act(() => result.current.toggleAll())
    expect(result.current.allSelected).toBe(true)
    expect(result.current.count).toBe(2)
    act(() => result.current.toggleAll())
    expect(result.current.count).toBe(0)
  })
})
