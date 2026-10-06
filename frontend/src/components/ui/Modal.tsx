import { useEffect, useId, useLayoutEffect, useRef } from 'react'
import type { ReactNode, RefObject } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { popModalScope, pushModalScope, isTopModalScope } from '../../lib/keyboard'

export interface ModalProps {
  open: boolean
  onClose: () => void
  /** Header title; omit to render children only (no header) */
  title?: ReactNode
  /** Small icon/badge left of the title */
  icon?: ReactNode
  children?: ReactNode
  /** Right-aligned footer row (buttons) */
  footer?: ReactNode
  size?: 'sm' | 'md' | 'lg' | 'xl'
  /** Click on the backdrop closes (default true) */
  closeOnBackdrop?: boolean
  /** Escape closes (default true) */
  closeOnEscape?: boolean
  /** Show the × button in the header (default true when there's a title) */
  showCloseButton?: boolean
  /** Element to focus on open; defaults to the first focusable element */
  initialFocusRef?: RefObject<HTMLElement>
  /** Vertical placement: centered (default) or near the top (palettes) */
  position?: 'center' | 'top'
  /** Extra classes for the panel */
  className?: string
  /** Accessible label when there's no title */
  ariaLabel?: string
}

const SIZE: Record<NonNullable<ModalProps['size']>, string> = {
  sm: 'max-w-sm',
  md: 'max-w-md',
  lg: 'max-w-lg',
  xl: 'max-w-2xl',
}

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"]), [contenteditable="true"]'

function focusables(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => !el.hasAttribute('data-focus-skip'))
}

/**
 * The app's dialog shell: portal, backdrop, Escape, focus trap, focus
 * restore, and a keyboard-shortcut scope that suspends page shortcuts while
 * open. Render it conditionally or with `open` — state inside `children`
 * is discarded on close either way (the panel unmounts).
 */
export default function Modal({
  open, onClose, title, icon, children, footer, size = 'md',
  closeOnBackdrop = true, closeOnEscape = true, showCloseButton,
  initialFocusRef, position = 'center', className = '', ariaLabel,
}: ModalProps) {
  const panelRef = useRef<HTMLDivElement>(null)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const titleId = useId()

  // Shortcut scope + Escape. Layout effect so the scope is active before any
  // keystroke can reach page shortcuts.
  useLayoutEffect(() => {
    if (!open) return
    const scope = pushModalScope()
    const onKey = (e: KeyboardEvent) => {
      if (!isTopModalScope(scope)) return
      if (e.key === 'Escape' && closeOnEscape) {
        e.preventDefault()
        e.stopPropagation()
        onCloseRef.current()
        return
      }
      if (e.key === 'Tab' && panelRef.current) {
        const items = focusables(panelRef.current)
        if (items.length === 0) { e.preventDefault(); panelRef.current.focus(); return }
        const first = items[0]
        const last = items[items.length - 1]
        const active = document.activeElement
        if (e.shiftKey && (active === first || !panelRef.current.contains(active))) {
          e.preventDefault(); last.focus()
        } else if (!e.shiftKey && (active === last || !panelRef.current.contains(active))) {
          e.preventDefault(); first.focus()
        }
      }
    }
    // Capture phase: the modal sees Escape before page listeners
    window.addEventListener('keydown', onKey, true)
    return () => {
      window.removeEventListener('keydown', onKey, true)
      popModalScope(scope)
    }
  }, [open, closeOnEscape])

  // Initial focus + restore focus on close
  useEffect(() => {
    if (!open) return
    const previouslyFocused = document.activeElement as HTMLElement | null
    const panel = panelRef.current
    if (panel && !panel.contains(document.activeElement)) {
      const target = initialFocusRef?.current ?? focusables(panel)[0] ?? panel
      target.focus()
    }
    return () => {
      if (previouslyFocused && previouslyFocused.isConnected) previouslyFocused.focus()
    }
    // initialFocusRef is a ref: reading .current at open time is intended
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  if (!open) return null

  const withClose = showCloseButton ?? title != null

  return createPortal(
    <div
      className={`fixed inset-0 z-50 flex justify-center px-4 ${position === 'top' ? 'items-start pt-[15vh]' : 'items-center'}`}
      data-modal-root=""
    >
      <div
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        data-testid="modal-backdrop"
        onClick={closeOnBackdrop ? () => onCloseRef.current() : undefined}
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={title != null ? titleId : undefined}
        aria-label={title == null ? ariaLabel : undefined}
        tabIndex={-1}
        className={`relative flex max-h-[85vh] w-full flex-col overflow-hidden rounded-xl border border-slate-700/80 bg-slate-800 shadow-2xl outline-none ${SIZE[size]} ${className}`}
      >
        {title != null && (
          <div className="flex items-center justify-between gap-3 border-b border-slate-700/50 px-5 py-3.5">
            <div className="flex min-w-0 items-center gap-3">
              {icon}
              <h2 id={titleId} className="truncate text-sm font-semibold text-gray-100">{title}</h2>
            </div>
            {withClose && (
              <button
                type="button"
                onClick={() => onCloseRef.current()}
                aria-label="Close"
                data-focus-skip=""
                className="rounded-lg p-1.5 text-slate-400 transition-colors hover:bg-slate-700 hover:text-slate-200"
              >
                <X size={16} />
              </button>
            )}
          </div>
        )}
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
        {footer != null && (
          <div className="flex justify-end gap-2 border-t border-slate-700 px-5 py-3">{footer}</div>
        )}
      </div>
    </div>,
    document.body,
  )
}

/** Button styles matching the dialog visual language */
export const modalButton = {
  secondary: 'rounded-lg px-3 py-1.5 text-xs text-slate-400 transition-colors hover:bg-slate-700 disabled:opacity-50',
  primary: 'rounded-lg bg-indigo-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-indigo-400 disabled:opacity-50',
  danger: 'rounded-lg bg-red-500/20 px-3 py-1.5 text-xs font-medium text-red-400 transition-colors hover:bg-red-500/30 disabled:opacity-50',
}
