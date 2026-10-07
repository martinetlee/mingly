import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useState } from 'react'
import { render, screen, fireEvent, act, cleanup, waitFor } from '@testing-library/react'
import Modal from '../Modal'
import { ConfirmProvider, useConfirm } from '../ConfirmDialog'
import type { ConfirmFn } from '../ConfirmDialog'
import { ToastProvider } from '../Toast'
import { toast, resetToasts, reportError } from '../../../lib/toast'
import { useKeyboardShortcuts } from '../../../hooks/useKeyboardShortcuts'
import { ApiError } from '../../../lib/http'

beforeEach(() => resetToasts())
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('Modal', () => {
  it('closes on Escape and backdrop, configurable', () => {
    const onClose = vi.fn()
    const { rerender } = render(<Modal open onClose={onClose} title="T"><button>inside</button></Modal>)
    fireEvent.keyDown(window, { key: 'Escape' })
    fireEvent.click(screen.getByTestId('modal-backdrop'))
    expect(onClose).toHaveBeenCalledTimes(2)

    rerender(<Modal open onClose={onClose} title="T" closeOnEscape={false} closeOnBackdrop={false}><button>inside</button></Modal>)
    fireEvent.keyDown(window, { key: 'Escape' })
    fireEvent.click(screen.getByTestId('modal-backdrop'))
    expect(onClose).toHaveBeenCalledTimes(2)
  })

  it('suspends page shortcuts while open and restores them after', () => {
    const next = vi.fn()
    function Page() {
      const [open, setOpen] = useState(true)
      useKeyboardShortcuts({ j: next })
      return (
        <>
          <Modal open={open} onClose={() => setOpen(false)} title="Delete?"><button>ok</button></Modal>
          <button onClick={() => setOpen(false)} data-testid="close">close</button>
        </>
      )
    }
    render(<Page />)
    fireEvent.keyDown(document.body, { key: 'j' })
    expect(next).not.toHaveBeenCalled()
    fireEvent.keyDown(window, { key: 'Escape' })
    fireEvent.keyDown(document.body, { key: 'j' })
    expect(next).toHaveBeenCalledTimes(1)
  })

  it('only the top-most modal handles Escape', () => {
    const outer = vi.fn()
    const inner = vi.fn()
    render(
      <>
        <Modal open onClose={outer} title="Outer" />
        <Modal open onClose={inner} title="Inner" />
      </>,
    )
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(inner).toHaveBeenCalledTimes(1)
    expect(outer).not.toHaveBeenCalled()
  })

  it('traps focus and restores it on close', () => {
    const trigger = document.createElement('button')
    document.body.appendChild(trigger)
    trigger.focus()
    const { rerender } = render(
      <Modal open onClose={() => {}} ariaLabel="x">
        <button>first</button>
        <button>last</button>
      </Modal>,
    )
    expect(document.activeElement?.textContent).toBe('first')
    const last = screen.getByText('last')
    last.focus()
    fireEvent.keyDown(window, { key: 'Tab' })
    expect(document.activeElement?.textContent).toBe('first')
    fireEvent.keyDown(window, { key: 'Tab', shiftKey: true })
    expect(document.activeElement?.textContent).toBe('last')
    rerender(<Modal open={false} onClose={() => {}} ariaLabel="x" />)
    expect(document.activeElement).toBe(trigger)
    trigger.remove()
  })
})

describe('useConfirm', () => {
  let confirm: ConfirmFn
  function Grab() {
    confirm = useConfirm()
    return null
  }
  const renderConfirm = () => render(<ConfirmProvider><Grab /></ConfirmProvider>)

  it('resolves true/false and focuses Cancel for destructive confirms', async () => {
    renderConfirm()
    let p!: Promise<boolean>
    act(() => { p = confirm({ title: 'Delete org?', message: 'Removes 4 positions', destructive: true }) })
    expect(screen.getByText('Removes 4 positions')).toBeTruthy()
    expect(document.activeElement?.textContent).toBe('Cancel')
    fireEvent.click(screen.getByText('Delete'))
    await expect(p).resolves.toBe(true)

    act(() => { p = confirm({ title: 'Again?' }) })
    fireEvent.keyDown(window, { key: 'Escape' })
    await expect(p).resolves.toBe(false)
  })

  it('checkbox state resets on every open ("Delete for everyone" stays unticked)', async () => {
    renderConfirm()
    const opts = { title: 'Delete message?', destructive: true, checkbox: { label: 'Delete for everyone' } } as const
    let p!: Promise<{ confirmed: boolean; checked: boolean }>
    act(() => { p = confirm(opts) })
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByText('Delete'))
    await expect(p).resolves.toEqual({ confirmed: true, checked: true })

    act(() => { p = confirm(opts) })
    expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(false)
    fireEvent.click(screen.getByText('Delete'))
    await expect(p).resolves.toEqual({ confirmed: true, checked: false })
  })

  it('runs onConfirm single-flight, stays open on error and shows it inline', async () => {
    renderConfirm()
    let attempts = 0
    let release!: () => void
    const onConfirm = vi.fn(() => {
      attempts++
      if (attempts === 1) return Promise.reject(new Error('Telegram said no'))
      return new Promise<void>((r) => { release = r })
    })
    let p!: Promise<boolean>
    act(() => { p = confirm({ title: 'Forward?', confirmLabel: 'Forward', onConfirm }) })
    await act(async () => { fireEvent.click(screen.getByText('Forward')) })
    expect(screen.getByRole('alert').textContent).toBe('Telegram said no')

    await act(async () => {
      fireEvent.click(screen.getByText('Forward'))
      fireEvent.click(screen.getByText('Forward')) // double click while pending
    })
    expect(onConfirm).toHaveBeenCalledTimes(2)
    await act(async () => { release() })
    await expect(p).resolves.toBe(true)
    expect(screen.queryByText('Forward?')).toBeNull()
  })
})

describe('Toast', () => {
  it('renders, auto-dismisses and dedupes bursts', async () => {
    vi.useFakeTimers()
    render(<ToastProvider />)
    act(() => {
      toast.success('Saved')
      toast.error('Boom')
      toast.error('Boom')
    })
    expect(screen.getAllByText('Boom')).toHaveLength(1)
    expect(screen.getByText('Saved')).toBeTruthy()
    act(() => { vi.advanceTimersByTime(3000) })
    expect(screen.queryByText('Saved')).toBeNull()
    expect(screen.getByText('Boom')).toBeTruthy()
    act(() => { vi.advanceTimersByTime(3000) })
    expect(screen.queryByText('Boom')).toBeNull()
  })

  it('reportError toasts with context but ignores aborts and session expiry', async () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<ToastProvider />)
    act(() => {
      reportError(new DOMException('x', 'AbortError'), 'Load')
      reportError(new ApiError('Session expired', { status: 401, code: 'session_expired', path: '/api/x' }), 'Load')
      reportError(new Error('disk full'), 'Export failed')
    })
    await waitFor(() => expect(screen.getByText('Export failed: disk full')).toBeTruthy())
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    spy.mockRestore()
  })
})
