import { useState, useRef, useEffect } from 'react'
import { Play, Pause, Download, RotateCcw } from 'lucide-react'
import { authenticatedMediaUrl } from '../api/session'
import { useMediaDownload } from '../hooks/useMediaDownload'
import { useMediaPlayback } from '../hooks/useMediaPlayback'
import { formatDuration } from '../utils/time'

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
  // Click-to-load (shared download pipeline: dedupe, bounded retries, abort on unmount)
  const media = useMediaDownload(chatTelegramId, msgTelegramId, { initialUrl: mediaUrl })
  const [wantPlay, setWantPlay] = useState(false)
  const p = useMediaPlayback<HTMLAudioElement>()
  const waveform = useRef(generateWaveform(msgTelegramId)).current

  // Play once a click-to-load download completes
  useEffect(() => {
    if (wantPlay && media.url && p.ref.current) {
      setWantPlay(false)
      p.toggle()
    }
  }, [wantPlay, media.url, p.ref, p.toggle])

  const label = mediaType === 'voice' ? 'voice message' : 'audio'

  if (!media.url) {
    const loading = media.status === 'loading' || media.status === 'queued'
    const failed = media.status === 'error'
    return (
      <button
        onClick={() => { setWantPlay(true); media.start() }}
        disabled={loading}
        className={`flex items-center gap-2 px-3 py-2 ${
          isOut ? 'text-indigo-200 hover:text-white' : 'text-slate-300 hover:text-white'
        } transition-colors`}
      >
        {loading ? (
          <>
            <div className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
            <span className="text-sm italic">Downloading {label}…</span>
          </>
        ) : failed ? (
          <>
            <RotateCcw size={16} />
            <span className="text-sm italic">{media.error ?? 'Download failed'} — retry</span>
          </>
        ) : (
          <>
            <Download size={16} />
            <span className="text-sm italic">Play {label}</span>
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
      <audio {...p.bind} src={authenticatedMediaUrl(media.url)} preload="metadata" />

      <button
        onClick={p.toggle}
        className={`flex-shrink-0 flex items-center justify-center rounded-full w-8 h-8 transition-colors ${playButtonColor}`}
        aria-label={p.playing ? 'Pause' : 'Play'}
      >
        {p.playing ? <Pause size={14} /> : <Play size={14} className="ml-0.5" />}
      </button>

      <div className="flex-1 min-w-0">
        <div className="flex items-end gap-[2px] h-6 cursor-pointer" onClick={p.seekFromClick}>
          {waveform.map((height, i) => (
            <div
              key={i}
              className={`flex-1 rounded-sm transition-colors duration-75 ${
                (i / WAVEFORM_BARS) * 100 < p.progress ? barActiveColor : barInactiveColor
              }`}
              style={{ height: `${height * 100}%`, minWidth: 2 }}
            />
          ))}
        </div>
        <div className={`flex justify-between mt-0.5 text-[10px] ${isOut ? 'text-indigo-200/70' : 'text-slate-400'}`}>
          <span>{formatDuration(p.currentTime) || '0:00'}</span>
          {p.duration > 0 && <span>{formatDuration(p.duration)}</span>}
        </div>
      </div>
    </div>
  )
}
