import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import Markdown, { linkCitations } from '../Markdown'

describe('Markdown', () => {
  it('renders formatting but never raw HTML or javascript: links', () => {
    const { container } = render(
      <Markdown content={'**Bold** <img src=x onerror="alert(1)"> <script>bad()</script> [x](javascript:alert(1))'} />,
    )
    expect(container.querySelector('strong')?.textContent).toBe('Bold')
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('script')).toBeNull()
    const a = container.querySelector('a')
    expect(a?.getAttribute('href') ?? '').not.toContain('javascript')
  })

  it('turns [n] citations into source buttons (1-based) and leaves out-of-range ones as text', () => {
    const onCite = vi.fn()
    render(<Markdown content={'- Launch moved [1][2]\n- Unknown [3]'} sources={[{ conversation_id: 100, message_id: 5 }, { conversation_id: 100, message_id: 6 }]} onCite={onCite} />)
    fireEvent.click(screen.getAllByTitle('Jump to source message')[1])
    expect(onCite).toHaveBeenCalledWith({ conversation_id: 100, message_id: 6 })
    expect(screen.getAllByTitle('Jump to source message')).toHaveLength(2)
    expect(screen.getByText('[3]')).toBeTruthy()
  })

  it('does not rewrite citations inside code or existing links', () => {
    expect(linkCitations('`a[1]` [x](http://e) [2]')).toBe('`a[1]` [x](http://e) [\\[2\\]](#cite-2)')
  })
})
