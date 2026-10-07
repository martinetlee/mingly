import { useCallback } from 'react'
import { getShieldCloudSettings, setShieldCloudSettings } from '../../api/client'
import { useLatestQuery } from '../../hooks/useLatestQuery'
import { reportError } from '../../lib/toast'

/** Shield cloud settings shared by the settings panel and the Sync History tab. */
export function useCloudSettings() {
  const q = useLatestQuery('shield-cloud-settings', () => getShieldCloudSettings())
  const { data, setData, refetch } = q

  const setSyncDismissals = useCallback(async (value: boolean) => {
    if (!data) return
    setData({ ...data, sync_dismissals: value })
    try {
      await setShieldCloudSettings(data.cloud_url, '', value)
    } catch (e) {
      reportError(e, "Couldn't save the dismissal sync setting")
    } finally {
      void refetch()
    }
  }, [data, setData, refetch])

  return {
    cloudUrl: data?.cloud_url ?? '',
    keySet: data?.api_key_set ?? false,
    syncDismissals: data?.sync_dismissals ?? false,
    loaded: !!data,
    setSyncDismissals,
    refetch,
  }
}

export type CloudSettings = ReturnType<typeof useCloudSettings>
