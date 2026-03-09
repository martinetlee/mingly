# Codex Security Review Findings

## Finding 1 (backend/api/llm.py:199)
**[P2] Downloaded binary is not integrity-verified**

The installer fetches a moving `latest` artifact and extracts/runs the binary without checksum or signature verification. Compromise of transport/source could lead to local code execution.

> **Response: VALID — no fix applied**
>
> The finding is technically correct: there is no post-download SHA256 or GPG verification.
>
> However, the practical risk is low for this threat model:
> - The download uses HTTPS from `github.com/ollama/ollama/releases/latest/download`, so transport is TLS-protected against MITM.
> - The download is explicitly user-initiated (Settings → AI Models → "Download & Set up") — not automatic.
> - Ollama's official release infrastructure (GitHub CDN) does not publish standalone SHA256 sums alongside the tgz, making automated verification non-trivial.
> - This is a single-user desktop app; the attacker would need to compromise GitHub's release CDN, a supply-chain risk far beyond the scope of this app.
>
> A future improvement would be to fetch the GitHub releases API, extract the expected digest from a `.sha256` sidecar if Ollama publishes one, and verify before executing. Deferred.

---

## Finding 2 (backend/api/router.py:31)
**[P1] Path traversal in /api/media**

The media endpoint concatenates `DATA_DIR` with a user-controlled path and serves it without resolving and checking containment. Attackers can use `../` to escape `DATA_DIR` and read arbitrary local files the app user can access.

> **Response: VALID — fixed**
>
> Confirmed. `DATA_DIR / file_path` without `.resolve()` allows `../` sequences to escape the intended directory. Even though the server only listens on `127.0.0.1`, a malicious local page or a crafted payload in the webview could reach it.
>
> Fix applied in `backend/api/router.py`: both `DATA_DIR` and the joined path are now fully resolved before comparison. Requests where the resolved path does not start with `DATA_DIR.resolve()` receive a 403.

---

## Finding 3 (frontend/src/components/MessageBubble.tsx:28)
**[P2] Unsafe URL scheme allowed in rendered HTML**

Markdown link conversion injects `href` from message content without protocol allowlisting. Combined with `dangerouslySetInnerHTML`, attacker-controlled messages can produce `javascript:` links and execute script on click.

> **Response: VALID — fixed**
>
> Confirmed. `escapeHtml()` only escapes `& < > "` — the string `javascript:alert(1)` passes through untouched and lands in `href="…"`. A Telegram message like `[click me](javascript:alert(1))` would produce a live XSS link rendered via `dangerouslySetInnerHTML`.
>
> Fix applied in `frontend/src/components/MessageBubble.tsx`: added `safeHref(url)` which returns `#` for any URL that does not begin with `https?://`, applied to both the `[[display]](url)` and `[display](url)` link patterns.
