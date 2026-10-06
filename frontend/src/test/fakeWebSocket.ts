/** Minimal controllable WebSocket stand-in for tests. */
export class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  static reset() { FakeWebSocket.instances = [] }
  static live() { return FakeWebSocket.instances.filter((s) => s.readyState === 0 || s.readyState === 1) }

  readyState = 0
  url: string
  onopen: ((e: Event) => void) | null = null
  onmessage: ((e: MessageEvent) => void) | null = null
  onclose: ((e: CloseEvent) => void) | null = null
  onerror: ((e: Event) => void) | null = null

  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }

  /** Server accepts the handshake */
  open() {
    this.readyState = 1
    this.onopen?.(new Event('open'))
  }

  /** Server pushes a JSON event */
  receive(data: unknown) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }

  /** Connection drops / server closes with `code` */
  drop(code = 1006) {
    this.readyState = 3
    this.onclose?.(new CloseEvent('close', { code }))
  }

  close() {
    if (this.readyState === 3) return
    this.readyState = 3
    // Real sockets fire onclose asynchronously after close()
    const cb = this.onclose
    queueMicrotask(() => cb?.(new CloseEvent('close')))
  }

  send() {}
}
