/**
 * Global toast store. Usable from anywhere (React or not); rendered by
 * <ToastProvider> (components/ui/Toast.tsx). In React prefer useToast().
 */
import { isAbortError, isSessionExpiredError } from './http'

export type ToastKind = 'success' | 'error' | 'info'

export interface ToastItem {
  id: number
  kind: ToastKind
  message: string
  /** ms before auto-dismiss; 0 keeps it until closed */
  duration: number
}

export interface ToastOptions {
  duration?: number
}

type Listener = (toasts: ToastItem[]) => void

const DEFAULT_DURATION: Record<ToastKind, number> = { success: 3000, info: 4000, error: 6000 }
const MAX_VISIBLE = 4
// Same message within this window is collapsed (e.g. a burst of failing polls)
const DEDUPE_MS = 2000

let toasts: ToastItem[] = []
let nextId = 1
const listeners = new Set<Listener>()
const timers = new Map<number, ReturnType<typeof setTimeout>>()
const recent = new Map<string, number>()

function emit() {
  for (const l of [...listeners]) l(toasts)
}

export function subscribeToasts(listener: Listener): () => void {
  listeners.add(listener)
  listener(toasts)
  return () => { listeners.delete(listener) }
}

export function dismissToast(id: number): void {
  const t = timers.get(id)
  if (t) { clearTimeout(t); timers.delete(id) }
  const before = toasts.length
  toasts = toasts.filter((x) => x.id !== id)
  if (toasts.length !== before) emit()
}

export function showToast(kind: ToastKind, message: string, opts: ToastOptions = {}): number {
  const key = `${kind}:${message}`
  const now = Date.now()
  const last = recent.get(key)
  const dup = toasts.find((t) => t.kind === kind && t.message === message)
  if (dup && last != null && now - last < DEDUPE_MS) return dup.id
  recent.set(key, now)

  const item: ToastItem = { id: nextId++, kind, message, duration: opts.duration ?? DEFAULT_DURATION[kind] }
  toasts = [...toasts, item]
  while (toasts.length > MAX_VISIBLE) dismissToastSilently(toasts[0].id)
  if (item.duration > 0) {
    timers.set(item.id, setTimeout(() => dismissToast(item.id), item.duration))
  }
  emit()
  return item.id
}

function dismissToastSilently(id: number) {
  const t = timers.get(id)
  if (t) { clearTimeout(t); timers.delete(id) }
  toasts = toasts.filter((x) => x.id !== id)
}

/** Test helper: drop all toasts and timers. */
export function resetToasts(): void {
  for (const t of timers.values()) clearTimeout(t)
  timers.clear()
  recent.clear()
  toasts = []
  emit()
}

export const toast = {
  success: (message: string, opts?: ToastOptions) => showToast('success', message, opts),
  error: (message: string, opts?: ToastOptions) => showToast('error', message, opts),
  info: (message: string, opts?: ToastOptions) => showToast('info', message, opts),
  dismiss: dismissToast,
}

export function errorMessage(e: unknown): string {
  if (e instanceof Error) return e.message || e.name
  if (typeof e === 'string') return e
  return 'Unknown error'
}

/**
 * Replacement for swallowed `.catch(() => {})`: logs the error and shows an
 * error toast "<context>: <message>". Aborts and session expiry (which has
 * its own global handling) are ignored. Returns undefined so it can be used
 * directly as a catch handler:  promise.catch((e) => reportError(e, 'Save tag'))
 */
export function reportError(e: unknown, context?: string): undefined {
  if (isAbortError(e) || isSessionExpiredError(e)) return undefined
  console.error(context ?? 'Error', e)
  const msg = errorMessage(e)
  toast.error(context ? `${context}: ${msg}` : msg)
  return undefined
}
