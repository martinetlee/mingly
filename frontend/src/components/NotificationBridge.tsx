import { useCallback, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { getPrefs } from '../api/client'
import { useWsEvent } from '../hooks/useWebSocket'
import { notifyNewMessage, setCachedNotifPref } from '../lib/notifications'

/**
 * The single place desktop notifications are raised from (one per incoming
 * message, regardless of which page is mounted). Renders nothing.
 */
export default function NotificationBridge() {
  const navigate = useNavigate()

  const loadPref = useCallback(() => {
    getPrefs()
      .then((p) => setCachedNotifPref(p.notif_pref))
      .catch(() => { /* keep the cached/default pref; Settings surfaces pref errors */ })
  }, [])

  useEffect(loadPref, [loadPref])
  useWsEvent('resync', loadPref)

  useWsEvent('new_message', (event) => {
    notifyNewMessage(event, (chatId) => navigate(`/chats?chatId=${chatId}`))
  })

  return null
}
