# Frontend infrastructure: public API

This is the shared layer that pages build on. Use it instead of hand-rolling fetch, sockets, dialogs or date parsing. Run `npm run typecheck` to type-check the app (`tsconfig.app.json`) and the tests (`tsconfig.test.json`: `*.test.ts(x)`, `__tests__/`, `src/test/`). Everything listed here has tests in `lib/__tests__`, `hooks/__tests__`, `components/ui/__tests__`, `utils/time.test.ts` and `src/__tests__/App.bootstrap.test.tsx`.

## Provider tree (App.tsx)

```
ThemeProvider > ToastProvider > ConfirmProvider > AppRoot (vault/session state machine)
  └─ (authenticated) WebSocketProvider > BrowserRouter > NotificationBridge + Routes > WorkspaceProvider > Layout
```

`useConfirm`, `useToast` and `useTheme` work on every screen. The WebSocket exists only after unlock: locking, logout and session expiry unmount it, which closes the socket for good.

---

## HTTP: `lib/http.ts` (re-exported from `api/client.ts`)

Import from `api/client` in pages: `import { request, stream, upload, ApiError } from '../api/client'`.

| Export | Purpose |
|---|---|
| `request<T>(path, opts?)` | JSON request. Sends the token header. Retries only GETs (3 attempts on network errors, 429, 502, 503 and 504). A 401 calls `notifySessionExpired()` except on the public routes (`/api/auth/vault-status`, `vault/init`, `vault/unlock`, `vault/reset`, `/api/session/validate`). Other `/api/auth/*` routes, such as `status`, now require the token. Resolves to `undefined` on 204 or an empty body. `opts` is `RequestInit` plus `attempts` and `skipSessionExpiry`. |
| `stream<T>(path, {method, body, signal, headers})` | Async generator of SSE events `{event, data, id}`. `data` is JSON-parsed and malformed payloads are skipped. Handles `event:`/`id:`, multi-line `data:`, comments, CRLF and chunk splits. Has the same 401 and error handling as `request`. Breaking out of the loop cancels the HTTP stream. |
| `upload<T>(path, formData, {onProgress, signal})` | XHR upload with progress, returning `{promise, abort}`. A 401 goes to session expiry. Abort rejects with an `AbortError`. |
| `requestBlob(path, signal?)` | Authenticated download as a Blob. |
| `apiFetch(method, path, body?, signal?)` / `streamSSE(path, signal?)` | Compatibility helpers that Settings/LlmSetup used to define locally. `streamSSE` yields only `data`. |
| `ApiError` | Every failure: `.message` (always readable; the backend's `detail` string), `.status`, `.code` (transport category: `http`, `validation`, `session_expired`, `html_response`, `network`, `bad_response`), `.serverCode` (the body's machine `code`, e.g. `'vault_corrupt'` on a 409 from unlock), `.errors` (422 field errors), `.body` (the full parsed error body, e.g. `existing_id` on a 409), `.path`, `.detail`. A 422 `detail[]` becomes `"field: msg; field2: msg"`. An HTML/SPA-fallback response becomes `"API endpoint not found: /api/…"`. |
| `isAbortError(e)`, `isSessionExpiredError(e)`, `isRateLimitError(e)` | Error predicates. |
| `validateSession()` (client.ts) | `GET /api/session/validate` (no token needed; refreshes the idle timer) returns `{valid, vault_status, idle_timeout_minutes}`, or `null` if the backend predates the endpoint. Network errors and 5xx throw. Never fires session expiry. |
| `resetVault(confirm)` (client.ts) | Sends `{confirm}`. The backend accepts only `"RESET"`, so collect it from a typed-confirmation input. |
| `lockVault()` (client.ts) / `useLockVault()` → `{lock, locking}` | `POST /api/auth/lock`, which revokes every session and stops Telegram. Then `notifySessionExpired()`, the same path as expiry: the token is cleared once, App shows Unlock and the socket stops. Later 401s hit the stampede guard. A 401 from the lock call still locks locally; other failures reject (the hook toasts "Lock failed"). The Sidebar has a Lock button, and the shortcut is `LOCK_ACTION` (⌘⇧L) in `lib/nav.ts`. |
| `getSessionTimeout()` / `setSessionTimeout(minutes)` (client.ts) | Idle session expiry: `0` (never) or `5..10080`; default 720. |

```ts
// SSE (replaces the hand-written parsers in Intelligence.tsx)
for await (const ev of stream<Record<string, unknown>>('/api/intelligence/chat', { method: 'POST', body: { conversation_id, message }, signal })) {
  handle(ev.event, ev.data)   // ev.event is "message" when the server sent no event: line
}
```

## Session: `api/session.ts`

`getSessionToken`, `setSessionToken`, `clearSessionToken`, `notifySessionExpired` (stampede-guarded). `onSessionExpired(cb)` and `onSessionTokenChanged(cb)` both support multiple listeners and return an unsubscribe function.

---

## WebSocket: `context/WebSocketContext.tsx`, `lib/ws.ts`, `hooks/useWebSocket.ts`

