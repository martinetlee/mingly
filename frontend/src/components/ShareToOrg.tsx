import { useState, useCallback } from 'react'
import {
  shareContactToOrg,
  listDirectory,
  createSharedOrg,
  createContactLink,
  type SharedContact,
  type SharedOrg,
} from '../api/client'
import { useCloudOrgs } from '../hooks/useCloudOrgs'

interface ShareToOrgProps {
  /** Local contact telegram_id */
  telegramId: number
  /** Display name of the local contact */
  contactName: string
  /** Telegram handle (without @) */
  telegramHandle?: string
  /** Called after successful share (receives the org_id) */
  onShared?: (orgId: string) => void
}

type Step = 'idle' | 'select-org' | 'sharing' | 'link-org' | 'creating-link' | 'success' | 'duplicate' | 'error'

const RELATIONSHIP_OPTIONS = [
  { value: '', label: 'None' },
  { value: 'client', label: 'Client' },
  { value: 'vendor', label: 'Vendor' },
  { value: 'partner', label: 'Partner' },
  { value: 'prospect', label: 'Prospect' },
] as const

export default function ShareToOrg({
  telegramId,
  contactName,
  telegramHandle,
  onShared,
}: ShareToOrgProps) {
  // State
  const [step, setStep] = useState<Step>('idle')
  const { orgs } = useCloudOrgs()
  const [selectedOrgId, setSelectedOrgId] = useState<string | null>(null)
  const [sharedContact, setSharedContact] = useState<SharedContact | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [sharedOrgName, setSharedOrgName] = useState('')

  // Link-org step state
  const [directory, setDirectory] = useState<SharedOrg[]>([])
  const [selectedSharedOrgId, setSelectedSharedOrgId] = useState<string | null>(null)
  const [newOrgName, setNewOrgName] = useState('')
  const [position, setPosition] = useState('')
  const [relationshipContext, setRelationshipContext] = useState('')
  const [loadingDirectory, setLoadingDirectory] = useState(false)

  // Duplicate state
  const [existingContact, setExistingContact] = useState<SharedContact | null>(null)

  // Filter to active orgs
  const activeOrgs = orgs.filter((o) => o.status === 'active')

  const doShare = useCallback(async (orgId: string) => {
    setStep('sharing')
    setError(null)
    try {
      const data: { telegram_id: number; name: string; telegram_handle?: string } = {
        telegram_id: telegramId,
        name: contactName,
      }
      if (telegramHandle) {
        data.telegram_handle = telegramHandle
      }
      const contact = await shareContactToOrg(orgId, data)
      setSharedContact(contact)
      setSharedOrgName(activeOrgs.find((o) => o.id === orgId)?.name || orgId)
      setStep('success')
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to share contact'
      // Check for duplicate (409)
      if (msg.includes('409') || msg.toLowerCase().includes('already exists') || msg.toLowerCase().includes('duplicate') || msg.toLowerCase().includes('conflict')) {
        setExistingContact(null)
        setSharedOrgName(activeOrgs.find((o) => o.id === orgId)?.name || orgId)
        setStep('duplicate')
      } else {
        setError(msg)
        setStep('error')
      }
    }
  }, [telegramId, contactName, telegramHandle, activeOrgs])

  const handleStartShare = useCallback(() => {
    setError(null)
    if (activeOrgs.length === 0) {
      setError('You are not a member of any cloud org.')
      setStep('error')
      return
    }
    if (activeOrgs.length === 1) {
      // Skip org selection — go straight to sharing
      setSelectedOrgId(activeOrgs[0].id)
      doShare(activeOrgs[0].id)
    } else {
      setStep('select-org')
    }
  }, [activeOrgs, doShare])

  const handleOrgSelected = useCallback(() => {
    if (!selectedOrgId) return
    doShare(selectedOrgId)
  }, [selectedOrgId, doShare])

  const handleLinkToOrg = useCallback(async () => {
    if (!selectedOrgId || !sharedContact) return
    setLoadingDirectory(true)
    try {
      const dir = await listDirectory(selectedOrgId)
      setDirectory(Array.isArray(dir) ? dir : [])
    } catch {
      setDirectory([])
    } finally {
      setLoadingDirectory(false)
    }
    setStep('link-org')
  }, [selectedOrgId, sharedContact])

  const handleCreateLink = useCallback(async () => {
    if (!selectedOrgId || !sharedContact) return

    setStep('creating-link')
    setError(null)

    try {
      let targetSharedOrgId = selectedSharedOrgId

      // Create new shared org if needed
      if (!targetSharedOrgId && newOrgName.trim()) {
        const newOrg = await createSharedOrg(selectedOrgId, { name: newOrgName.trim() })
        targetSharedOrgId = newOrg.id
      }

      if (!targetSharedOrgId) {
        setError('Select or create an external organization.')
        setStep('link-org')
        return
      }

      const linkData: { shared_org_id: string; position?: string; relationship_context?: string } = {
        shared_org_id: targetSharedOrgId,
      }
      if (position.trim()) linkData.position = position.trim()
      if (relationshipContext) linkData.relationship_context = relationshipContext

      await createContactLink(selectedOrgId, sharedContact.id, linkData)
      setStep('success')
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to create link'
      setError(msg)
      setStep('link-org')
    }
  }, [selectedOrgId, sharedContact, selectedSharedOrgId, newOrgName, position, relationshipContext])

  const handleDone = useCallback(() => {
    if (selectedOrgId) {
      onShared?.(selectedOrgId)
    }
    setStep('idle')
    // Reset state
    setSharedContact(null)
    setExistingContact(null)
    setError(null)
    setNewOrgName('')
    setPosition('')
    setRelationshipContext('')
    setSelectedSharedOrgId(null)
  }, [selectedOrgId, onShared])

  const handleReset = useCallback(() => {
    setStep('idle')
    setError(null)
    setSharedContact(null)
    setExistingContact(null)
  }, [])

  // Don't render if not connected to any cloud org
  if (activeOrgs.length === 0 && step === 'idle') return null

  return (
    <div className="inline-block">
      {/* Idle: show the share button */}
      {step === 'idle' && (
        <button
          onClick={handleStartShare}
          className="flex items-center gap-1.5 rounded-lg border border-blue-700/50 bg-blue-900/30 px-3 py-1.5 text-xs font-medium text-blue-300 hover:bg-blue-900/50 transition-colors"
        >
          <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
          </svg>
          Share to Org
        </button>
      )}

      {/* Org selector */}
      {step === 'select-org' && (
        <div className="rounded-xl border border-slate-700 bg-slate-800 p-4 min-w-[280px]">
          <h3 className="mb-3 text-sm font-medium text-slate-200">Select Organization</h3>
          <div className="space-y-2 mb-3">
            {activeOrgs.map((org) => (
              <label
                key={org.id}
                className={`flex items-center gap-2 rounded-lg border p-2.5 cursor-pointer transition-colors ${
                  selectedOrgId === org.id
                    ? 'border-blue-500 bg-blue-900/20'
                    : 'border-slate-600 hover:border-slate-500'
                }`}
              >
                <input
                  type="radio"
                  name="org"
                  value={org.id}
                  checked={selectedOrgId === org.id}
                  onChange={() => setSelectedOrgId(org.id)}
                  className="sr-only"
                />
                <div>
                  <p className="text-sm text-slate-200">{org.name}</p>
                  <p className="text-xs text-slate-400">{org.domain}</p>
                </div>
              </label>
            ))}
          </div>
          <div className="flex gap-2">
            <button
              onClick={handleReset}
              className="flex-1 rounded-lg border border-slate-600 py-2 text-xs font-medium text-slate-400 hover:bg-slate-700"
            >
              Cancel
            </button>
            <button
              onClick={handleOrgSelected}
              disabled={!selectedOrgId}
              className="flex-1 rounded-lg bg-blue-600 py-2 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
            >
              Share
            </button>
          </div>
        </div>
      )}

      {/* Sharing spinner */}
      {step === 'sharing' && (
        <div className="rounded-xl border border-slate-700 bg-slate-800 p-4 min-w-[280px] text-center">
          <div className="mx-auto mb-2 h-6 w-6 animate-spin rounded-full border-2 border-blue-400 border-t-transparent" />
          <p className="text-xs text-slate-400">Sharing contact...</p>
        </div>
      )}

      {/* Duplicate handling */}
      {step === 'duplicate' && (
        <div className="rounded-xl border border-amber-700/50 bg-slate-800 p-4 min-w-[280px]">
          <div className="mb-2 rounded-lg bg-amber-900/40 border border-amber-700/50 px-3 py-2 text-xs text-amber-300">
            This contact already exists in {sharedOrgName}.
          </div>
          {existingContact && (
            <p className="mb-3 text-xs text-slate-400">
              Shared as: {existingContact.name}
            </p>
          )}
          <div className="flex gap-2">
            <button
              onClick={handleReset}
              className="flex-1 rounded-lg border border-slate-600 py-2 text-xs font-medium text-slate-400 hover:bg-slate-700"
            >
              Close
            </button>
          </div>
        </div>
      )}

      {/* Success */}
      {step === 'success' && (
        <div className="rounded-xl border border-green-700/50 bg-slate-800 p-4 min-w-[280px]">
          <div className="mb-3 rounded-lg bg-green-900/40 border border-green-700/50 px-3 py-2 text-xs text-green-300">
            Contact shared to {sharedOrgName}
          </div>
          <div className="flex gap-2">
            <button
              onClick={handleLinkToOrg}
              className="flex-1 rounded-lg border border-blue-700/50 bg-blue-900/30 py-2 text-xs font-medium text-blue-300 hover:bg-blue-900/50"
            >
              Link to Ext. Org
            </button>
            <button
              onClick={handleDone}
              className="flex-1 rounded-lg bg-green-700 py-2 text-xs font-medium text-white hover:bg-green-600"
            >
              Done
            </button>
          </div>
        </div>
      )}

      {/* Link to external org */}
      {step === 'link-org' && (
        <div className="rounded-xl border border-slate-700 bg-slate-800 p-4 min-w-[300px]">
          <h3 className="mb-3 text-sm font-medium text-slate-200">Link to External Organization</h3>

          {loadingDirectory ? (
            <div className="mb-3 text-center">
              <div className="mx-auto h-5 w-5 animate-spin rounded-full border-2 border-blue-400 border-t-transparent" />
            </div>
          ) : (
            <>
              {directory.length > 0 && (
                <div className="mb-3">
                  <label className="mb-1 block text-xs font-medium text-slate-400">
                    Existing organization
                  </label>
                  <select
                    value={selectedSharedOrgId || ''}
                    onChange={(e) => {
                      setSelectedSharedOrgId(e.target.value || null)
                      if (e.target.value) setNewOrgName('')
                    }}
                    className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-sm text-slate-100 outline-none focus:border-blue-500"
                  >
                    <option value="">-- Select --</option>
                    {directory.map((org) => (
                      <option key={org.id} value={org.id}>
                        {org.name}{org.domain ? ` (${org.domain})` : ''}
                      </option>
                    ))}
                  </select>
                </div>
              )}

              <div className="mb-3">
                <label className="mb-1 block text-xs font-medium text-slate-400">
                  {directory.length > 0 ? 'Or create new' : 'Organization name'}
                </label>
                <input
                  type="text"
                  value={newOrgName}
                  onChange={(e) => {
                    setNewOrgName(e.target.value)
                    if (e.target.value) setSelectedSharedOrgId(null)
                  }}
                  placeholder="e.g. Acme Corp"
                  disabled={!!selectedSharedOrgId}
                  className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-sm text-slate-100 outline-none placeholder:text-slate-500 focus:border-blue-500 disabled:opacity-50"
                />
              </div>
            </>
          )}

          <div className="mb-3">
            <label className="mb-1 block text-xs font-medium text-slate-400">
              Position / Title
            </label>
            <input
              type="text"
              value={position}
              onChange={(e) => setPosition(e.target.value)}
              placeholder="e.g. VP Sales"
              className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-sm text-slate-100 outline-none placeholder:text-slate-500 focus:border-blue-500"
            />
          </div>

          <div className="mb-3">
            <label className="mb-1 block text-xs font-medium text-slate-400">
              Relationship
            </label>
            <select
              value={relationshipContext}
              onChange={(e) => setRelationshipContext(e.target.value)}
              className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-sm text-slate-100 outline-none focus:border-blue-500"
            >
              {RELATIONSHIP_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>

          {error && (
            <div className="mb-3 rounded-lg bg-red-900/40 border border-red-700/50 px-3 py-2 text-xs text-red-300">
              {error}
            </div>
          )}

          <div className="flex gap-2">
            <button
              onClick={handleDone}
              className="flex-1 rounded-lg border border-slate-600 py-2 text-xs font-medium text-slate-400 hover:bg-slate-700"
            >
              Skip
            </button>
            <button
              onClick={handleCreateLink}
              disabled={!selectedSharedOrgId && !newOrgName.trim()}
              className="flex-1 rounded-lg bg-blue-600 py-2 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
            >
              Link
            </button>
          </div>
        </div>
      )}

      {/* Creating link spinner */}
      {step === 'creating-link' && (
        <div className="rounded-xl border border-slate-700 bg-slate-800 p-4 min-w-[280px] text-center">
          <div className="mx-auto mb-2 h-6 w-6 animate-spin rounded-full border-2 border-blue-400 border-t-transparent" />
          <p className="text-xs text-slate-400">Creating link...</p>
        </div>
      )}

      {/* Error */}
      {step === 'error' && (
        <div className="rounded-xl border border-red-700/50 bg-slate-800 p-4 min-w-[280px]">
          <div className="mb-3 rounded-lg bg-red-900/40 border border-red-700/50 px-3 py-2 text-xs text-red-300">
            {error || 'An error occurred.'}
          </div>
          <button
            onClick={handleReset}
            className="w-full rounded-lg border border-slate-600 py-2 text-xs font-medium text-slate-400 hover:bg-slate-700"
          >
            Close
          </button>
        </div>
      )}
    </div>
  )
}
