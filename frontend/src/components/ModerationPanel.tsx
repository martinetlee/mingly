import { useState, useEffect, useCallback } from 'react'
import { Pin, Clock, BarChart3, Eye, ArrowRight } from 'lucide-react'
import type { Conversation, Message, ChannelPostStats } from '../api/types'
import { getPinnedMessages, pinMessage, setSlowMode, getPostStats } from '../api/client'
import { reportError } from '../lib/toast'
import { can } from '../lib/capabilities'
import { useWsEvent } from '../hooks/useWebSocket'
import { fmtShortDate } from '../utils/time'

const SLOW_MODE_OPTIONS = [
  { value: 0, label: 'Off' },
  { value: 10, label: '10s' },
  { value: 30, label: '30s' },
  { value: 60, label: '1m' },
  { value: 300, label: '5m' },
  { value: 900, label: '15m' },
  { value: 3600, label: '1h' },
]

/**
 * Mount with `key={chat.id}`: all state (pinned list, stats) belongs
 * to one conversation, so switching can never unpin in the wrong one.
 * Rendered only for conversations with the 'admin_moderation' capability;
 * the pinned list additionally needs 'pin'.
 */
interface ModerationPanelProps {
  chat: Conversation
  onChatUpdate?: (updates: Partial<Conversation>) => void
}

