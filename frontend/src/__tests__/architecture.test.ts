/**
 * Architecture fitness test: the folder layering of src/ is enforced here, so
 * a misplaced import fails CI instead of slowly eroding the structure.
 *
 *   pages/            screens; a screen folder (pages/<name>/) is private to its screen
 *   components/       reusable UI, grouped by domain
 *   features/         cross-screen domain logic and state (no UI)
 *   hooks/, context/  React data/state plumbing
 *   api/              the backend client (one module per domain, api/client.ts barrel)
 *   lib/, utils/      framework-free helpers
 *
 * Imports point down this list. Tests (__tests__/, *.test.*, src/test/) may
 * reach anywhere, but every relative specifier — including vi.mock paths,
 * which fail silently when stale — must resolve to a real file.
 */
import { describe, expect, it } from 'vitest'

const sources = import.meta.glob('../**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true }) as Record<string, string>

// '../api/client.ts' → 'api/client.ts'
const files = new Map(Object.entries(sources).map(([k, v]) => [k.replace(/^\.\.\//, ''), v]))

const SPECIFIER = /(?:from|import|vi\.mock|vi\.doMock|vi\.importActual)\s*\(?\s*(['"])(\.{1,2}\/[^'"]*)\1/g
const RESOLVE_SUFFIXES = ['', '.ts', '.tsx', '/index.ts', '/index.tsx']
const ASSET = /\.(png|jpe?g|svg|css|json)$/

/** Forbidden target areas per source area (production code only). */
const FORBIDDEN: Record<string, string[]> = {
  api: ['hooks', 'components', 'pages', 'context', 'features'],
  lib: ['hooks', 'components', 'pages', 'context', 'features'],
  utils: ['hooks', 'components', 'pages', 'context', 'features', 'api'],
  hooks: ['components', 'pages'],
  context: ['components', 'pages'],
  features: ['components', 'pages'],
  components: ['pages'],
}

function normalize(path: string): string {
  const out: string[] = []
  for (const part of path.split('/')) {
    if (part === '..') out.pop()
    else if (part !== '.' && part !== '') out.push(part)
  }
  return out.join('/')
}

function dirname(path: string): string {
  return path.split('/').slice(0, -1).join('/')
}

function resolve(from: string, spec: string): string | null {
  const base = normalize(`${dirname(from)}/${spec}`)
  for (const suffix of RESOLVE_SUFFIXES) {
    if (files.has(base + suffix)) return base + suffix
  }
  return null
}

function isTest(path: string): boolean {
  return /(^|\/)__tests__\//.test(path) || /\.(test|spec)\.tsx?$/.test(path) || path.startsWith('test/')
}

/** 'pages/chats/ChatPane.tsx' → 'pages/chats'; 'pages/Home.tsx' → 'pages'; 'api/x.ts' → 'api' */
function area(path: string): string {
  const parts = path.split('/')
  if (parts[0] === 'pages' && parts.length > 2) return `pages/${parts[1]}`
  return parts.length > 1 ? parts[0] : '(root)'
}

function imports(path: string): { spec: string; target: string | null }[] {
  const src = files.get(path) ?? ''
  return [...src.matchAll(SPECIFIER)].map((m) => ({ spec: m[2], target: resolve(path, m[2]) }))
}

describe('architecture', () => {
  it('finds the source tree', () => {
    expect(files.has('api/client.ts')).toBe(true)
    expect(files.size).toBeGreaterThan(100)
  })

  it('every relative import and vi.mock path resolves to a file', () => {
    const dangling: string[] = []
    for (const path of files.keys()) {
      for (const { spec, target } of imports(path)) {
        if (target === null && !ASSET.test(spec)) dangling.push(`${path} → ${spec}`)
      }
    }
    expect(dangling).toEqual([])
  })

  it('imports follow the layering', () => {
    const violations: string[] = []
    for (const path of files.keys()) {
      if (isTest(path)) continue
      const from = area(path)
      for (const { spec, target } of imports(path)) {
        if (!target) continue
        const to = area(target)
        if (from === to) continue
        const top = to.split('/')[0]
        if ((FORBIDDEN[from.split('/')[0]] ?? []).includes(top)) violations.push(`${path} → ${spec}`)
      }
    }
    expect(violations).toEqual([])
  })

  it('a screen folder is private to its screen', () => {
    const violations: string[] = []
    for (const path of files.keys()) {
      if (isTest(path)) continue
      const from = area(path)
      for (const { spec, target } of imports(path)) {
        if (!target) continue
        const to = area(target)
        // pages/<a>/ may not import pages/<b>/; only pages/*.tsx (the screens) open their own folder
        if (to.startsWith('pages/') && to !== from && from !== 'pages') violations.push(`${path} → ${spec}`)
      }
    }
    expect(violations).toEqual([])
  })

  it('callers use the api/client barrel, not domain modules (keeps vi.mock(…/api/client) effective)', () => {
    const domainModules = [...files.keys()].filter(
      (p) => p.startsWith('api/') && !p.startsWith('api/types/') && dirname(p) === 'api' && !isTest(p),
    )
    const barrelled = new Set(
      [...(files.get('api/client.ts') ?? '').matchAll(/export \* from '\.\/([A-Za-z]+)'/g)].map((m) => `api/${m[1]}.ts`),
    )
    expect(barrelled.size).toBeGreaterThan(10)
    expect(domainModules.filter((p) => barrelled.has(p)).length).toBe(barrelled.size)
    const violations: string[] = []
    for (const path of files.keys()) {
      if (path.startsWith('api/')) continue
      for (const { spec, target } of imports(path)) {
        if (target && barrelled.has(target)) violations.push(`${path} → ${spec}`)
      }
    }
    expect(violations).toEqual([])
  })
})
