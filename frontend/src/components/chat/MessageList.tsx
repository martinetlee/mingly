import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Virtuoso } from 'react-virtuoso'
import type { VirtuosoHandle } from 'react-virtuoso'
import { ArrowDown, Loader2 } from 'lucide-react'
import type { ChannelName, ConversationKind, Message, MessageSender } from '../../api/types'
import type { ScrollIntent } from '../../hooks/useChatMessages'
import { parseServerDate } from '../../utils/time'
import MessageBubble, { ServiceMessage } from './MessageBubble'
import type { ScamVerdictScope } from './MessageBubble'

interface Props {
  messages: Message[]
  /** Group / channel / email thread: show sender names and avatars */
  multiParty: boolean
  channel?: ChannelName | null
  /** Email threads render messages as emails */
  conversationKind?: ConversationKind
  /** Visual highlight only (scrolling is driven by `scrollIntent`) */
  highlightMessageId?: number | null
  onReply?: (msg: Message) => void
  onReact?: (msg: Message, emoji: string | null) => void
  onEdit?: (msg: Message) => void
  onDelete?: (msg: Message, album?: Message[]) => void
  onForward?: (msg: Message, album?: Message[]) => void
  onPin?: (msg: Message) => void
  onJumpToMessage?: (messageId: number) => void
  onJumpToExternal?: (externalId: string) => void
  /** The unread separator is drawn above this message */
  firstUnreadMessageId?: number | null
  /**
   * One-shot scroll request: each intent id is performed once, then ignored
   * (re-renders and new messages never re-centre the view). Without it the
   * list starts at the bottom.
   */
  scrollIntent?: ScrollIntent | null
  /** Called when the user scrolls to the top / bottom of the loaded window */
  onScrollNearTop?: () => void
  onScrollNearBottom?: () => void
  loadingOlder?: boolean
  loadingNewer?: boolean
  /** Rendered above the first message (load-older / history status) */
  header?: ReactNode
  /** The window isn't at the newest message (show "Jump to latest") */
  hasNewer?: boolean
  onJumpToLatest?: () => void
  /** Identity ids flagged as scammers */
  flaggedSenderIds?: Set<number>
  onReportScam?: (msg: Message) => void
  onScamVerdict?: (msg: Message, scope: ScamVerdictScope) => void
  /** Avatar context menu callbacks */
  onOpenProfile?: (sender: MessageSender) => void
  onSendMessage?: (sender: MessageSender) => void
  onMention?: (sender: MessageSender) => void
  onRestrict?: (sender: MessageSender, conversationId: number) => void
}

export interface Item {
  /** 'service' = a system line (joined, pinned, call…), never grouped with bubbles */
  type: 'single' | 'album' | 'service'
  /** Stable across window merges (albums keyed by grouped_id, so loading more of an album keeps its key) */
  key: string
  message: Message
  album?: Message[]
  ids: number[]
  showSender: boolean
  /** Day label when this item starts a new day */
  dateLabel: string | null
}

/** Group consecutive messages sharing a grouped_id into album items, with sender/day separators. */
export function buildItems(messages: Message[], multiParty: boolean): Item[] {
  const result: Item[] = []
  const thisYear = new Date().getFullYear()
  let prevMsg: Message | undefined
  let i = 0
  while (i < messages.length) {
    const msg = messages[i]
    const isService = msg.kind === 'service'
    let j = i + 1
    if (msg.grouped_id && !isService) {
      while (j < messages.length && messages[j].grouped_id === msg.grouped_id && messages[j].kind !== 'service') j++
    }
    const group = messages.slice(i, j)
    const isAlbum = group.length > 1
    // The bubble carries the caption item (Telegram puts the caption on one item)
    const primary = isAlbum ? (group.find((m) => m.text) ?? group[0]) : msg
    const date = parseServerDate(primary.date)
    const prevDate = prevMsg ? parseServerDate(prevMsg.date) : null
    result.push({
      type: isService ? 'service' : isAlbum ? 'album' : 'single',
      key: isAlbum ? `a${msg.grouped_id}` : `m${msg.id}`,
      message: primary,
      album: isAlbum ? group : undefined,
      ids: group.map((m) => m.id),
      // A bubble after a service line starts a new sender run
      showSender: !isService && multiParty && !primary.is_outgoing
        && (prevMsg?.kind === 'service' || primary.sender?.name !== prevMsg?.sender?.name),
      dateLabel: date && (!prevDate || date.toDateString() !== prevDate.toDateString())
        ? date.toLocaleDateString([], { day: 'numeric', month: 'long', year: date.getFullYear() !== thisYear ? 'numeric' : undefined })
        : null,
    })
    prevMsg = group[group.length - 1]
    i = j
  }
  return result
}

