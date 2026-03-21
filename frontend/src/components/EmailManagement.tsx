import { useState, useEffect, useCallback, useRef } from 'react'
import { getCloudProfile, addCloudEmail, pollEmailVerification, removeCloudEmail } from '../api/client'

interface VerifiedEmail {
  id: string
  email: string
  domain: string
  verified_at: string
}

interface Profile {
  id: string
  primary_email: string
  created_at: string
  emails: VerifiedEmail[]
}

export default function EmailManagement() {
  const [profile, setProfile] = useState<Profile | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Add email state
  const [showAddForm, setShowAddForm] = useState(false)
  const [newEmail, setNewEmail] = useState('')
  const [addingEmail, setAddingEmail] = useState(false)
  const [verifying, setVerifying] = useState(false)
  const [verifyRequestId, setVerifyRequestId] = useState<string | null>(null)
  const [verifyClaimSecret, setVerifyClaimSecret] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // Remove email state
  const [removeTarget, setRemoveTarget] = useState<VerifiedEmail | null>(null)
  const [removing, setRemoving] = useState(false)

  const fetchProfile = useCallback(async () => {
    try {
      const data = await getCloudProfile()
      setProfile(data)
      setError(null)
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to load profile'
      setError(msg)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchProfile()
  }, [fetchProfile])

  // Clean up polling on unmount
  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
  }, [])

  const handleAddEmail = useCallback(async () => {
    if (!newEmail.trim()) return
    setAddingEmail(true)
    setError(null)
    try {
      const result = await addCloudEmail(newEmail.trim())
      setVerifyRequestId(result.request_id)
      setVerifyClaimSecret(result.claim_secret)
      setVerifying(true)
      setAddingEmail(false)

      // Start polling for verification
      const poll = setInterval(async () => {
        try {
          const status = await pollEmailVerification(result.request_id, result.claim_secret)
          if (status.status === 'completed') {
            clearInterval(poll)
            pollRef.current = null
            setVerifying(false)
            setShowAddForm(false)
            setNewEmail('')
            setVerifyRequestId(null)
            setVerifyClaimSecret(null)
            await fetchProfile()
          } else if (status.status === 'expired') {
            clearInterval(poll)
            pollRef.current = null
            setVerifying(false)
            setError('Verification link expired. Please try again.')
          }
        } catch {
          // Keep polling on transient errors
        }
      }, 3000)
      pollRef.current = poll

      // Stop polling after 15 minutes
      setTimeout(() => {
        if (pollRef.current === poll) {
          clearInterval(poll)
          pollRef.current = null
          setVerifying(false)
          setError('Verification timed out. Please try again.')
        }
      }, 15 * 60 * 1000)
    } catch (err: unknown) {
      setAddingEmail(false)
      const msg = err instanceof Error ? err.message : 'Failed to add email'
      setError(msg)
    }
  }, [newEmail, fetchProfile])

  const handleCancelVerification = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
    setVerifying(false)
    setVerifyRequestId(null)
    setVerifyClaimSecret(null)
    setShowAddForm(false)
    setNewEmail('')
  }, [])

  const handleRemoveEmail = useCallback(async (confirm: boolean) => {
    if (!removeTarget) return
    setRemoving(true)
    setError(null)
    try {
      await removeCloudEmail(removeTarget.id, confirm)
      setRemoveTarget(null)
      await fetchProfile()
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to remove email'
      setError(msg)
    } finally {
      setRemoving(false)
    }
  }, [removeTarget, fetchProfile])

  if (loading) {
    return (
      <section className="mb-6">
        <h3 className="mb-2 text-sm font-medium text-slate-300">Verified Emails</h3>
        <p className="text-xs text-slate-500">Loading...</p>
      </section>
    )
  }

  if (!profile) {
    return (
      <section className="mb-6">
        <h3 className="mb-2 text-sm font-medium text-slate-300">Verified Emails</h3>
        {error && (
          <div className="rounded-lg bg-red-900/40 border border-red-700/50 px-3 py-2 text-xs text-red-300">
            {error}
          </div>
        )}
      </section>
    )
  }

  return (
    <section className="mb-6">
      <h3 className="mb-2 text-sm font-medium text-slate-300">Verified Emails</h3>
      <p className="mb-3 text-xs text-slate-400">
        Add business email addresses to join or create organizations.
      </p>

      {/* Email list */}
      <div className="space-y-2 mb-3">
        {profile.emails.map((em) => {
          const isPrimary = em.email === profile.primary_email
          return (
            <div
              key={em.id}
              className="flex items-center justify-between rounded-lg border border-slate-700 bg-slate-800/50 px-3 py-2.5"
            >
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="truncate text-sm text-slate-200">{em.email}</span>
                  {isPrimary && (
                    <span className="shrink-0 rounded bg-blue-900/50 border border-blue-700/50 px-1.5 py-0.5 text-[10px] font-medium text-blue-300">
                      PRIMARY
                    </span>
                  )}
                </div>
                <div className="mt-0.5 flex items-center gap-2 text-[11px] text-slate-500">
                  <span>{em.domain}</span>
                  <span>Verified {new Date(em.verified_at).toLocaleDateString()}</span>
                </div>
              </div>
              {!isPrimary && (
                <button
                  onClick={() => setRemoveTarget(em)}
                  className="shrink-0 rounded px-2 py-1 text-xs text-red-400 hover:bg-red-900/30 hover:text-red-300"
                >
                  Remove
                </button>
              )}
            </div>
          )
        })}
        {profile.emails.length === 0 && (
          <p className="text-xs text-slate-500 italic">No verified emails yet.</p>
        )}
      </div>

      {/* Add email form / verification */}
      {verifying ? (
        <div className="rounded-lg border border-slate-700 bg-slate-800 p-4 text-center">
          <div className="mx-auto mb-2 h-6 w-6 animate-spin rounded-full border-2 border-blue-400 border-t-transparent" />
          <p className="mb-1 text-sm font-medium text-slate-200">Check your email</p>
          <p className="mb-3 text-xs text-slate-400">
            We sent a verification link to <span className="text-slate-300">{newEmail}</span>. Click it to verify.
          </p>
          <button
            onClick={handleCancelVerification}
            className="rounded-lg border border-slate-600 px-3 py-1.5 text-xs text-slate-400 hover:bg-slate-700 hover:text-slate-200"
          >
            Cancel
          </button>
        </div>
      ) : showAddForm ? (
        <div className="rounded-lg border border-slate-700 bg-slate-800 p-3">
          <label className="mb-1 block text-xs font-medium text-slate-400">
            Business email address
          </label>
          <div className="flex gap-2">
            <input
              type="email"
              value={newEmail}
              onChange={(e) => setNewEmail(e.target.value)}
              placeholder="you@company.com"
              className="flex-1 rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-sm text-slate-100 outline-none placeholder:text-slate-500 focus:border-blue-500"
              onKeyDown={(e) => { if (e.key === 'Enter') handleAddEmail() }}
              disabled={addingEmail}
            />
            <button
              onClick={handleAddEmail}
              disabled={!newEmail.trim() || addingEmail}
              className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {addingEmail ? 'Sending...' : 'Verify'}
            </button>
            <button
              onClick={() => { setShowAddForm(false); setNewEmail('') }}
              className="rounded-lg border border-slate-600 px-3 py-2 text-sm text-slate-400 hover:bg-slate-700"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <button
          onClick={() => setShowAddForm(true)}
          className="rounded-lg border border-dashed border-slate-600 px-4 py-2 text-xs font-medium text-slate-400 hover:border-slate-500 hover:text-slate-300"
        >
          + Add Business Email
        </button>
      )}

      {/* Error banner */}
      {error && (
        <div className="mt-3 rounded-lg bg-red-900/40 border border-red-700/50 px-3 py-2 text-xs text-red-300">
          {error}
        </div>
      )}

      {/* Remove confirmation dialog */}
      {removeTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <div className="w-full max-w-sm rounded-xl border border-slate-700 bg-slate-800 p-5 shadow-xl">
            <h4 className="mb-2 text-sm font-medium text-slate-200">Remove Email</h4>
            <p className="mb-1 text-xs text-slate-400">
              Are you sure you want to remove <span className="text-slate-300">{removeTarget.email}</span>?
            </p>
            <div className="mb-4 rounded-lg bg-amber-900/30 border border-amber-700/40 px-3 py-2 text-xs text-amber-300">
              Removing this email will revoke your membership in any organizations linked to the <span className="font-medium">{removeTarget.domain}</span> domain.
            </div>
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setRemoveTarget(null)}
                disabled={removing}
                className="rounded-lg border border-slate-600 px-4 py-2 text-xs font-medium text-slate-400 hover:bg-slate-700"
              >
                Cancel
              </button>
              <button
                onClick={() => handleRemoveEmail(true)}
                disabled={removing}
                className="rounded-lg bg-red-600 px-4 py-2 text-xs font-medium text-white hover:bg-red-500 disabled:opacity-50"
              >
                {removing ? 'Removing...' : 'Remove & Revoke'}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