export default function ModerationPanel({ chat, onChatUpdate }: ModerationPanelProps) {
  const [pinnedMessages, setPinnedMessages] = useState<Message[]>([])
  const [loadingPinned, setLoadingPinned] = useState(false)
  const [postStats, setPostStats] = useState<ChannelPostStats[]>([])
  const [loadingStats, setLoadingStats] = useState(false)
  const [slowModeValue, setSlowModeValue] = useState(chat.slow_mode_seconds ?? 0)
  const [settingSlowMode, setSettingSlowMode] = useState(false)
  const [showPinned, setShowPinned] = useState(false)
  const [showStats, setShowStats] = useState(false)

  const loadPinnedMessages = useCallback(async () => {
    setLoadingPinned(true)
    try {
      const data = await getPinnedMessages(chat.id)
      setPinnedMessages(data)
    } catch (e) {
      setPinnedMessages([])
      reportError(e, 'Couldn’t load pinned messages')
    } finally {
      setLoadingPinned(false)
    }
  }, [chat.id])

  const loadPostStats = useCallback(async () => {
    setLoadingStats(true)
    try {
      const data = await getPostStats(chat.id)
      setPostStats(data)
    } catch (e) {
      setPostStats([])
      reportError(e, 'Couldn’t load post stats')
    } finally {
      setLoadingStats(false)
    }
  }, [chat.id])

  useEffect(() => {
    setSlowModeValue(chat.slow_mode_seconds ?? 0)
  }, [chat.slow_mode_seconds])

  const handleSlowMode = async (seconds: number) => {
    setSettingSlowMode(true)
    try {
      await setSlowMode(chat.id, seconds)
      setSlowModeValue(seconds)
      onChatUpdate?.({ slow_mode_seconds: seconds })
    } catch (e) {
      reportError(e, 'Slow mode change failed')
    } finally {
      setSettingSlowMode(false)
    }
  }

  const handleUnpin = async (msg: Message) => {
    // A pinned row always belongs to the chat it was loaded for
    try {
      await pinMessage(msg.id, false)
      setPinnedMessages((prev) => prev.filter((m) => m.id !== msg.id))
    } catch (e) {
      reportError(e, 'Unpin failed')
    }
  }

  // Pins changed from any device: refresh the open list
  useWsEvent('messages_pinned', (e) => {
    if (e.conversation_id === chat.id && showPinned) void loadPinnedMessages()
  })


  return (
    <div className="px-4 py-4 border-t border-slate-700/50 flex flex-col gap-3">
      <div className="flex items-center gap-1.5 text-xs font-medium text-slate-400 uppercase tracking-wider">
        Moderation
      </div>

      {/* Pinned Messages */}
      {can(chat, 'pin') && (
      <div>
        <button
          onClick={() => { setShowPinned((v) => !v); if (!showPinned && pinnedMessages.length === 0) loadPinnedMessages() }}
          className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200 transition-colors w-full"
        >
          <Pin size={11} />
          Pinned Messages
          <ArrowRight size={10} className={`ml-auto transition-transform ${showPinned ? 'rotate-90' : ''}`} />
        </button>
        {showPinned && (
          <div className="mt-2 flex flex-col gap-1 max-h-48 overflow-y-auto">
            {loadingPinned ? (
              <p className="text-[11px] text-slate-500">Loading...</p>
            ) : pinnedMessages.length === 0 ? (
              <p className="text-[11px] text-slate-500">No pinned messages</p>
            ) : (
              pinnedMessages.map((msg) => (
                <div key={msg.id} className="group flex items-start gap-2 rounded px-2 py-1.5 bg-slate-700/30">
                  <div className="flex-1 min-w-0">
                    <p className="text-[10px] text-slate-400 font-medium">{msg.sender?.name || 'Unknown'}</p>
                    <p className="text-[11px] text-slate-300 truncate">{msg.text || '[media]'}</p>
                  </div>
                  <button
                    onClick={() => handleUnpin(msg)}
                    title="Unpin"
                    className="flex-shrink-0 rounded p-0.5 text-slate-500 opacity-0 group-hover:opacity-100 hover:text-rose-400 transition-all"
                  >
                    <Pin size={10} />
                  </button>
                </div>
              ))
            )}
          </div>
        )}
      </div>
      )}

      {/* Slow Mode (groups only) */}
      {chat.kind === 'group' && (
        <div>
          <div className="flex items-center gap-1.5 text-xs text-slate-400 mb-1.5">
            <Clock size={11} />
            Slow Mode
          </div>
          <div className="flex flex-wrap gap-1">
            {SLOW_MODE_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                onClick={() => handleSlowMode(opt.value)}
                disabled={settingSlowMode}
                className={`rounded px-2 py-0.5 text-[10px] font-medium transition-colors ${
                  slowModeValue === opt.value
                    ? 'bg-indigo-500/20 text-indigo-400 border border-indigo-500/30'
                    : 'bg-slate-700/50 text-slate-400 border border-slate-600 hover:bg-slate-700 hover:text-slate-200'
                } disabled:opacity-50`}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Channel Post Stats */}
      {chat.kind === 'channel' && (
        <div>
          <button
            onClick={() => { setShowStats((v) => !v); if (!showStats && postStats.length === 0) loadPostStats() }}
            className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200 transition-colors w-full"
          >
            <BarChart3 size={11} />
            Post Stats
            <ArrowRight size={10} className={`ml-auto transition-transform ${showStats ? 'rotate-90' : ''}`} />
          </button>
          {showStats && (
            <div className="mt-2 flex flex-col gap-1 max-h-60 overflow-y-auto">
              {loadingStats ? (
                <p className="text-[11px] text-slate-500">Loading...</p>
              ) : postStats.length === 0 ? (
                <p className="text-[11px] text-slate-500">No post stats available</p>
              ) : (
                postStats.map((post) => (
                  <div key={post.external_id} className="rounded px-2 py-1.5 bg-slate-700/30">
                    <p className="text-[11px] text-slate-300 truncate mb-1">{post.text || '[media]'}</p>
                    <div className="flex items-center gap-3 text-[10px] text-slate-500">
                      <span className="flex items-center gap-0.5">
                        <Eye size={9} />
                        {post.views.toLocaleString()}
                      </span>
                      <span className="flex items-center gap-0.5">
                        <ArrowRight size={9} />
                        {post.forwards}
                      </span>
                      <span className="flex items-center gap-0.5">
                        <BarChart3 size={9} />
                        {post.replies_count} replies
                      </span>
                      {post.date && (
                        <span className="ml-auto">{fmtShortDate(post.date)}</span>
                      )}
                    </div>
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
