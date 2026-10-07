import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

/** Keep only ids still present in the list (same Set back when nothing changed). */
export function pruneSelection(selected: ReadonlySet<string>, presentIds: ReadonlySet<string>): Set<string> {
  let changed = false
  const next = new Set<string>()
  for (const id of selected) {
    if (presentIds.has(id)) next.add(id)
    else changed = true
  }
  return changed ? next : (selected as Set<string>)
}

/**
 * Ids between the anchor and the target (inclusive) in the list's *current*
 * order. If the anchor is no longer in the list, only the target is returned.
 */
export function rangeIds(orderedIds: readonly string[], anchorId: string | null, targetId: string): string[] {
  const to = orderedIds.indexOf(targetId)
  if (to < 0) return []
  const from = anchorId == null ? -1 : orderedIds.indexOf(anchorId)
  if (from < 0) return [targetId]
  const [a, b] = from <= to ? [from, to] : [to, from]
  return orderedIds.slice(a, b + 1)
}

export interface Selection<T> {
  /** Selected ids, always a subset of the current items */
  selectedIds: Set<string>
  /** Selected items in list order — exactly what a bulk action affects */
  selectedItems: T[]
  count: number
  allSelected: boolean
  isSelected(id: string): boolean
  toggle(id: string): void
  /** Row click: shift extends from the anchor (by id), otherwise toggles. */
  click(id: string, opts?: { shift?: boolean }): void
  toggleAll(): void
  clear(): void
}

/**
 * Multi-select keyed by stable item ids.
 * - Selection is pruned whenever `items` changes (reloads, WS refetches,
 *   removed rows), so the count never includes rows that aren't shown.
 * - The shift-click anchor is an id, not an index, so it survives inserts and
 *   reorders; a vanished anchor degrades to a plain toggle.
 */
export function useSelection<T>(items: readonly T[], getId: (item: T) => string): Selection<T> {
  const getIdRef = useRef(getId)
  getIdRef.current = getId
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  const anchorRef = useRef<string | null>(null)

  const orderedIds = useMemo(() => items.map((i) => getIdRef.current(i)), [items])
  const presentIds = useMemo(() => new Set(orderedIds), [orderedIds])

  // Drop selections whose rows are gone (state stays in sync with what's shown)
  useEffect(() => {
    setSelected((prev) => pruneSelection(prev, presentIds))
    if (anchorRef.current != null && !presentIds.has(anchorRef.current)) anchorRef.current = null
  }, [presentIds])

  // Derive from the current items too, so even the render before the prune
  // effect runs never reports a stale count
  const effective = useMemo(() => pruneSelection(selected, presentIds), [selected, presentIds])
  const selectedItems = useMemo(
    () => items.filter((i) => effective.has(getIdRef.current(i))),
    [items, effective],
  )

  const toggle = useCallback((id: string) => {
    anchorRef.current = id
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  const click = useCallback((id: string, opts?: { shift?: boolean }) => {
    if (opts?.shift && anchorRef.current != null) {
      const ids = rangeIds(orderedIds, anchorRef.current, id)
      setSelected((prev) => {
        const next = new Set(prev)
        ids.forEach((x) => next.add(x))
        return next
      })
      anchorRef.current = id
      return
    }
    toggle(id)
  }, [orderedIds, toggle])

  const allSelected = orderedIds.length > 0 && effective.size === orderedIds.length

  const toggleAll = useCallback(() => {
    setSelected(allSelected ? new Set() : new Set(orderedIds))
  }, [allSelected, orderedIds])

  const clear = useCallback(() => {
    anchorRef.current = null
    setSelected(new Set())
  }, [])

  return {
    selectedIds: effective,
    selectedItems,
    count: selectedItems.length,
    allSelected,
    isSelected: (id: string) => effective.has(id),
    toggle,
    click,
    toggleAll,
    clear,
  }
}