The app has one socket. Subscribing doesn't open a connection; it registers a handler that is removed on unmount.

```ts
useWsEvent('new_message', (e) => { /* e is narrowed to the new_message shape */ })
useWsEvent('resync', () => refetch())      // fires after every *re*connect: events may have been missed
useWebSocket((e) => { … })                 // all events (legacy signature, still leak-free)
```

- **Reconnects.** Backoff is exponential (1s base, 30s cap) with jitter in `[d/2, d]`. Nothing reconnects after a stop, dispose or session expiry. Close code `4001` (session ended) calls `notifySessionExpired()` and stops; `4003` (origin rejected) stops; anything else (e.g. `1001` shutdown) backs off and reconnects. After 3 failed handshakes the client asks `/api/session/validate`; if the session is gone it calls `notifySessionExpired()`, otherwise it keeps retrying. It reconnects immediately on `online` and when a new token is set.
- **`{type: 'resync'}`.** Synthetic and client-side only (in the `WSEvent` union). Every page that renders server state should refetch on it.
- **`useWebSocketClient()`.** Gives raw access to `subscribe(type | '*', handler) → unsubscribe`, `dispatch`, `connected` and `handlerCount`.
- **Desktop notifications.** Raised only by `<NotificationBridge>`, once per message, on any page. They use the server `notif_pref`, kept current by `setNotifPref()` in client.ts. They are skipped under pywebview because the backend already sends native notifications there. Pages must not call `new Notification`.

## Data hooks (`hooks/`)

### `useLatestQuery(key, fetcher, {enabled, deps, keepPreviousData})`
A fetch bound to an identity. The fetcher receives an `AbortSignal`. Each key change, refetch or unmount aborts the previous request. A result is applied only if its key is still current and no newer request or `setData` has superseded it.

Returns `{data, error, loading, fetching, isStale, refetch, setData}`:
- `data` is `undefined` until data for the current key arrives. Previous data is never shown under a new key unless you pass `keepPreviousData` (then `isStale` is true).
- `loading` means the current key hasn't settled yet; `fetching` means any request is in flight.
- `setData(v | prev => v)` is for optimistic updates. It discards in-flight reads, so an older read can't snap the value back. Call `refetch()` after the mutation settles.

```ts
const { data: window, loading, refetch, setData } =
  useLatestQuery(['messages', chatId, topicId], (signal) => getMessageWindow(chatId!, { topicId }, signal), { enabled: chatId != null })
useWsEvent('resync', () => { void refetch() })
```
Keys are JSON-serialised, so include every input the fetcher reads.

### `useDebouncedQuery(query, (q, signal) => …, {delayMs=250, minLength=1, enabled, deps, keepPreviousData=true})`
Typeahead with debounce, latest-only results and aborts. It returns everything `useLatestQuery` does plus `debouncedQuery` and `pending` (true while typing or fetching). Clearing the box hides results immediately. `useDebouncedValue(value, ms)` is exported too.

### `useAsyncAction(fn, {errorContext?, onError?})` → `{run, pending, error, reset}`
Single-flight: while a run is pending, further `run()` calls return the same promise without starting another, so double-clicks are safe. `run` never rejects; it resolves to `undefined` on failure and sets `error`. If you pass `errorContext`, failures also toast `"<context>: <message>"`.

### `useSettingField({initial, serverValue | get, set, echo?, debounceMs=500, validate?, label?})`
→ `{value, setValue(v, {immediate?}), flush, saving, invalid, error, loaded}`

Writes are debounced and serialized: never two in flight, and the latest value wins. When `echo` is given, the field adopts the server's normalised value. If a write fails, the field reverts to the last saved value and toasts `"Couldn't save <label>: …"`.

`validate` blocks a write without clamping the user's typing. `flush()` (call it on blur) writes now, or reverts an invalid value. `serverValue` is adopted unless an edit is pending. A pending edit is still written if the component unmounts during the debounce. Use `{immediate: true}` for selects and toggles.

### `useCoalescedRefetch(fn, ms)` → `trigger()`
Runs once on the leading edge. A burst during the window becomes one trailing call, and an async `fn` never overlaps itself. Use it for WebSocket storms: `useWsEvent('new_message', useCoalescedRefetch(refetch, 2000))`.

---

## UI primitives (`components/ui/`, barrel `components/ui/index.ts`)

### `<Modal open onClose title? icon? footer? size="sm|md|lg|xl" closeOnBackdrop closeOnEscape showCloseButton initialFocusRef position="center|top" ariaLabel>`
A portal with the standard backdrop (`bg-black/60 backdrop-blur-sm`) and panel (`slate-800`, `border-slate-700/80`, `rounded-xl`), styled with theme-aware classes.

While open, the modal:
- handles Escape (top-most modal only, capture phase);
- traps Tab focus and restores focus when it closes;
- suspends page shortcuts (`useKeyboardShortcuts`) through a modal scope.

Prefer rendering it conditionally (`{target && <Modal …/>}`) and capture the dialog's target, e.g. `{chatId, tgMessageId}`, when it opens, not at confirm time. `modalButton.secondary`, `modalButton.primary` and `modalButton.danger` give the standard button classes.

