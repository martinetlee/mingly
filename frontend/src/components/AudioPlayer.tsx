import { useState, useRef, useCallback, useEffect } from 'react'
import { Play, Pause, Download } from 'lucide-react'
import { authenticatedMediaUrl } from '../api/session'
import { downloadMedia } from '../api/client'

interface Props {
  chatTelegramId: number
  msgTelegramId: number
  /** Pre-existing media URL if already downloaded */
  mediaUrl: string | null
  /** 'voice' for voice messages, 'audio' for audio files */
  mediaType: 'voice' | 'audio'
  isOut: boolean
}

/** Number of bars in the waveform visualization */
const WAVEFORM_BARS = 32

/** Generate pseudo-random waveform heights from a seed (message ID) */
function generateWaveform(seed: number): number[] {
  const bars: number[] = []
  let hash = seed
  for (let i = 0; i < WAVEFORM_BARS; i++) {
    hash = ((hash * 1103515245 + 12345) & 0x7fffffff) >>> 0
    // Heights between 0.15 and 1.0
    bars.push(0.15 + (hash % 1000) / 1000 * 0.85)
  }
  return bars
}

export default function AudioPlayer({ chatTelegramId, msgTelegramId, mediaUrl, mediaType, isOut }: Props) {
  const [url, setUrl] = useState<string | null>(mediaUrl)
  const [downloading, setDownloading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [playing, setPlaying] = useState(false)
  const [progress, setProgress] = useState(0)
  const [duration, setDuration] = useState(0)
  const [currentTime, setCurrentTime] = useState(0)
  const audioRef = useRef<HTMLAudioElement>(null)
  const waveform = useRef(generateWaveform(msgTelegramId)).current

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
    const audio = audioRef.current
    if (!audio) return
    if (audio.paused) {
      audio.play()
    } else {
      audio.pause()
    }
  }, [])

  const handleTimeUpdate = useCallback(() => {
    const audio = audioRef.current
    if (!audio || !audio.duration) return
    setCurrentTime(audio.currentTime)
    setProgress((audio.currentTime / audio.duration) * 100)
  }, [])

  const handleLoadedMetadata = useCallback(() => {
    const audio = audioRef.current
    if (audio && isFinite(audio.duration)) {
      setDuration(audio.duration)
    }
  }, [])

  const handleEnded = useCallback(() => {
    setPlaying(false)
    setProgress(0)
    setCurrentTime(0)
  }, [])

  const handleWaveformClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const audio = audioRef.current
    if (!audio || !audio.duration) return
    const rect = e.currentTarget.getBoundingClientRect()
    const ratio = (e.clientX - rect.left) / rect.width
    audio.currentTime = ratio * audio.duration
  }, [])

  // Listen for duration changes (some browsers report duration asynchronously)
  useEffect(() => {
    const audio = audioRef.current
    if (!audio) return
    const onDuration = () => {
      if (isFinite(audio.duration)) setDuration(audio.duration)
    }
    audio.addEventListener('durationchange', onDuration)
    return () => audio.removeEventListener('durationchange', onDuration)
  }, [url])

  const formatTime = (seconds: number) => {
    const m = Math.floor(seconds / 60)
    const s = Math.floor(seconds % 60)
    return `${m}:${s.toString().padStart(2, '0')}`
  }

  const isVoice = mediaType === 'voice'
  const label = isVoice ? 'voice message' : 'audio'

  // Not yet downloaded
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
            <span className="text-sm italic">Downloading {label}...</span>
          </>
        ) : (
          <>
            <Download size={16} />
            <span className="text-sm italic">{error ?? `Download ${label}`}</span>
          </>
        )}
      </button>
    )
  }

  const playButtonColor = isOut
    ? 'bg-indigo-400/30 hover:bg-indigo-400/50 text-white'
    : 'bg-slate-500/30 hover:bg-slate-500/50 text-slate-100'

  const barActiveColor = isOut ? 'bg-white' : 'bg-purple-400'
  const barInactiveColor = isOut ? 'bg-indigo-300/40' : 'bg-slate-500/50'

  return (
    <div className="flex items-center gap-2 px-3 py-2" style={{ minWidth: 200, maxWidth: 280 }}>
      <audio
        ref={audioRef}
        src={authenticatedMediaUrl(url)}
        onTimeUpdate={handleTimeUpdate}
        onLoadedMetadata={handleLoadedMetadata}
        onEnded={handleEnded}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        preload="metadata"
      />

      {/* Play/Pause button */}
      <button
        onClick={togglePlayPause}
        className={`flex-shrink-0 flex items-center justify-center rounded-full w-8 h-8 transition-colors ${playButtonColor}`}
      >
        {playing ? <Pause size={14} /> : <Play size={14} className="ml-0.5" />}
      </button>

      {/* Waveform / progress area */}
      <div className="flex-1 min-w-0">
        <div
          className="flex items-end gap-[2px] h-6 cursor-pointer"
          onClick={handleWaveformClick}
        >
          {waveform.map((height, i) => {
            const barProgress = (i / WAVEFORM_BARS) * 100
            const isActive = barProgress < progress
            return (
              <div
                key={i}
                className={`flex-1 rounded-sm transition-colors duration-75 ${
                  isActive ? barActiveColor : barInactiveColor
                }`}
                style={{ height: `${height * 100}%`, minWidth: 2 }}
              />
            )
          })}
        </div>

        {/* Time display */}
        <div className={`flex justify-between mt-0.5 text-[10px] ${
          isOut ? 'text-indigo-200/70' : 'text-slate-400'
        }`}>
          <span>{formatTime(currentTime)}</span>
          {duration > 0 && <span>{formatTime(duration)}</span>}
        </div>
      </div>
    </div>
  )
}
