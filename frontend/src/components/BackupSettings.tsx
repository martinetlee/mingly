import { useState, useEffect, useCallback } from 'react'
import {
  getBackupStatus,
  enableBackup,
  disableBackup,
  restoreBackup,
  syncBackup,
  type BackupStatus,
} from '../api/client'

export default function BackupSettings() {
  const [status, setStatus] = useState<BackupStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [actionLoading, setActionLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)

  // Password input for enable/restore
  const [showPasswordInput, setShowPasswordInput] = useState<'enable' | 'restore' | null>(null)
  const [password, setPassword] = useState('')

  // Disable confirmation
  const [showDisableConfirm, setShowDisableConfirm] = useState(false)
  const [deleteRemote, setDeleteRemote] = useState(false)

  const fetchStatus = useCallback(async () => {
    try {
      const res = await getBackupStatus()
      setStatus(res)
    } catch {
      // Not connected or other error — leave status null
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchStatus()
  }, [fetchStatus])

  const clearMessages = () => {
    setError(null)
    setSuccess(null)
  }

  const handleEnable = useCallback(async () => {
    if (!password.trim()) return
    clearMessages()
    setActionLoading(true)
    try {
      const res = await enableBackup(password)
      setSuccess('Backup enabled and synced successfully.')
      setShowPasswordInput(null)
      setPassword('')
      setStatus({ enabled: true, last_sync: res.last_sync, dirty: false })
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to enable backup')
    } finally {
      setActionLoading(false)
    }
  }, [password])

  const handleDisable = useCallback(async () => {
    clearMessages()
    setActionLoading(true)
    try {
      await disableBackup(deleteRemote)
      setSuccess(deleteRemote ? 'Backup disabled and remote data deleted.' : 'Backup disabled.')
      setShowDisableConfirm(false)
      setDeleteRemote(false)
      setStatus({ enabled: false, last_sync: null, dirty: false })
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to disable backup')
    } finally {
      setActionLoading(false)
    }
  }, [deleteRemote])

  const handleRestore = useCallback(async () => {
    if (!password.trim()) return
    clearMessages()
    setActionLoading(true)
    try {
      const res = await restoreBackup(password)
      if (res.status === 'restored') {
        const s = res.import_summary
        const parts: string[] = []
        if (s?.contacts) {
          const c = s.contacts
          const cParts: string[] = []
          if (c.new > 0) cParts.push(`${c.new} new`)
          if (c.updated > 0) cParts.push(`${c.updated} updated`)
          if (c.skipped > 0) cParts.push(`${c.skipped} unchanged`)
          if (cParts.length > 0) parts.push(`Contacts: ${cParts.join(', ')}`)
        }
        if (s?.positions) {
          const p = s.positions
          const pParts: string[] = []
          if (p.new > 0) pParts.push(`${p.new} new`)
          if (p.updated > 0) pParts.push(`${p.updated} updated`)
          if (p.skipped > 0) pParts.push(`${p.skipped} unchanged`)
          if (pParts.length > 0) parts.push(`Positions: ${pParts.join(', ')}`)
        }
        const detail = parts.length > 0 ? ` ${parts.join('. ')}.` : ''
        setSuccess(`Backup restored and imported successfully.${detail}`)
      } else if (res.status === 'no_backup') {
        setError('No backup found on the server.')
      } else if (res.status === 'decrypt_failed') {
        setError(res.detail || 'Decryption failed. Check your master password.')
      } else {
        setError(`Unexpected status: ${res.status}`)
      }
      setShowPasswordInput(null)
      setPassword('')
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to restore backup')
    } finally {
      setActionLoading(false)
    }
  }, [password])

  const handleManualSync = useCallback(async () => {
    clearMessages()
    setActionLoading(true)
    try {
      const res = await syncBackup()
      if (res.status === 'synced') {
        setSuccess('Backup synced successfully.')
        setStatus((prev) => prev ? { ...prev, last_sync: res.last_sync || prev.last_sync, dirty: false } : prev)
      } else {
        setError(`Sync returned: ${res.status}`)
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to sync backup')
    } finally {
      setActionLoading(false)
    }
  }, [])

  if (loading) {
    return (
      <div className="mt-4">
        <h3 className="mb-1 text-sm font-medium text-slate-300">Personal Backup</h3>
        <p className="text-xs text-slate-500">Loading...</p>
      </div>
    )
  }

  return (
    <div className="mt-4">
      <h3 className="mb-1 text-sm font-medium text-slate-300">Personal Backup</h3>
      <p className="mb-3 text-xs text-slate-400">
        Encrypted backup of your personal contacts to Mingly Cloud.
      </p>

      <div className="rounded-xl border border-slate-700 bg-slate-800 p-4">
        {/* Key-loss warning */}
        <div className="mb-4 rounded-lg border border-amber-700/50 bg-amber-900/30 px-3 py-2 text-xs text-amber-300">
          Your cloud backup is encrypted with your master password. If you lose it, your backup cannot be recovered.
        </div>

        {/* Status display */}
        {status && (
          <div className="mb-4 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <span
                className={`inline-block h-2 w-2 rounded-full ${
                  status.enabled ? 'bg-green-400' : 'bg-slate-500'
                }`}
              />
              <span className="text-sm font-medium text-slate-200">
                {status.enabled ? 'Backup Enabled' : 'Backup Disabled'}
              </span>
            </div>
            {status.last_sync && (
              <span className="text-xs text-slate-400">
                Last sync: {new Date(status.last_sync).toLocaleString()}
              </span>
            )}
          </div>
        )}

        {/* Actions based on state */}
        {!status?.enabled && !showPasswordInput && !showDisableConfirm && (
          <div className="flex gap-2">
            <button
              onClick={() => { clearMessages(); setShowPasswordInput('enable') }}
              className="flex-1 rounded-lg bg-blue-600 py-2 text-sm font-medium text-white hover:bg-blue-500"
            >
              Enable Backup
            </button>
            <button
              onClick={() => { clearMessages(); setShowPasswordInput('restore') }}
              className="flex-1 rounded-lg border border-slate-600 py-2 text-sm font-medium text-slate-300 hover:bg-slate-700"
            >
              Restore from Backup
            </button>
          </div>
        )}

        {status?.enabled && !showPasswordInput && !showDisableConfirm && (
          <div className="flex gap-2">
            <button
              onClick={handleManualSync}
              disabled={actionLoading}
              className="flex-1 rounded-lg bg-blue-600 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
            >
              {actionLoading ? 'Syncing...' : 'Sync Now'}
            </button>
            <button
              onClick={() => { clearMessages(); setShowDisableConfirm(true) }}
              className="rounded-lg border border-red-700/50 bg-red-900/30 px-4 py-2 text-sm font-medium text-red-300 hover:bg-red-900/50"
            >
              Disable
            </button>
          </div>
        )}

        {/* Password input for enable/restore */}
        {showPasswordInput && (
          <div className="mt-3">
            <label className="mb-1.5 block text-xs font-medium text-slate-400">
              Master Password
            </label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Enter your master password"
              className="mb-3 w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2.5 text-sm text-slate-100 outline-none placeholder:text-slate-500 focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  showPasswordInput === 'enable' ? handleEnable() : handleRestore()
                }
              }}
            />
            <div className="flex gap-2">
              <button
                onClick={showPasswordInput === 'enable' ? handleEnable : handleRestore}
                disabled={actionLoading || !password.trim()}
                className="flex-1 rounded-lg bg-blue-600 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
              >
                {actionLoading
                  ? 'Working...'
                  : showPasswordInput === 'enable'
                    ? 'Enable & Sync'
                    : 'Restore'}
              </button>
              <button
                onClick={() => { setShowPasswordInput(null); setPassword(''); clearMessages() }}
                className="rounded-lg border border-slate-600 px-4 py-2 text-sm font-medium text-slate-400 hover:bg-slate-700"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {/* Disable confirmation */}
        {showDisableConfirm && (
          <div className="mt-3 rounded-lg border border-red-700/50 bg-red-900/20 p-3">
            <p className="mb-3 text-sm text-red-200">
              Are you sure you want to disable backup?
            </p>
            <label className="mb-3 flex items-center gap-2 text-xs text-slate-300">
              <input
                type="checkbox"
                checked={deleteRemote}
                onChange={(e) => setDeleteRemote(e.target.checked)}
                className="rounded border-slate-600"
              />
              Also delete backup data from the server
            </label>
            <div className="flex gap-2">
              <button
                onClick={handleDisable}
                disabled={actionLoading}
                className="flex-1 rounded-lg bg-red-600 py-2 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-50"
              >
                {actionLoading ? 'Disabling...' : 'Confirm Disable'}
              </button>
              <button
                onClick={() => { setShowDisableConfirm(false); setDeleteRemote(false); clearMessages() }}
                className="rounded-lg border border-slate-600 px-4 py-2 text-sm font-medium text-slate-400 hover:bg-slate-700"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {/* Error / Success messages */}
        {error && (
          <div className="mt-3 rounded-lg border border-red-700/50 bg-red-900/40 px-3 py-2 text-xs text-red-300">
            {error}
          </div>
        )}
        {success && (
          <div className="mt-3 rounded-lg border border-green-700/50 bg-green-900/40 px-3 py-2 text-xs text-green-300">
            {success}
          </div>
        )}
      </div>
    </div>
  )
}
