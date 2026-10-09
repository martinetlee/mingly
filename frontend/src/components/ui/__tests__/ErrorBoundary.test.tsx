import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import ErrorBoundary from '../ErrorBoundary'

let broken = true
function Flaky() {
  if (broken) throw new Error('boom')
  return <p>page content</p>
}

describe('ErrorBoundary', () => {
  it('shows a recoverable message instead of blanking, and retries', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<ErrorBoundary><Flaky /></ErrorBoundary>)
    expect(screen.getByRole('alert').textContent).toContain('boom')
    broken = false
    fireEvent.click(screen.getByRole('button', { name: /try again/i }))
    expect(screen.getByText('page content')).toBeTruthy()
  })
})
