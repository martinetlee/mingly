import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import type { Workspace } from '../api/types'
import { getWorkspaces } from '../api/client'
import { useWsEvent } from '../hooks/useWebSocket'
import { useCoalescedRefetch } from '../hooks/useCoalescedRefetch'
import { reportError } from '../lib/toast'

const STORAGE_KEY = 'activeWorkspaceId'
const REFRESH_THROTTLE_MS = 15000

interface WorkspaceContextValue {
  workspaces: Workspace[]
  activeWorkspace: Workspace | null
  loaded: boolean
  setActiveWorkspaceId: (id: number | null) => void
  refreshWorkspaces: () => Promise<void>
}

const WorkspaceContext = createContext<WorkspaceContextValue>({
  workspaces: [],
  activeWorkspace: null,
  loaded: false,
  setActiveWorkspaceId: () => {},
  refreshWorkspaces: async () => {},
})

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [loaded, setLoaded] = useState(false)
  const [activeId, setActiveId] = useState<number | null>(() => {
    const stored = localStorage.getItem(STORAGE_KEY)
    return stored ? parseInt(stored, 10) : null
  })
  const snapshotRef = useRef('')
  // Ordering guard: only the most recently *started* refresh may apply, so a
  // slow WS-triggered read can't overwrite the list after a save
  const seqRef = useRef(0)
  const loadedRef = useRef(false)

  const refreshWorkspaces = useCallback(async () => {
    const seq = ++seqRef.current
    try {
      const data = await getWorkspaces()
      if (seq !== seqRef.current) return
      loadedRef.current = true
      setLoaded(true)
      // Skip identical payloads — churning object identities re-renders every
      // consumer and destabilizes their useCallback deps for no reason
      const snapshot = JSON.stringify(data)
      if (snapshot === snapshotRef.current) return
      snapshotRef.current = snapshot
      setWorkspaces(data)
    } catch (e) {
      // Background refreshes retry on the next event; only a failed first
      // load is worth telling the user about
      if (seq === seqRef.current && !loadedRef.current) reportError(e, 'Couldn\'t load workspaces')
    }
  }, [])

  useEffect(() => {
    refreshWorkspaces()
  }, [refreshWorkspaces])

  // Keep badge counts loosely fresh on conversation activity: at most one refresh per
  // window, with a trailing one so the last burst is never dropped
  const refreshSoon = useCoalescedRefetch(refreshWorkspaces, REFRESH_THROTTLE_MS)
  useWsEvent('new_message', refreshSoon)
  useWsEvent('conversations_synced', refreshSoon)
  useWsEvent('conversation_synced', refreshSoon)
  // Events may have been missed while disconnected
  useWsEvent('resync', () => { refreshWorkspaces() })

  const setActiveWorkspaceId = useCallback((id: number | null) => {
    setActiveId(id)
    if (id === null) localStorage.removeItem(STORAGE_KEY)
    else localStorage.setItem(STORAGE_KEY, String(id))
  }, [])

  const activeWorkspace = workspaces.find((w) => w.id === activeId) ?? null

  return (
    <WorkspaceContext.Provider value={{ workspaces, activeWorkspace, loaded, setActiveWorkspaceId, refreshWorkspaces }}>
      {children}
    </WorkspaceContext.Provider>
  )
}

// eslint-disable-next-line react-refresh/only-export-components
export function useWorkspaces() {
  return useContext(WorkspaceContext)
}
