import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import StatusBanner, { LinkifiedText } from '../StatusBanner'

afterEach(cleanup)

describe('StatusBanner', () => {
  it('wraps long text and makes its URLs external links (trailing punctuation left out)', () => {
    const url = 'https://console.developers.google.com/apis/api/calendar-json.googleapis.com/overview?project=1'
    render(<StatusBanner testId="b">Error: enable it at {url}. <LinkifiedText text={`Enable it by visiting ${url}.`} /></StatusBanner>)
    const banner = screen.getByTestId('b')
    expect(banner.getAttribute('role')).toBe('status')
    const link = screen.getByRole('link')
    expect(link.getAttribute('href')).toBe(url)
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.getAttribute('rel')).toContain('noopener')
    expect(banner.textContent).toContain(`${url}.`)
    expect(banner.innerHTML).toContain('overflow-wrap:anywhere')
  })

  it('leaves text without links alone', () => {
    render(<p data-testid="p"><LinkifiedText text="Sync is stopped." /></p>)
    expect(screen.getByTestId('p').textContent).toBe('Sync is stopped.')
    expect(screen.queryByRole('link')).toBeNull()
  })
})