/** Virtuoso's firstItemIndex starts high so prepends can keep decreasing it */
export const BASE_INDEX = 1_000_000

/**
 * Keep Virtuoso's scroll anchor across window changes: a prepend of N items
 * lowers firstItemIndex by N, a top trim raises it; anything without overlap
 * is a new window (remount at the intent).
 */
export function rebaseFirstIndex(prevKeys: string[], prevBase: number, nextKeys: string[]): { base: number; replaced: boolean } {
  if (prevKeys.length === 0 || nextKeys.length === 0) return { base: BASE_INDEX, replaced: true }
  const prepended = nextKeys.indexOf(prevKeys[0])
  if (prepended >= 0) return { base: prevBase - prepended, replaced: false }
  const trimmed = prevKeys.indexOf(nextKeys[0])
  if (trimmed >= 0) return { base: prevBase + trimmed, replaced: false }
  return { base: BASE_INDEX, replaced: true }
}

/** Separator line between read and unread messages */
function UnreadSeparator() {
  return (
    <div className="flex items-center gap-3 my-2 px-2" data-unread-separator="">
      <div className="flex-1 border-t border-indigo-500/50" />
      <span className="flex-shrink-0 text-[11px] font-medium text-indigo-400">Unread messages</span>
      <div className="flex-1 border-t border-indigo-500/50" />
    </div>
  )
}

interface ListContext {
  header?: ReactNode
  loadingOlder?: boolean
  loadingNewer?: boolean
}

// Stable component identities (Virtuoso remounts header/footer when these change)
const ListHeader = ({ context }: { context?: ListContext }) => (
  <div className="px-4 pt-3">
    {context?.header}
    {context?.loadingOlder && (
      <div className="flex justify-center py-3"><Loader2 size={18} className="animate-spin text-indigo-400" /></div>
    )}
  </div>
)
const ListFooter = ({ context }: { context?: ListContext }) => (
  <div className="px-4 pb-3">
    {context?.loadingNewer && (
      <div className="flex justify-center py-3"><Loader2 size={18} className="animate-spin text-indigo-400" /></div>
    )}
  </div>
)
const COMPONENTS = { Header: ListHeader, Footer: ListFooter }

