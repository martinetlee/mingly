/**
 * ComposeBar focus: autoFocus=false keeps focus where it is even under
 * StrictMode (effects run twice); autoFocus focuses; a finished send
 * focuses the box again.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { StrictMode } from 'react'
import ComposeBar from '../ComposeBar'

function focusOutside() {
  const btn = document.createElement('button')
  btn.textContent = 'outside'
  document.body.appendChild(btn)
  btn.focus()
  return btn
}

afterEach(() => { cleanup(); document.body.innerHTML = '' })

describe('ComposeBar focus', () => {
  it('autoFocus=false keeps focus outside under StrictMode', () => {
    const outside = focusOutside()
    render(<StrictMode><ComposeBar onSend={async () => {}} autoFocus={false} /></StrictMode>)
    expect(document.activeElement).toBe(outside)
  })

  it('autoFocus focuses the text box under StrictMode', () => {
    focusOutside()
    render(<StrictMode><ComposeBar onSend={async () => {}} /></StrictMode>)
    expect(document.activeElement?.tagName).toBe('TEXTAREA')
  })

  it('focuses the text box after a send finishes', async () => {
    const outside = focusOutside()
    render(<StrictMode><ComposeBar onSend={async () => { outside.focus() }} autoFocus={false} /></StrictMode>)
    const box = screen.getByRole('textbox')
    fireEvent.change(box, { target: { value: 'hi' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(document.activeElement).toBe(box))
  })
})
