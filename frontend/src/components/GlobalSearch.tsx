import { useEffect, useState, useRef, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { Search, MessageSquare, Users, Building2, FileText, Globe, X, Loader2, AlertTriangle } from 'lucide-react'
import type { GlobalSearchResults, TelegramSearchResult } from '../api/types'
import { globalSearch, telegramSearch, ensureChat } from '../api/client'
import { useDebouncedQuery } from '../hooks/useDebouncedQuery'
import { errorMessage } from '../lib/toast'
import { fmtRelative } from '../utils/time'
import { Modal } from './ui'

// Telegram directory searches cost two RPCs and repeat queries are common —
// cache results briefly so re-searching the same term renders instantly.
const TELEGRAM_CACHE_TTL_MS = 3 * 60 * 1000
const telegramCache = new Map<string, { ts: number; results: TelegramSearchResult[] }>()

async function cachedTelegramSearch(query: string, signal?: AbortSignal): Promise<TelegramSearchResult[]> {
  const key = query.trim().toLowerCase()
  const hit = telegramCache.get(key)
  if (hit && Date.now() - hit.ts < TELEGRAM_CACHE_TTL_MS) return hit.results
  const data = await telegramSearch(query, 20, signal)
  telegramCache.set(key, { ts: Date.now(), results: data.telegram_dialogs })
  return data.telegram_dialogs
}

interface ResultItem {
  type: 'chat' | 'contact' | 'organization' | 'message' | 'telegram'
  id: string
  label: string
  sublabel?: string
  icon: 'chat' | 'contact' | 'org' | 'message' | 'telegram'
  navigateTo: string
  telegramId?: number
}

function flattenResults(results: GlobalSearchResults): ResultItem[] {
  const items: ResultItem[] = []

  for (const c of results.chats) {
    items.push({
      type: 'chat',
      id: `chat-${c.telegram_id}`,
      label: c.name,
      sublabel: c.type,
      icon: 'chat',
      navigateTo: `/chats?chatId=${c.telegram_id}`,
    })
  }

  for (const c of results.contacts) {
    items.push({
      type: 'contact',
      id: `contact-${c.id}`,
      label: c.name,
      sublabel: c.username ? `@${c.username}` : undefined,
      icon: 'contact',
      navigateTo: `/profile/${c.telegram_id}`,
    })
  }

  for (const o of results.organizations) {
    items.push({
      type: 'organization',
      id: `org-${o.id}`,
      label: o.name,
      sublabel: o.context ?? undefined,
      icon: 'org',
      navigateTo: `/organizations?select=${o.id}`,
    })
  }

  for (const m of results.messages) {
    items.push({
      type: 'message',
      id: `msg-${m.chat_id}-${m.tg_message_id}`,
      label: m.text,
      sublabel: `${m.chat_name}${m.sender_name ? ` — ${m.sender_name}` : ''} · ${fmtRelative(m.date)}`,
      icon: 'message',
      navigateTo: `/chats?chatId=${m.chat_id}&msgId=${m.tg_message_id}`,
    })
  }

  return items
}

function telegramResultsToItems(results: TelegramSearchResult[]): ResultItem[] {
  return results.map((t) => ({
    type: 'telegram' as const,
    id: `tg-${t.telegram_id}`,
    label: t.name,
    sublabel: t.username ? `@${t.username}` : t.type,
    icon: 'telegram' as const,
    navigateTo: `/chats?chatId=${t.telegram_id}`,
    telegramId: t.telegram_id,
  }))
}

const ICON_MAP = {
  chat: MessageSquare,
  contact: Users,
  org: Building2,
  message: FileText,
  telegram: Globe,
}

const CATEGORY_LABELS: Record<string, string> = {
  chat: 'Chats',
  contact: 'Contacts',
  organization: 'Organizations',
  message: 'Messages',
  telegram: 'Telegram',
}

export default function GlobalSearch({ open, onClose }: { open: boolean; onClose: () => void }) {
  if (!open) return null
  // Mounted per open: query, results and errors always start fresh
  return <GlobalSearchPanel onClose={onClose} />
}

function GlobalSearchPanel({ onClose }: { onClose: () => void }) {
  const [query, setQuery] = useState('')
  const [ensuring, setEnsuring] = useState(false)
  const [ensureError, setEnsureError] = useState<string | null>(null)
  const [selectedIdx, setSelectedIdx] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const navigate = useNavigate()

  // Local + Telegram run independently (debounced, latest-only, aborted on
  // change): a failure in one section never blanks the other
  const local = useDebouncedQuery(query, (q, signal) => globalSearch(q, 20, signal), {
    delayMs: 300, minLength: 2, keepPreviousData: false,
  })
  const tg = useDebouncedQuery(query, (q, signal) => cachedTelegramSearch(q, signal), {
    delayMs: 300, minLength: 2, keepPreviousData: false,
  })
  const results: GlobalSearchResults | null = local.data ?? null
  const telegramResults: TelegramSearchResult[] = tg.data ?? []
  const loading = local.pending
  const telegramLoading = tg.pending
  const localError = local.error != null ? errorMessage(local.error) : null
  const telegramError = tg.error != null ? errorMessage(tg.error) : null

  useEffect(() => { setSelectedIdx(0) }, [results, telegramResults.length])

  // Build unified items list: local results + Telegram results (deduped)
  const localItems = results ? flattenResults(results) : []
  const tgItems = telegramResultsToItems(telegramResults)
  // Deduplicate: skip Telegram results whose telegram_id already appears in local chats/contacts
  const localTgIds = new Set<number>()
  if (results) {
    for (const c of results.chats) localTgIds.add(c.telegram_id)
    for (const c of results.contacts) localTgIds.add(c.telegram_id)
  }
  const dedupedTgItems = tgItems.filter((t) => !t.telegramId || !localTgIds.has(t.telegramId))
  const items = [...localItems, ...dedupedTgItems]

  const handleSelect = useCallback(
    async (item: ResultItem) => {
      if (item.type === 'telegram' && item.telegramId) {
        // Ensure the chat exists locally before navigating
        setEnsuring(true)
        setEnsureError(null)
        try {
          await ensureChat(item.telegramId)
        } catch {
          // Navigating anyway would land on a blank chat pane — surface the error instead
          setEnsureError(`Couldn't open "${item.label}" from Telegram`)
          return
        } finally {
          setEnsuring(false)
        }
      }
      onClose()
      navigate(item.navigateTo)
    },
    [navigate, onClose],
  )

  // Keyboard navigation
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setSelectedIdx((prev) => Math.min(prev + 1, items.length - 1))
      } else if (e.key === 'ArrowUp') {
        e.preventDefault()
        setSelectedIdx((prev) => Math.max(prev - 1, 0))
      } else if (e.key === 'Enter' && items[selectedIdx]) {
        e.preventDefault()
        handleSelect(items[selectedIdx])
      } else if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      }
    },
    [items, selectedIdx, handleSelect, onClose],
  )

  // Scroll selected item into view
  useEffect(() => {
    const el = listRef.current?.querySelector(`[data-idx="${selectedIdx}"]`)
    el?.scrollIntoView({ block: 'nearest' })
  }, [selectedIdx])

  // Group items by type for display
  const grouped = new Map<string, ResultItem[]>()
  for (const item of items) {
    const list = grouped.get(item.type) ?? []
    list.push(item)
    grouped.set(item.type, list)
  }

  const allDone = !loading && !telegramLoading
  let globalIdx = 0

  const sectionError = (label: string, msg: string) => (
    <div role="alert" className="mx-4 my-2 flex items-center gap-2 rounded-md border border-rose-500/30 bg-rose-500/10 px-3 py-1.5 text-[11px] text-rose-400">
      <AlertTriangle size={12} className="flex-shrink-0" />
      <span className="truncate">{label}: {msg}</span>
    </div>
  )

  return (
    <Modal
      open
      onClose={onClose}
      position="top"
      size="xl"
      ariaLabel="Search"
      initialFocusRef={inputRef}
      className="overflow-hidden"
    >
      <div onKeyDown={handleKeyDown}>
        {/* Search input */}
        <div className="flex items-center gap-3 border-b border-slate-700/50 px-4 py-3">
          <Search size={16} className="flex-shrink-0 text-slate-400" />
          <input
            ref={inputRef}
            type="text"
            placeholder="Search chats, contacts, messages..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="flex-1 bg-transparent text-sm text-gray-200 outline-none placeholder:text-slate-500"
          />
          <div className="flex items-center gap-1.5">
            {query && (
              <button onClick={() => setQuery('')} className="text-slate-500 hover:text-slate-300 transition-colors">
                <X size={14} />
              </button>
            )}
            <kbd className="rounded border border-slate-600 bg-slate-700 px-1.5 py-0.5 text-[10px] text-slate-400">
              ESC
            </kbd>
          </div>
        </div>

        {/* Results */}
        <div ref={listRef} className="max-h-[50vh] overflow-y-auto">
          {ensureError && (
            <div className="mx-4 mt-2 rounded-md border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-400">
              {ensureError}
            </div>
          )}
          {query.length < 2 ? (
            <div className="px-4 py-8 text-center text-sm text-slate-500">
              Type to search across chats, contacts, and messages
            </div>
          ) : loading && items.length === 0 && !localError ? (
            <div className="px-4 py-8 text-center text-sm text-slate-500">
              Searching...
            </div>
          ) : ensuring ? (
            <div className="px-4 py-8 text-center text-sm text-slate-400 flex items-center justify-center gap-2">
              <Loader2 size={14} className="animate-spin" />
              Syncing chat...
            </div>
          ) : items.length === 0 && allDone && !localError && !telegramError ? (
            <div className="px-4 py-8 text-center text-sm text-slate-500">
              No results found for &ldquo;{query}&rdquo;
            </div>
          ) : (
            <>
              {localError && sectionError('Local search failed', localError)}
              {Array.from(grouped.entries()).map(([type, groupItems]) => (
                <div key={type}>
                  <div className="sticky top-0 bg-slate-800/95 px-4 py-1.5 text-[10px] font-medium uppercase tracking-wider text-slate-500 backdrop-blur-sm">
                    {CATEGORY_LABELS[type] ?? type} ({groupItems.length})
                  </div>
                  {groupItems.map((item) => {
                    const idx = globalIdx++
                    const Icon = ICON_MAP[item.icon]
                    return (
                      <button
                        key={item.id}
                        data-idx={idx}
                        onClick={() => handleSelect(item)}
                        onMouseEnter={() => setSelectedIdx(idx)}
                        className={`flex w-full items-center gap-3 px-4 py-2 text-left transition-colors ${
                          selectedIdx === idx ? 'bg-indigo-500/15' : 'hover:bg-slate-700/50'
                        }`}
                      >
                        <Icon size={14} className={`flex-shrink-0 ${item.type === 'telegram' ? 'text-indigo-400' : 'text-slate-500'}`} />
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-sm text-gray-200">{item.label}</p>
                          {item.sublabel && (
                            <p className="truncate text-[11px] text-slate-500">{item.sublabel}</p>
                          )}
                        </div>
                        {item.type === 'telegram' && (
                          <span className="flex-shrink-0 rounded-full bg-indigo-500/15 px-2 py-0.5 text-[10px] text-indigo-400">
                            not synced
                          </span>
                        )}
                      </button>
                    )
                  })}
                </div>
              ))}
              {telegramLoading && (
                <div className="flex items-center gap-2 px-4 py-2 text-[11px] text-slate-500">
                  <Loader2 size={12} className="animate-spin" />
                  Searching Telegram...
                </div>
              )}
              {telegramError && !telegramLoading && sectionError('Telegram search unavailable', telegramError)}
            </>
          )}
        </div>

        {/* Footer */}
        {items.length > 0 && (
          <div className="flex items-center justify-between border-t border-slate-700/50 px-4 py-2 text-[10px] text-slate-500">
            <div className="flex items-center gap-2">
              <span><kbd className="rounded border border-slate-600 bg-slate-700 px-1 py-0.5">↑↓</kbd> navigate</span>
              <span><kbd className="rounded border border-slate-600 bg-slate-700 px-1 py-0.5">↵</kbd> select</span>
            </div>
            <span>{items.length} result{items.length !== 1 ? 's' : ''}</span>
          </div>
        )}
      </div>
    </Modal>
  )
}