export default function MessageList({
  messages, multiParty, channel, conversationKind, highlightMessageId, onReply, onReact, onEdit, onDelete, onForward, onPin, onJumpToMessage,
  onJumpToExternal, firstUnreadMessageId, scrollIntent, onScrollNearTop, onScrollNearBottom, loadingOlder, loadingNewer, header,
  hasNewer, onJumpToLatest, flaggedSenderIds, onReportScam, onScamVerdict, onOpenProfile, onSendMessage, onMention, onRestrict,
}: Props) {
  const isGroup = multiParty
  const items = useMemo(() => buildItems(messages, isGroup), [messages, isGroup])
  const virtuosoRef = useRef<VirtuosoHandle>(null)
  const [atBottom, setAtBottom] = useState(true)
  const scrollerElRef = useRef<HTMLElement | null>(null)
  /**
   * Bottom pinning: rendered items replace Virtuoso's height estimates with
   * real heights after the first scroll, which left the view short of the end
   * (measured 55px in Chrome). Re-pin on height changes for a short window —
   * cancelled the moment the user scrolls — and set the scroller's own
   * scrollTop (Virtuoso's scrollTo stops before the footer).
   */
  const pinBottomUntilRef = useRef(0)
  const toBottom = useCallback(() => {
    const el = scrollerElRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [])
  const pinBottom = useCallback(() => {
    pinBottomUntilRef.current = Date.now() + 1500
    toBottom()
    requestAnimationFrame(toBottom)
  }, [toBottom])
  /** Distance from the bottom as of the last scroll event (i.e. before any re-measure) */
  const gapAtLastScrollRef = useRef(0)
  const listenersRef = useRef<(() => void) | null>(null)
  const setScroller = useCallback((el: HTMLElement | Window | null) => {
    listenersRef.current?.()
    listenersRef.current = null
    scrollerElRef.current = el instanceof HTMLElement ? el : null
    const sc = scrollerElRef.current
    if (!sc) return
    const cancel = () => { pinBottomUntilRef.current = 0 }
    const onScroll = () => { gapAtLastScrollRef.current = sc.scrollHeight - sc.scrollTop - sc.clientHeight }
    const user = ['wheel', 'touchstart', 'pointerdown', 'keydown']
    for (const ev of user) sc.addEventListener(ev, cancel, { passive: true })
    sc.addEventListener('scroll', onScroll, { passive: true })
    listenersRef.current = () => {
      for (const ev of user) sc.removeEventListener(ev, cancel)
      sc.removeEventListener('scroll', onScroll)
    }
  }, [])
  const consumedIntentRef = useRef<number | null>(null)

  const indexOfMessage = useCallback((messageId: number) => items.findIndex((it) => it.ids.includes(messageId)), [items])

  // firstItemIndex bookkeeping, recomputed in the same render as the data. A
  // new (non-overlapping) window bumps `generation`, remounting Virtuoso at
  // `initialTop`: the pending scroll intent's target, else the bottom.
  const layoutRef = useRef({
    items: [] as Item[], keys: [] as string[], base: BASE_INDEX, generation: 0,
    initialTop: { index: 0, align: 'end' as 'end' | 'center', offset: 0 }, initialIntentId: null as number | null,
  })
  if (layoutRef.current.items !== items) {
    const keys = items.map((it) => it.key)
    const prev = layoutRef.current
    const { base, replaced } = rebaseFirstIndex(prev.keys, prev.base, keys)
    let { initialTop, initialIntentId } = prev
    if (replaced) {
      const pending = scrollIntent && consumedIntentRef.current !== scrollIntent.id ? scrollIntent : null
      const idx = pending?.kind === 'message' ? items.findIndex((it) => it.ids.includes(pending.messageId)) : -1
      // Bottom: overshoot past the footer — the browser clamps to the true end
      initialTop = idx >= 0
        ? { index: idx, align: 'center', offset: 0 }
        : { index: Math.max(0, items.length - 1), align: 'end', offset: 100_000 }
      initialIntentId = pending && (pending.kind === 'bottom' || idx >= 0) ? pending.id : null
    }
    layoutRef.current = { items, keys, base, generation: prev.generation + (replaced ? 1 : 0), initialTop, initialIntentId }
  }
  const { base, generation, initialTop } = layoutRef.current

  // A window that starts at the bottom: pin it there while items get measured
  useEffect(() => {
    if (layoutRef.current.initialTop.align === 'end') pinBottom()
  }, [generation, pinBottom])

  // Intents not already served by a remount: scroll to the target once
  useEffect(() => {
    if (!scrollIntent || consumedIntentRef.current === scrollIntent.id || items.length === 0) return
    if (scrollIntent.kind === 'bottom') {
      // Past the last item too (footer): scroll to the very end, not "last item aligned to end"
      pinBottom()
      consumedIntentRef.current = scrollIntent.id
      return
    }
    if (layoutRef.current.initialIntentId === scrollIntent.id) {
      consumedIntentRef.current = scrollIntent.id
      return
    }
    const idx = indexOfMessage(scrollIntent.messageId)
    if (idx < 0) return // not in the window (yet)
    virtuosoRef.current?.scrollToIndex({ index: idx, align: 'center' })
    consumedIntentRef.current = scrollIntent.id
  }, [scrollIntent, items.length, indexOfMessage, pinBottom])

  const onHeightChanged = useCallback(() => {
    if (hasNewer) return
    // Re-measure while the user was really at the end (not mid-scroll-up) keeps them there
    if (Date.now() < pinBottomUntilRef.current || gapAtLastScrollRef.current < 4) toBottom()
  }, [hasNewer, toBottom])


  // Follow new messages at the end while the user is at the bottom (instead of
  // Virtuoso's followOutput, which aligns to the estimated height and ends short)
  const lastKey = items.length ? items[items.length - 1].key : null
  const prevLastKeyRef = useRef(lastKey)
  useEffect(() => {
    const appended = lastKey !== prevLastKeyRef.current
    prevLastKeyRef.current = lastKey
    if (appended && !hasNewer && gapAtLastScrollRef.current < 150) pinBottom()
  }, [lastKey, hasNewer, pinBottom])
  const context = useMemo<ListContext>(() => ({ header, loadingOlder, loadingNewer }), [header, loadingOlder, loadingNewer])

  const itemContent = useCallback((_index: number, item: Item) => {
    const msg = item.message
    return (
      <div data-message-ids={item.ids.join(' ')} className="px-4">
        {item.dateLabel && (
          <div className="flex items-center justify-center py-2 my-1">
            <span className="text-[11px] font-medium text-slate-500">{item.dateLabel}</span>
          </div>
        )}
        {firstUnreadMessageId != null && item.ids.includes(firstUnreadMessageId) && <UnreadSeparator />}
        {item.type === 'service' ? (
          <ServiceMessage
            message={msg}
            highlighted={highlightMessageId != null && item.ids.includes(highlightMessageId)}
            onJumpToMessage={onJumpToMessage}
            onJumpToExternal={onJumpToExternal}
          />
        ) : (
          <MessageBubble
            message={msg}
            album={item.album}
            showSender={item.showSender}
            showAvatar={item.showSender}
            isGroupChat={isGroup}
            highlighted={highlightMessageId != null && item.ids.includes(highlightMessageId)}
            channel={channel}
            conversationKind={conversationKind}
            onReply={onReply}
            onReact={onReact}
            onEdit={onEdit}
            onDelete={onDelete}
            onForward={onForward}
            onPin={onPin}
            onJumpToMessage={onJumpToMessage}
            onJumpToExternal={onJumpToExternal}
            isFlaggedSender={msg.sender?.identity_id != null && !!flaggedSenderIds?.has(msg.sender.identity_id)}
            onReportScam={onReportScam}
            onScamVerdict={onScamVerdict}
            onOpenProfile={onOpenProfile}
            onSendMessage={onSendMessage}
            onMention={onMention}
            onRestrict={onRestrict}
          />
        )}
      </div>
    )
  }, [firstUnreadMessageId, isGroup, channel, conversationKind, highlightMessageId, onReply, onReact, onEdit, onDelete, onForward, onPin, onJumpToMessage, onJumpToExternal,
    flaggedSenderIds, onReportScam, onScamVerdict, onOpenProfile, onSendMessage, onMention, onRestrict])

  if (items.length === 0) {
    return (
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-4 py-3" data-testid="message-scroll">
        {header}
        <div className="flex flex-1 items-center justify-center text-slate-500 text-sm">No messages yet</div>
      </div>
    )
  }

  const showJump = !!onJumpToLatest && (hasNewer || !atBottom)

  return (
    <div className="relative flex min-h-0 flex-1 flex-col" data-testid="message-scroll">
      <Virtuoso
        // A new (non-overlapping) window remounts at its intent; prepends/appends keep the anchor
        key={generation}
        ref={virtuosoRef}
        scrollerRef={setScroller}
        className="flex-1"
        data={items}
        firstItemIndex={base}
        initialTopMostItemIndex={initialTop}
        computeItemKey={(_i, item) => item.key}
        itemContent={itemContent}
        components={COMPONENTS}
        context={context}
        atBottomStateChange={setAtBottom}
        totalListHeightChanged={onHeightChanged}
        atBottomThreshold={150}
        startReached={onScrollNearTop ? () => onScrollNearTop() : undefined}
        endReached={onScrollNearBottom && hasNewer ? () => onScrollNearBottom() : undefined}
        increaseViewportBy={{ top: 600, bottom: 600 }}
      />
      {showJump && (
        <button
          onClick={onJumpToLatest}
          className="absolute bottom-3 right-5 flex items-center gap-1.5 rounded-full border border-slate-600 bg-slate-800/95 px-3 py-1.5 text-xs text-slate-200 shadow-lg hover:bg-slate-700"
          title="Jump to latest"
        >
          <ArrowDown size={13} />
          Latest
        </button>
      )}
    </div>
  )
}