### `useConfirm()`
Replaces `window.confirm` and ad-hoc confirm dialogs.

```ts
const confirm = useConfirm()
if (!(await confirm({ title: 'Delete organization?', message: 'Also removes 4 positions.', destructive: true }))) return

const { confirmed, checked } = await confirm({
  title: 'Delete message?', destructive: true,
  checkbox: { label: 'Delete for everyone' },   // starts unticked on every open
})

// Single-flight action inside the dialog: spinner, errors shown inline, closes on success
await confirm({ title: 'Ban user?', destructive: true, confirmLabel: 'Ban', onConfirm: () => banUser(chatId, userId) })
```
Destructive dialogs focus Cancel. Opening a new confirm resolves any unanswered one as cancelled.

### Toasts: `useToast()` / `toast` / `reportError` (`lib/toast.ts`)
`toast.success(msg)`, `toast.error(msg)` and `toast.info(msg, {duration})` auto-dismiss after 3s, 6s and 4s respectively. At most 4 are visible, and an identical toast within 2s is collapsed. They work outside React too.

`reportError(e, 'Context')` replaces swallowed `.catch(() => {})`. It logs the error and toasts `"Context: message"`. It ignores aborts and session expiry, and returns `undefined`, so `p.catch((e) => reportError(e, 'Pin failed'))` works directly.

---

## Keyboard: `hooks/useKeyboardShortcuts.ts`, `lib/keyboard.ts`, `lib/nav.ts`

```ts
useKeyboardShortcuts({
  j: () => next(),
  enter: () => { if (!selected) return false; open(selected) },  // return false = not handled
  'mod+k': () => toggleSearch(),
}, { enabled: true, allowInModal: false, yieldScrollKeys: true })
```
- The listener is registered once, and the map and options are read through refs, so inline objects are fine.
- `preventDefault` happens only when the handler didn't return `false`.
- Plain keys are ignored in text fields (input, textarea, select, contenteditable). `mod+…` and Escape work everywhere.
- Enter and Space on a focused button, link, `[role=button]` and similar activate that element instead of the shortcut.
- With `yieldScrollKeys` (the default), arrow, Page, Home, End and Space keys yield to native scrolling when focus, or the last click, is inside a scrollable region. Pass `false` for list navigators that must keep arrows.
- All of this is suspended while a `<Modal>` is open unless you pass `allowInModal`.
- `lib/nav.ts` exports `NAV`, `SETTINGS_NAV` and `NAV_SHORTCUTS` (`mod+1…9` in sidebar order). The Sidebar, Layout's ⌘-number bindings and ShortcutHelp are all generated from it, so add a page there and all three update.

## Theme: `context/ThemeContext.tsx`, `hooks/useTheme.ts`, `lib/theme.ts`
`useTheme()` returns `{theme, setTheme, toggleTheme}`, shared app-wide. The stored theme is applied before first paint by an inline script in `index.html`, with `main.tsx` as a fallback. The storage key is `mingly_theme`.

## Dates: `utils/time.ts`
- `parseServerDate(s)` returns `Date | null`. Offset-less ISO (naive UTC from the DB, with `T` or a space) is read as UTC. Microseconds are truncated to milliseconds, because WebKit rejects more digits.
- Formatters:
  - `fmtRelative`: "Just now", "5m ago", "3h ago", "2d ago", "Mar 4"
  - `fmtRelativeDays`: "Today", "Yesterday", "3d ago", "2w ago", "4mo ago", "1y ago"
  - `fmtDate`: the time if today, otherwise "Yesterday", the weekday, or "Mar 4"
  - `fmtShortDate`, `fmtTime`, `fmtDateTime`
- `formatDuration(sec)` gives "4:32" or "1:02:03". `ageDays(s)` and `calendarDaysAgo(d)` are also exported.
- All formatters accept `string | number | Date | null` and return `''` for missing input. Never call `new Date(serverString)` directly.

## App bootstrap (App.tsx)
1. It always calls `GET /api/session/validate` first, which reports both token validity and vault status.
   - `valid` → shows the app. No channel gates it: with no account connected, Home and Chats show `NoAccountsNotice`, which links to Settings → Connections (`/settings?tab=connections`).
   - Not valid → clears any stale token, then shows Create vault (`unset`) or Unlock (`locked`/`unlocked`). Unlock always issues a fresh token for this window, and other windows keep theirs, then shows the app.
   - Create vault (`Onboarding`) sets the master password, optionally connects a first account, then shows LLM setup, then the app.
   - Every channel signs in through `components/AuthFlow.tsx` (the backend's step machine via `api/accounts.ts`): `fields`, `qr`, `oauth_browser` (opened with `openExternalUrl`, then polled every 2s while `poll`), `done`, `error`.
2. If the endpoint is missing (older backend), it falls back to vault status. An unlocked vault without a token for this tab still shows Unlock.
3. If any request fails (network or 5xx), it shows "Can't reach the Mingly backend" and retries with backoff (1s up to a 10s cap), with a "Retry now" button. It never falls through to "create vault".
