import { useState, useEffect } from 'react'
import { Play, Pause, Maximize, RotateCcw, Film } from 'lucide-react'
import { authenticatedMediaUrl } from '../api/session'
import { useMediaDownload } from '../hooks/useMediaDownload'
import { useMediaPlayback } from '../hooks/useMediaPlayback'
import { formatDuration } from '../utils/time'
import { LightboxShell } from './ImageLightbox'

interface Props {
  chatTelegramId: number
  msgTelegramId: number
  /** Pre-existing media URL if already downloaded */
  mediaUrl: string | null
  /** Whether this is a round video note */
  isVideoNote?: boolean
  isOut: boolean
  /** Album cell: fill the cell, no inline controls */
  compact?: boolean
}

function SeekBar({ progress, onSeek, thick }: { progress: number; onSeek: (e: React.MouseEvent<Element>) => void; thick?: boolean }) {
  const h = thick ? 'h-1.5' : 'h-1'
  return (
    <div className={`flex-1 ${h} bg-white/30 rounded-full cursor-pointer`} onClick={onSeek}>
      <div className={`${h} bg-white rounded-full transition-[width] duration-100`} style={{ width: `${progress}%` }} />
    </div>
  )
}

function VideoLightbox({ src, onClose }: { src: string; onClose: () => void }) {
  const c = useMediaPlayback<HTMLVideoElement>()
  return (
    <LightboxShell onClose={onClose} onKey={(e) => { if (e.key === ' ') { c.toggle(); return true } return false }}>
      <div className="relative flex flex-col items-center" style={{ maxWidth: '90vw', maxHeight: '90vh' }}>
        <video
          {...c.bind}
          src={src}
          className="max-h-[85vh] max-w-[90vw] rounded-lg cursor-pointer"
          onClick={c.toggle}
          autoPlay
          playsInline
        />
        <div className="w-full mt-2 px-2 flex items-center gap-3">
          <button onClick={c.toggle} className="text-white hover:text-slate-200" aria-label={c.playing ? 'Pause' : 'Play'}>
            {c.playing ? <Pause size={18} /> : <Play size={18} />}
          </button>
          <SeekBar progress={c.progress} onSeek={c.seekFromClick} thick />
          {c.duration > 0 && <span className="text-xs text-white/80">{formatDuration(c.duration)}</span>}
        </div>
      </div>
    </LightboxShell>
  )
}

export default function VideoPlayer({ chatTelegramId, msgTelegramId, mediaUrl, isVideoNote, isOut, compact }: Props) {
  // Click-to-load: videos are large, never auto-download every one in view
  const media = useMediaDownload(chatTelegramId, msgTelegramId, { initialUrl: mediaUrl })
  const [wantPlay, setWantPlay] = useState(false)
  const [lightboxOpen, setLightboxOpen] = useState(false)
  const c = useMediaPlayback<HTMLVideoElement>()
  const size = isVideoNote ? 240 : 320

  // Start playback once a click-to-load download finishes
  useEffect(() => {
    if (!wantPlay || !media.url) return
    setWantPlay(false)
    if (compact) setLightboxOpen(true)
    else void c.ref.current?.play().catch(() => { /* autoplay can be refused; the play button still works */ })
  }, [wantPlay, media.url, compact, c.ref])

  if (!media.url) {
    const loading = media.status === 'loading' || media.status === 'queued'
    const failed = media.status === 'error'
    return (
      <button
        onClick={() => { setWantPlay(true); media.start() }}
        disabled={loading}
        className={`relative flex flex-col items-center justify-center gap-2 bg-black/30 transition-colors hover:bg-black/40 ${
          isVideoNote ? 'rounded-full' : compact ? 'h-full w-full' : 'rounded-md'
        } ${isOut ? 'text-indigo-100' : 'text-slate-200'}`}
        style={compact ? { minHeight: 80 } : isVideoNote ? { width: size, height: size } : { width: 260, height: 160 }}
        title={failed ? media.error ?? 'Download failed' : 'Load video'}
      >
        {loading ? (
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-current border-t-transparent" />
        ) : failed ? (
          <RotateCcw size={22} />
        ) : (
          <div className="rounded-full bg-black/50 p-3"><Play size={22} className="ml-0.5 text-white" /></div>
        )}
        {!compact && (
          <span className="flex items-center gap-1 text-xs">
            <Film size={12} />
            {loading ? 'Loading…' : failed ? `${media.error ?? 'Failed'} — retry` : isVideoNote ? 'Video message' : 'Video'}
          </span>
        )}
      </button>
    )
  }

  const authedUrl = authenticatedMediaUrl(media.url)

  if (compact) {
    return (
      <>
        <button className="relative block h-full w-full bg-black/40" onClick={() => setLightboxOpen(true)} aria-label="Play video">
          <video src={authedUrl} className="block h-full w-full object-cover" style={{ minHeight: 80 }} preload="metadata" muted playsInline />
          <span className="absolute inset-0 flex items-center justify-center">
            <span className="rounded-full bg-black/50 p-2"><Play size={18} className="ml-0.5 text-white" /></span>
          </span>
        </button>
        {lightboxOpen && <VideoLightbox src={authedUrl} onClose={() => setLightboxOpen(false)} />}
      </>
    )
  }

  const videoClasses = isVideoNote
    ? 'rounded-full object-cover cursor-pointer'
    : `block cursor-pointer ${isOut ? 'ml-auto' : 'mr-auto'}`

  return (
    <>
      <div className="relative group/video" style={{ maxWidth: size }}>
        <video
          {...c.bind}
          src={authedUrl}
          className={videoClasses}
          style={isVideoNote ? { width: size, height: size } : { maxWidth: size, maxHeight: size }}
          onClick={c.toggle}
          playsInline
          preload="metadata"
        />
        {!c.playing && (
          <div
            className="absolute inset-0 flex items-center justify-center cursor-pointer"
            onClick={c.toggle}
            style={isVideoNote ? { borderRadius: '50%' } : undefined}
          >
            <div className="rounded-full bg-black/50 p-3">
              <Play size={24} className="text-white ml-0.5" />
            </div>
          </div>
        )}
        {!isVideoNote && (
          <div className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-black/60 to-transparent p-2 opacity-0 group-hover/video:opacity-100 transition-opacity">
            <div className="flex items-center gap-2">
              <button onClick={c.toggle} className="text-white hover:text-slate-200" aria-label={c.playing ? 'Pause' : 'Play'}>
                {c.playing ? <Pause size={14} /> : <Play size={14} />}
              </button>
              <SeekBar progress={c.progress} onSeek={c.seekFromClick} />
              {c.duration > 0 && <span className="text-[10px] text-white/80">{formatDuration(c.duration)}</span>}
              <button
                onClick={() => { c.ref.current?.pause(); setLightboxOpen(true) }}
                className="text-white hover:text-slate-200"
                aria-label="Full screen"
              >
                <Maximize size={12} />
              </button>
            </div>
          </div>
        )}
      </div>
      {lightboxOpen && <VideoLightbox src={authedUrl} onClose={() => setLightboxOpen(false)} />}
    </>
  )
}
