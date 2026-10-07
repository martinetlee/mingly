import type { SSEEvent } from '../../../api/client'

/**
 * A hand-driven SSE stream for mocking `stream()`: push events, end it, and
 * observe the AbortSignal the consumer passed. Aborting rejects the pending
 * read with an AbortError, like fetch does.
 */
export function controllableStream() {
  const queue: SSEEvent[] = []
  let ended = false
  let wake: (() => void) | null = null
  let signal: AbortSignal | undefined

  const notify = () => { wake?.(); wake = null }

  async function* gen(opts?: { signal?: AbortSignal }): AsyncGenerator<SSEEvent> {
    signal = opts?.signal
    signal?.addEventListener('abort', notify)
    while (true) {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
      if (queue.length) { yield queue.shift()!; continue }
      if (ended) return
      await new Promise<void>((r) => { wake = r })
    }
  }

  return {
    gen,
    push(event: string, data: Record<string, unknown> = {}) { queue.push({ event, data }); notify() },
    end() { ended = true; notify() },
    get signal() { return signal },
  }
}

export type ControllableStream = ReturnType<typeof controllableStream>
