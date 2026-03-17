# Backend/Frontend Integration + Dead Code Review

## Findings

### 1. Shared auth/session transport is bypassed in `Settings` and `Intelligence`

Severity: Medium

The app has a central authenticated request path in `frontend/src/api/client.ts` that adds `X-Session-Token` and converts `401` responses into `notifySessionExpired()` so the UI returns to the unlock flow (`frontend/src/api/client.ts:4-25`). `Settings.tsx` and `Intelligence.tsx` each reimplement their own raw `fetch` helpers instead of going through that layer:

- `frontend/src/pages/Settings.tsx:10-41`
- `frontend/src/pages/Intelligence.tsx:98-116`

These helpers do not call `notifySessionExpired()` on `401`, unlike the shared client and unlike the defensive logic in `frontend/src/hooks/useWebSocket.ts:38-72`. The practical effect is that session expiry behavior now depends on which screen the user is on: normal pages lock correctly, but SSE/chat/setup flows can stay in a broken state and surface generic fetch errors instead of returning to the unlock screen.

This is also dead-code-adjacent because the project now maintains two parallel transport layers for the same backend.

### 2. The `getPrefs()` frontend contract has drifted from the backend response

Severity: Medium

The backend returns the full persisted prefs object verbatim (`backend/api/settings.py:50-52`), including keys such as `briefing_schedule`, `briefing_time`, `briefing_day`, `briefing_max_tokens`, and `catchup_summary_interval_minutes`. But the shared client still types `getPrefs()` as only `{ notif_pref: string }` (`frontend/src/api/client.ts:310-311`).

Both `Settings.tsx` and `Intelligence.tsx` work around that mismatch by casting the response to `Record<string, string>`:

- `frontend/src/pages/Settings.tsx:528-535`
- `frontend/src/pages/Intelligence.tsx:1448-1455`

That means the typed integration layer is no longer the source of truth for this API contract, so future backend changes will bypass TypeScript entirely. This is not dead code by itself, but it is a clear frontend/backend integration drift.

## High-confidence dead code

### 3. Unused frontend imports currently hidden by TS config

Confirmed by running:

```bash
npx tsc -p frontend/tsconfig.app.json --noEmit --noUnusedLocals --noUnusedParameters
```

Unused imports reported:

- `frontend/src/components/ContactInfoSection.tsx:3` imports `ContactPosition` but never uses it.
- `frontend/src/pages/Tracked.tsx:2` imports `Radio` but never uses it.

The repo currently suppresses this class of issue via `noUnusedLocals: false` and `noUnusedParameters: false` in `frontend/tsconfig.app.json`.

### 4. `/api/chats/{telegram_id}/monitor` appears to be orphaned

The backend still exposes monitor state and a monitor toggle:

- `backend/api/chats.py:199-213`
- `frontend/src/api/client.ts:113-117`

But there are no frontend call sites for `setMonitored(...)`, and no visible UI path that toggles `is_monitored`. A repo-wide search found no usages outside the client wrapper itself. Unless this endpoint is intentionally reserved for future work or external callers, it looks like dead API surface plus an unused field carried through chat payloads.

### 5. `/api/scan/chat/{telegram_id}` appears to be completely unintegrated

The scan router is mounted in the backend (`backend/api/router.py:29`), and the endpoint implementation is present in `backend/api/scan.py:14-61`, but there are no frontend callers and no backend scheduler/job references to this route. The only related usage is the implementation itself.

If manual message-importance rescoring is no longer a product feature, this is dead backend surface area.

## Lower-confidence cleanup candidates

These are not proven bugs, but they are worth confirming:

- `frontend/src/api/client.ts:291-300` exports `refreshAllStatuses` and `resolvePersonsBatch`, but there are no current frontend call sites.
- `frontend/src/api/client.ts:348-355` exports `getBriefingPrompt` and `setBriefingPrompt`, while the UI now uses the newer dual-prompt API at `frontend/src/api/client.ts:357-367`.
- `frontend/src/api/client.ts:377-380` exports `regenerateBriefingEntry`, but `Intelligence.tsx` uses a custom streaming implementation instead of this wrapper.

If none of these are used by tests, planned work, or external consumers, they are good candidates to remove.

## Suggested next steps

1. Consolidate `Settings` and `Intelligence` onto the shared API/session layer, adding shared SSE helpers if needed instead of per-page transport code.
2. Replace the `getPrefs()` return type with a real `Prefs` interface that matches `backend/api/settings.py`.
3. Remove the confirmed unused imports.
4. Decide whether `monitor` and `scan` are real product surface or retired features; if retired, remove the backend route and the frontend wrapper together.
