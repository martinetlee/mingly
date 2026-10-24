import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import RichDescription from '../RichDescription'

afterEach(cleanup)

const renderText = (text: string) => {
  render(<RichDescription text={text} />)
  return screen.getByTestId('event-description')
}

describe('RichDescription', () => {
  it('keeps plain text (line breaks, links)', () => {
    const el = renderText('Dial in:\nhttps://zoom.us/j/123')
    expect(el.className).toContain('whitespace-pre-wrap')
    expect(el.querySelector('a')?.getAttribute('href')).toBe('https://zoom.us/j/123')
  })

  it('keeps allowed formatting and drops scripts, handlers, styles and unsafe links', () => {
    const el = renderText(
      '<p onclick="steal()">Hi <b>team</b><br><img src=x onerror="steal()"></p>'
      + '<ul><li>one</li></ul><style>body{display:none}</style><script>steal()</script>'
      + '<a href="javascript:steal()">bad</a> <a href="mailto:a@b.c" style="color:red">mail</a>'
      + '<iframe src="https://evil.example"></iframe>',
    )
    expect(el.querySelector('b')?.textContent).toBe('team')
    expect(el.querySelector('li')?.textContent).toBe('one')
    expect(el.querySelector('br')).not.toBeNull()
    expect(el.querySelector('script, style, iframe, img')).toBeNull()
    expect(el.innerHTML).not.toContain('onclick')
    expect(el.innerHTML).not.toContain('onerror')
    expect(el.textContent).not.toContain('steal()')
    expect(el.textContent).not.toContain('display:none')
    const links = Array.from(el.querySelectorAll('a'))
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['mailto:a@b.c'])
    expect(links[0].getAttribute('style')).toBeNull()
    expect(links[0].getAttribute('target')).toBe('_blank')
  })
})
