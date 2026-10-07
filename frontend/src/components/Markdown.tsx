import { memo, useMemo } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { BriefingSource } from '../api/types'

/**
 * Safe markdown renderer for LLM output (Intelligence chat replies, briefing
 * and catch-up summaries). Raw HTML is never rendered — react-markdown
 * escapes it unless rehype-raw is added, which we deliberately don't — and
 * link URLs go through react-markdown's default urlTransform, which drops
 * `javascript:`/`data:` and other unsafe protocols.
 *
 * Briefing summaries cite their sources inline as `[n]` (1-based index into
 * `sources`). Pass `sources` + `onCite` to turn those into clickable badges.
 */

const CITE_HREF = '#cite-'
// [n] not already part of a markdown link ([n](…) or [text][n])
const CITE_RE = /\[(\d{1,3})\](?!\()/g

/** Rewrite `[n]` citations into links our `a` renderer recognises (outside code). */
export function linkCitations(text: string): string {
  // Leave fenced/inline code untouched
  return text
    .split(/(```[\s\S]*?```|`[^`\n]*`)/g)
    .map((part, i) => (i % 2 === 1 ? part : part.replace(CITE_RE, (_m, n: string) => `[\\[${n}\\]](${CITE_HREF}${n})`)))
    .join('')
}

type Size = 'sm' | 'xs'

const SIZE_TEXT: Record<Size, string> = { sm: 'text-sm', xs: 'text-[11px]' }

function buildComponents(
  size: Size,
  sources: BriefingSource[] | undefined,
  onCite: ((src: BriefingSource) => void) | undefined,
): Components {
  const small = size === 'xs'
  return {
    p: ({ children }) => <p className={`${small ? 'mb-1' : 'mb-2'} last:mb-0 leading-relaxed`}>{children}</p>,
    h1: ({ children }) => <h1 className={`${small ? 'mb-1 mt-2 text-xs' : 'mb-2 mt-3 text-base'} font-semibold text-slate-100`}>{children}</h1>,
    h2: ({ children }) => <h2 className={`${small ? 'mb-1 mt-2 text-xs' : 'mb-1.5 mt-3 text-sm'} font-semibold text-slate-200`}>{children}</h2>,
    h3: ({ children }) => <h3 className={`${small ? 'mb-0.5 mt-1.5 text-[11px]' : 'mb-1 mt-2 text-sm'} font-medium text-slate-300`}>{children}</h3>,
    ul: ({ children }) => <ul className={`${small ? 'mb-1' : 'mb-2'} ml-4 list-disc space-y-0.5 last:mb-0`}>{children}</ul>,
    ol: ({ children }) => <ol className={`${small ? 'mb-1' : 'mb-2'} ml-4 list-decimal space-y-0.5 last:mb-0`}>{children}</ol>,
    li: ({ children }) => <li className="leading-relaxed">{children}</li>,
    code: ({ children, className }) => {
      const isBlock = Boolean(className?.startsWith('language-') || String(children).includes('\n'))
      return isBlock ? (
        <code className="block overflow-x-auto rounded-lg bg-slate-800 p-3 font-mono text-xs text-slate-300 leading-relaxed">
          {children}
        </code>
      ) : (
        <code className="rounded bg-slate-600/60 px-1 py-0.5 font-mono text-xs text-indigo-300">{children}</code>
      )
    },
    pre: ({ children }) => <pre className="mb-2 last:mb-0">{children}</pre>,
    blockquote: ({ children }) => (
      <blockquote className="mb-2 border-l-2 border-indigo-500/50 pl-3 text-slate-400 italic last:mb-0">{children}</blockquote>
    ),
    a: ({ children, href }) => {
      if (href?.startsWith(CITE_HREF)) {
        const n = parseInt(href.slice(CITE_HREF.length), 10)
        const src = sources?.[n - 1]
        if (src && onCite) {
          return (
            <button
              type="button"
              onClick={() => onCite(src)}
              className="mx-0.5 inline-flex items-center rounded border border-indigo-500/25 bg-indigo-500/15 px-1 py-0 align-baseline text-[10px] font-medium leading-none text-indigo-400 transition-colors hover:bg-indigo-500/30"
              title="Jump to source message"
            >
              [{n}]
            </button>
          )
        }
        // Citation without a matching source — plain text
        return <span className="text-slate-500">[{n}]</span>
      }
      return (
        <a href={href} target="_blank" rel="noopener noreferrer" className="text-indigo-400 underline underline-offset-2 hover:text-indigo-300">
          {children}
        </a>
      )
    },
    strong: ({ children }) => <strong className="font-semibold text-slate-100">{children}</strong>,
    em: ({ children }) => <em className="italic text-slate-300">{children}</em>,
    hr: () => <hr className="my-3 border-slate-600/50" />,
    table: ({ children }) => (
      <div className="mb-2 overflow-x-auto last:mb-0">
        <table className="w-full border-collapse text-xs">{children}</table>
      </div>
    ),
    th: ({ children }) => (
      <th className="border border-slate-600 bg-slate-700/60 px-2 py-1.5 text-left font-medium text-slate-200">{children}</th>
    ),
    td: ({ children }) => <td className="border border-slate-700 px-2 py-1.5 text-slate-300">{children}</td>,
    img: () => null, // never load remote images from LLM output
  }
}

interface MarkdownProps {
  content: string
  size?: Size
  className?: string
  /** Briefing citations: `[n]` → sources[n-1] */
  sources?: BriefingSource[]
  onCite?: (src: BriefingSource) => void
}

function Markdown({ content, size = 'sm', className, sources, onCite }: MarkdownProps) {
  const components = useMemo(() => buildComponents(size, sources, onCite), [size, sources, onCite])
  const text = useMemo(() => (sources !== undefined || onCite ? linkCitations(content) : content), [content, sources, onCite])
  return (
    <div className={`${SIZE_TEXT[size]} ${className ?? ''}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  )
}

export default memo(Markdown)
