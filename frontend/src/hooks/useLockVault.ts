import { lockVault } from '../api/client'
import { useAsyncAction } from './useAsyncAction'

/**
 * Lock the vault from anywhere (Sidebar button, ⌘⇧L). Single-flight; on
 * success the app moves to Unlock via the session-expiry path, on failure an
 * error toast is shown and the app stays as is.
 */
export function useLockVault() {
  const { run, pending } = useAsyncAction(lockVault, { errorContext: 'Lock failed' })
  return { lock: run, locking: pending }
}
