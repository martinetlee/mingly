/**
 * Per-browser storage of internal ids (active workspace, active board…).
 *
 * The schema v2 cutover replaced the database (docs/multichannel/SCHEMA_V2.md),
 * so an id saved before it names a different row — or one created later with
 * the same number. Every key holding an id carries the schema generation;
 * pre-v2 keys are dropped. Storage may be unavailable: then nothing persists.
 */
const GENERATION = 'v2'

/** Pre-v2 keys that held ids (exact names, or prefixes ending in ':') */
const LEGACY_KEYS = ['activeWorkspaceId', 'activeBoard:']

export function idKey(name: string): string {
  return `${GENERATION}:${name}`
}

export function readStoredId(name: string): number | null {
  try {
    const v = localStorage.getItem(idKey(name))
    const n = v ? parseInt(v, 10) : NaN
    return Number.isFinite(n) ? n : null
  } catch {
    return null
  }
}

export function storeId(name: string, id: number | null): void {
  try {
    if (id == null) localStorage.removeItem(idKey(name))
    else localStorage.setItem(idKey(name), String(id))
  } catch { /* storage unavailable: lasts this session */ }
}

/** Remove the pre-v2 id keys (idempotent) */
export function dropLegacyIdKeys(): void {
  try {
    const doomed: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)
      if (k && LEGACY_KEYS.some((l) => (l.endsWith(':') ? k.startsWith(l) : k === l))) doomed.push(k)
    }
    doomed.forEach((k) => localStorage.removeItem(k))
  } catch { /* storage unavailable */ }
}
