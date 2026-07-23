import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import type { Workspace, WSEvent } from '../api/types'
import { getWorkspaces } from '../api/client'
import { useWebSocket } from '../hooks/useWebSocket'

const STORAGE_KEY = 'activeWorkspaceId'
const REFRESH_THROTTLE_MS = 15000

interface WorkspaceContextValue {
  workspaces: Workspace[]
  activeWorkspace: Workspace | null
  setActiveWorkspaceId: (id: number | null) => void
  refreshWorkspaces: () => Promise<void>
}

const WorkspaceContext = createContext<WorkspaceContextValue>({
  workspaces: [],
  activeWorkspace: null,
  setActiveWorkspaceId: () => {},
  refreshWorkspaces: async () => {},
})

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [activeId, setActiveId] = useState<number | null>(() => {
    const stored = localStorage.getItem(STORAGE_KEY)
    return stored ? parseInt(stored, 10) : null
  })
  const lastRefreshRef = useRef(0)
  const snapshotRef = useRef('')

  const refreshWorkspaces = useCallback(async () => {
    try {
      const data = await getWorkspaces()
      lastRefreshRef.current = Date.now()
      // Skip identical payloads — churning object identities re-renders every
      // consumer and destabilizes their useCallback deps for no reason
      const snapshot = JSON.stringify(data)
      if (snapshot === snapshotRef.current) return
      snapshotRef.current = snapshot
      setWorkspaces(data)
    } catch {
      // Session may not be ready yet; the next trigger retries
    }
  }, [])

  useEffect(() => {
    refreshWorkspaces()
  }, [refreshWorkspaces])

  // Keep badge counts loosely fresh on chat activity
  const handleWsEvent = useCallback(
    (event: WSEvent) => {
      if (event.type !== 'new_message' && event.type !== 'chat_synced') return
      if (Date.now() - lastRefreshRef.current < REFRESH_THROTTLE_MS) return
      refreshWorkspaces()
    },
    [refreshWorkspaces],
  )
  useWebSocket(handleWsEvent)

  const setActiveWorkspaceId = useCallback((id: number | null) => {
    setActiveId(id)
    if (id === null) localStorage.removeItem(STORAGE_KEY)
    else localStorage.setItem(STORAGE_KEY, String(id))
  }, [])

  const activeWorkspace = workspaces.find((w) => w.id === activeId) ?? null

  return (
    <WorkspaceContext.Provider value={{ workspaces, activeWorkspace, setActiveWorkspaceId, refreshWorkspaces }}>
      {children}
    </WorkspaceContext.Provider>
  )
}

// eslint-disable-next-line react-refresh/only-export-components
export function useWorkspaces() {
  return useContext(WorkspaceContext)
}
