import { useEffect, useRef, useState } from 'react'
import { getOrganizations } from '../../api/client'
import { useDebouncedQuery } from '../../hooks/useDebouncedQuery'

/**
 * A text input that shows a dropdown of matching organization names while typing.
 * Allows free-text entry (for creating new orgs) as well as selecting an existing one.
 */
export default function ComboInput({
  value,
  onChange,
  placeholder = 'Organization name...',
  autoFocus = false,
  className = '',
}: {
  value: string
  onChange: (val: string) => void
  placeholder?: string
  autoFocus?: boolean
  className?: string
}) {
  // The dropdown opens only for text the user typed — picking a suggestion
  // (or a parent resetting the value) must not reopen it
  const [open, setOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const containerRef = useRef<HTMLDivElement>(null)

  // Latest-only: an older query's results can never replace a newer one's
  const { data, debouncedQuery } = useDebouncedQuery(
    value,
    (q, signal) => getOrganizations(q, signal),
    { delayMs: 150, keepPreviousData: false, enabled: open },
  )
  const suggestions = data ?? []

  useEffect(() => { setActiveIndex(-1) }, [debouncedQuery])

  // Close dropdown when clicking outside
  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [])

  const handleSelect = (name: string) => {
    setOpen(false)
    onChange(name)
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (!open || suggestions.length === 0) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIndex((i) => Math.min(i + 1, suggestions.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIndex((i) => Math.max(i - 1, -1))
    } else if (e.key === 'Enter' && activeIndex >= 0) {
      e.preventDefault()
      handleSelect(suggestions[activeIndex].name)
    } else if (e.key === 'Escape') {
      setOpen(false)
    }
  }

  return (
    <div ref={containerRef} className="relative w-full">
      <input
        autoFocus={autoFocus}
        value={value}
        onChange={(e) => { setOpen(true); onChange(e.target.value) }}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        className={`w-full bg-slate-700 border border-slate-600 rounded px-2 py-1 text-xs text-slate-200 outline-none focus:border-indigo-500 ${className}`}
      />
      {open && suggestions.length > 0 && (
        <div className="absolute left-0 top-full z-30 mt-0.5 w-full rounded-lg border border-slate-600 bg-slate-800 shadow-xl overflow-hidden">
          <div className="max-h-40 overflow-y-auto py-1">
            {suggestions.map((org, i) => (
              <button
                key={org.id}
                type="button"
                onMouseDown={(e) => { e.preventDefault(); handleSelect(org.name) }}
                className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors ${
                  i === activeIndex
                    ? 'bg-indigo-500/20 text-indigo-300'
                    : 'text-slate-300 hover:bg-slate-700'
                }`}
              >
                {org.name}
                {(org.member_count > 0 || org.former_member_count > 0) && (
                  <span className="ml-auto text-[10px] text-slate-500">
                    {org.member_count} member{org.member_count !== 1 ? 's' : ''}
                    {org.former_member_count > 0 && `, ${org.former_member_count} former`}
                  </span>
                )}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
