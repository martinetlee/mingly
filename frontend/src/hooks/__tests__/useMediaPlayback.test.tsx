import { describe, it, expect } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { clickRatio, useMediaPlayback } from '../useMediaPlayback'

const target = (left: number, width: number) => ({ getBoundingClientRect: () => ({ left, width }) }) as unknown as Element

describe('useMediaPlayback', () => {
  it('clickRatio is clamped to 0..1 and safe for zero width', () => {
    expect(clickRatio({ clientX: 150, currentTarget: target(100, 200) })).toBe(0.25)
    expect(clickRatio({ clientX: 50, currentTarget: target(100, 200) })).toBe(0)
    expect(clickRatio({ clientX: 900, currentTarget: target(100, 200) })).toBe(1)
    expect(clickRatio({ clientX: 10, currentTarget: target(0, 0) })).toBe(0)
  })

  it('seek sets currentTime from a ratio of the duration; tracks progress', () => {
    const { result } = renderHook(() => useMediaPlayback<HTMLAudioElement>())
    const el = { duration: 80, currentTime: 0 } as HTMLAudioElement
    ;(result.current.ref as { current: HTMLAudioElement | null }).current = el
    act(() => result.current.seek(0.5))
    expect(el.currentTime).toBe(40)
    act(() => result.current.bind.onTimeUpdate())
    expect(result.current.progress).toBe(50)
    expect(result.current.currentTime).toBe(40)
  })
})
