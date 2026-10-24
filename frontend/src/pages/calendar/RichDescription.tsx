/**
 * An event description as Google Calendar sends it: plain text, or limited
 * HTML (bold, italics, links, lists, line breaks). The HTML is parsed (not
 * executed) and only an allow-list of tags becomes React elements; links keep
 * just an http(s) / mailto href and open outside the app. Everything else
 * (scripts, styles, attributes, images…) is dropped — never innerHTML.
 */
import type { ReactNode } from 'react'
import { LinkifiedText } from '../../components/ui'

const LINK_CLASS = 'text-indigo-300 underline underline-offset-2 hover:text-indigo-200'
const DROP = new Set(['script', 'style', 'iframe', 'object', 'embed', 'template', 'head', 'title', 'noscript', 'svg', 'math'])
const KEEP: Record<string, string> = {
  b: 'font-semibold', strong: 'font-semibold', i: 'italic', em: 'italic', u: 'underline',
  p: 'mb-2', div: '', span: '', ul: 'mb-2 list-disc pl-5', ol: 'mb-2 list-decimal pl-5', li: '',
}
const SAFE_HREF = /^(https?:|mailto:)/i

function convert(node: Node, key: string): ReactNode {
  if (node.nodeType === Node.TEXT_NODE) {
    const text = node.textContent ?? ''
    return text ? <LinkifiedText key={key} text={text} linkClassName={LINK_CLASS} /> : null
  }
  if (node.nodeType !== Node.ELEMENT_NODE) return null
  const el = node as Element
  const tag = el.tagName.toLowerCase()
  if (DROP.has(tag)) return null
  if (tag === 'br') return <br key={key} />
  const children = Array.from(el.childNodes).map((c, i) => convert(c, `${key}.${i}`))
  if (tag === 'a') {
    const href = (el.getAttribute('href') ?? '').trim()
    if (!SAFE_HREF.test(href)) return <span key={key}>{children}</span>
    return <a key={key} href={href} target="_blank" rel="noopener noreferrer" className={LINK_CLASS}>{children}</a>
  }
  if (tag in KEEP) {
    const Tag = tag as 'b'
    return <Tag key={key} className={KEEP[tag] || undefined}>{children}</Tag>
  }
  // Unknown tags: keep the text inside
  return <span key={key}>{children}</span>
}

/** The description as safe React content */
export function renderDescription(text: string): ReactNode {
  if (!/<[a-z!/][^>]*>/i.test(text)) {
    return <LinkifiedText text={text} linkClassName={LINK_CLASS} />
  }
  const doc = new DOMParser().parseFromString(text, 'text/html')
  return Array.from(doc.body.childNodes).map((c, i) => convert(c, String(i)))
}

export default function RichDescription({ text }: { text: string }) {
  const html = /<[a-z!/][^>]*>/i.test(text)
  return (
    <div
      className={`text-xs leading-relaxed text-slate-300 [overflow-wrap:anywhere] ${html ? '' : 'whitespace-pre-wrap'}`}
      data-testid="event-description"
    >
      {renderDescription(text)}
    </div>
  )
}
