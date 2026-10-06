import { useLayoutEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { isTopModalScope, popModalScope, pushModalScope } from '../lib/keyboard'

/**
 * Full-screen media overlay. Registers a modal keyboard scope (page
 * shortcuts such as j/k are suspended while it's open), closes on Escape and
 * backdrop click, and locks background scrolling.
 */
export function LightboxShell({ onClose, children, onKey }: {
  onClose: () => void
  children: ReactNode
  /** Extra keys while open (e.g. Space = play/pause); return true if handled */
  onKey?: (e: KeyboardEvent) => boolean
}) {
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const onKeyRef = useRef(onKey)
  onKeyRef.current = onKey

  useLayoutEffect(() => {
    const scope = pushModalScope()
    const handler = (e: KeyboardEvent) => {
      if (!isTopModalScope(scope)) return
      if (e.key === 'Escape') {
        e.preventDefault()
        e.stopPropagation()
        onCloseRef.current()
        return
      }
      if (onKeyRef.current?.(e)) e.preventDefault()
    }
    window.addEventListener('keydown', handler, true)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', handler, true)
      document.body.style.overflow = prev
      popModalScope(scope)
    }
  }, [])

  return createPortal(
    <div
      className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/85 backdrop-blur-sm"
      onClick={() => onCloseRef.current()}
      role="dialog"
      aria-modal="true"
    >
      <button
        onClick={() => onCloseRef.current()}
        className="absolute top-4 right-4 z-10 rounded-full bg-black/50 p-2 text-white/80 hover:bg-black/70 hover:text-white transition-all"
        title="Close"
        aria-label="Close"
      >
        <X size={24} />
      </button>
      <div onClick={(e) => e.stopPropagation()}>{children}</div>
    </div>,
    document.body,
  )
}

interface Props {
  src: string
  alt?: string
  onClose: () => void
}

export default function ImageLightbox({ src, alt = 'photo', onClose }: Props) {
  return (
    <LightboxShell onClose={onClose}>
      <img
        src={src}
        alt={alt}
        className="max-h-[90vh] max-w-[90vw] rounded-lg object-contain shadow-2xl"
        draggable={false}
      />
    </LightboxShell>
  )
}
