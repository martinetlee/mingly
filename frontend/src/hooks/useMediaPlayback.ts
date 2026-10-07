import { useCallback, useRef, useState } from 'react'

/** Ratio (0..1) of a click along an element's width. */
export function clickRatio(e: { clientX: number; currentTarget: Element }): number {
  const rect = e.currentTarget.getBoundingClientRect()
  if (rect.width <= 0) return 0
  return Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width))
}

/**
 * Shared state for an <audio>/<video> element: play/pause, progress, current
 * time, duration and seeking. Spread `bind` onto the element.
 */
export function useMediaPlayback<T extends HTMLMediaElement>() {
  const ref = useRef<T>(null)
  const [playing, setPlaying] = useState(false)
  const [progress, setProgress] = useState(0) // percent
  const [currentTime, setCurrentTime] = useState(0)
  const [duration, setDuration] = useState(0)

  const toggle = useCallback(() => {
    const el = ref.current
    if (!el) return
    if (el.paused) void el.play().catch(() => { /* autoplay can be refused; the play button still works */ })
    else el.pause()
  }, [])

  const seek = useCallback((ratio: number) => {
    const el = ref.current
    if (el && el.duration && isFinite(el.duration)) el.currentTime = Math.min(1, Math.max(0, ratio)) * el.duration
  }, [])

  /** onClick handler for a seek bar / waveform */
  const seekFromClick = useCallback((e: React.MouseEvent<Element>) => seek(clickRatio(e)), [seek])

  const updateDuration = useCallback(() => {
    const el = ref.current
    if (el && isFinite(el.duration)) setDuration(el.duration)
  }, [])

  const bind = {
    ref,
    onTimeUpdate: () => {
      const el = ref.current
      if (!el || !el.duration) return
      setCurrentTime(el.currentTime)
      setProgress((el.currentTime / el.duration) * 100)
    },
    onLoadedMetadata: updateDuration,
    onDurationChange: updateDuration,
    onPlay: () => setPlaying(true),
    onPause: () => setPlaying(false),
    onEnded: () => { setPlaying(false); setProgress(0); setCurrentTime(0) },
  }

  return { ref, playing, progress, currentTime, duration, toggle, seek, seekFromClick, bind }
}
