import { useState, useEffect, useCallback } from 'react'
import { getCloudOrgs } from '../api/client'

export interface CloudOrg {
  id: string
  name: string
  domain: string
  role: string
  member_count: number
  status: string
}

/**
 * Shared hook for fetching the user's cloud organizations.
 * Deduplicates the fetch logic used by OrgManagement, ShareToOrg, etc.
 */
export function useCloudOrgs() {
  const [orgs, setOrgs] = useState<CloudOrg[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const fetchOrgs = useCallback(async () => {
    setLoading(true)
    try {
      const data = await getCloudOrgs()
      setOrgs(Array.isArray(data) ? data : [])
      setError(null)
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to load organizations'
      setError(msg)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchOrgs()
  }, [fetchOrgs])

  return { orgs, loading, error, refetch: fetchOrgs }
}
