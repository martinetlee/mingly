import { useState, useRef, useCallback } from 'react'
import { Play, Pause, Maximize, Download } from 'lucide-react'
import { authenticatedMediaUrl } from '../api/session'
import { downloadMedia } from '../api/client'

interface Props {
  chatTelegramId: number
  msgTelegramId: number
  /** Pre-existing media URL if already downloaded */
  mediaUrl: string | null
  /** Whether this is a round video note */
  isVideoNote?: boolean
  isOut: boolean
}

export default function VideoPlayer({ chatTelegramId, msgTelegramId, mediaUrl, isVideoNote, isOut }: Props) {
  const [url, setUrl] = useState<string | null>(mediaUrl)
  const [downloading, setDownloading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [playing, setPlaying] = useState(false)
  const [progress, setProgress] = useState(0)
  const [duration, setDuration] = useState(0)
  const videoRef = useRef<HTMLVideoElement>(null)

  const handleDownload = useCallback(async () => {
    if (downloading) return
    setDownloading(true)
    setError(null)
    try {
      const result = await downloadMedia(chatTelegramId, msgTelegramId)
      setUrl(result.media_url)
    } catch (e) {
      setError('Failed to download')
    } finally {
      setDownloading(false)
    }
  }, [chatTelegramId, msgTelegramId, downloading])

  const togglePlayPause = useCallback(() => {
    const video = videoRef.current
    if (!video) return
    if (video.paused) {
      video.play()
      setPlaying(true)
    } else {
      video.pause()
      setPlaying(false)
    }
  }, [])

  const handleTimeUpdate = useCallback(() => {
    const video = videoRef.current
    if (!video || !video.duration) return
    setProgress((video.currentTime / video.duration) * 100)
  }, [])

  const handleLoadedMetadata = useCallback(() => {
    const video = videoRef.current
    if (video) setDuration(video.duration)
  }, [])

  const handleSeek = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const video = videoRef.current
    if (!video || !video.duration) return
    const rect = e.currentTarget.getBoundingClientRect()
    const ratio = (e.clientX - rect.left) / rect.width
    video.currentTime = ratio * video.duration
  }, [])

  const handleFullscreen = useCallback(() => {
    const video = videoRef.current
    if (video) video.requestFullscreen?.()
  }, [])

  const handleEnded = useCallback(() => {
    setPlaying(false)
    setProgress(0)
  }, [])

  const formatDuration = (seconds: number) => {
    const m = Math.floor(seconds / 60)
    const s = Math.floor(seconds % 60)
    return `${m}:${s.toString().padStart(2, '0')}`
  }

  const size = isVideoNote ? 240 : 320

  // Not yet downloaded — show download prompt
  if (!url) {
    return (
      <button
        onClick={handleDownload}
        disabled={downloading}
        className={`flex items-center gap-2 px-3 py-2 ${
          isOut ? 'text-indigo-200 hover:text-white' : 'text-slate-300 hover:text-white'
        } transition-colors`}
      >
        {downloading ? (
          <>
            <div className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
            <span className="text-sm italic">Downloading{isVideoNote ? ' video note' : ' video'}...</span>
          </>
        ) : (
          <>
            <Download size={16} />
            <span className="text-sm italic">
              {error ?? (isVideoNote ? 'Download video note' : 'Download video')}
            </span>
          </>
        )}
      </button>
    )
  }

  const videoClasses = isVideoNote
    ? 'rounded-full object-cover cursor-pointer'
    : `block cursor-pointer ${isOut ? 'ml-auto' : 'mr-auto'}`

  return (
    <div className="relative group/video" style={{ maxWidth: size }}>
      <video
        ref={videoRef}
        src={authenticatedMediaUrl(url)}
        className={videoClasses}
        style={isVideoNote ? { width: size, height: size } : { maxWidth: size, maxHeight: size }}
        onClick={togglePlayPause}
        onTimeUpdate={handleTimeUpdate}
        onLoadedMetadata={handleLoadedMetadata}
        onEnded={handleEnded}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        playsInline
        preload="metadata"
      />

      {/* Overlay play button when paused */}
      {!playing && (
        <div
          className="absolute inset-0 flex items-center justify-center cursor-pointer"
          onClick={togglePlayPause}
          style={isVideoNote ? { borderRadius: '50%' } : undefined}
        >
          <div className="rounded-full bg-black/50 p-3">
            <Play size={24} className="text-white ml-0.5" />
          </div>
        </div>
      )}

      {/* Controls bar */}
      {!isVideoNote && (
        <div className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-black/60 to-transparent p-2 opacity-0 group-hover/video:opacity-100 transition-opacity">
          <div className="flex items-center gap-2">
            <button onClick={togglePlayPause} className="text-white hover:text-slate-200">
              {playing ? <Pause size={14} /> : <Play size={14} />}
            </button>
            <div
              className="flex-1 h-1 bg-white/30 rounded-full cursor-pointer"
              onClick={handleSeek}
            >
              <div
                className="h-1 bg-white rounded-full transition-[width] duration-100"
                style={{ width: `${progress}%` }}
              />
            </div>
            {duration > 0 && (
              <span className="text-[10px] text-white/80">{formatDuration(duration)}</span>
            )}
            <button onClick={handleFullscreen} className="text-white hover:text-slate-200">
              <Maximize size={12} />
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
