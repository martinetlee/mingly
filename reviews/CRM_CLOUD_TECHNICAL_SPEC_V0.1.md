# Mingly Cloud CRM — Technical Specification v0.1

## Overview

A cloud service providing enterprise CRM capabilities on top of the Mingly Telegram client. Users maintain full control of personal contacts locally; organizations get a shared contact directory via explicit opt-in contributions.

**Stack**: FastAPI (Python), PostgreSQL, Redis (rate limiting + token cache), S3-compatible blob store (encrypted backups).

**Transport security**: All cloud API endpoints MUST be served over TLS (HTTPS). Plaintext HTTP connections MUST be rejected or redirected. This is non-negotiable — magic link tokens, claim secrets, JWTs, and refresh tokens all transit between client and server and are interceptable without TLS.

---

## 1. Database Schema

### 1.1 CloudUser

| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK, default gen |
| primary_email | VARCHAR(255) | UNIQUE, NOT NULL |
| created_at | TIMESTAMP | NOT NULL, default now |

### 1.2 VerifiedEmail

| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| user_id | UUID | FK → CloudUser, NOT NULL |
| email | VARCHAR(255) | UNIQUE, NOT NULL |
| domain | VARCHAR(255) | NOT NULL, indexed |
| verified_at | TIMESTAMP | NOT NULL |
| last_reverified_at | TIMESTAMP | nullable |

- `domain` is extracted from email on insert (e.g., `acme.com` from `martin@acme.com`)
- Checked against `BlockedDomain` table on insert

### 1.3 BlockedDomain

| Column | Type | Constraints |
|--------|------|-------------|
| domain | VARCHAR(255) | PK |

Seeded with public providers: `gmail.com`, `yahoo.com`, `outlook.com`, `hotmail.com`, `icloud.com`, `protonmail.com`, etc.

### 1.4 Org

| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| name | VARCHAR(255) | NOT NULL |
| domain | VARCHAR(255) | UNIQUE, NOT NULL |
| join_policy | ENUM('auto', 'approval_required') | NOT NULL, default 'auto' |
| created_by | UUID | FK → CloudUser, NOT NULL |
| created_at | TIMESTAMP | NOT NULL |

- One org per domain, enforced by unique constraint.
- `join_policy`: `auto` (default, all plans) — verified domain email = instant join, admin notified. `approval_required` (paid plan only) — join creates a pending request, admin must approve.

### 1.5 OrgMembership

| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| user_id | UUID | FK → CloudUser, NOT NULL |
| org_id | UUID | FK → Org, NOT NULL |
| role | ENUM('admin', 'member') | NOT NULL, default 'member' |
| status | ENUM('pending', 'active', 'suspended', 'revoked') | NOT NULL, default 'active' |
| joined_at | TIMESTAMP | NOT NULL |
| revoked_at | TIMESTAMP | nullable |

- UNIQUE constraint on `(user_id, org_id)`
- All shared pool queries filter: `status = 'active'`
- `pending`: set when org `join_policy = 'approval_required'`. Pending members cannot access the shared pool. Admin approves (`pending → active`) or denies (`pending → revoked`).

### 1.6 SharedOrganization

| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| org_id | UUID | FK → Org, NOT NULL |
| name | VARCHAR(255) | NOT NULL |
| normalized_name | VARCHAR(255) | NOT NULL, indexed |
| metadata | JSONB | nullable |
| created_by | UUID | FK → CloudUser, NOT NULL |
| created_at | TIMESTAMP | NOT NULL |
| deleted_at | TIMESTAMP | nullable |
| deleted_by | UUID | FK → CloudUser, nullable |

- `normalized_name`: lowercased, stripped of punctuation, for dedup matching
- UNIQUE constraint on `(org_id, normalized_name)` — scoped to non-deleted rows (`WHERE deleted_at IS NULL`)
- Soft-delete: `deleted_at`/`deleted_by` set on deletion. Rows retained for audit/cleanup. All queries filter `WHERE deleted_at IS NULL` by default.

### 1.7 SharedContact

The person. One record per unique Telegram user per org. Position and relationship context live on the link table (1.8), not here.

| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| org_id | UUID | FK → Org, NOT NULL |
| telegram_id | BIGINT | NOT NULL |
| telegram_handle | VARCHAR(255) | nullable |
| name | VARCHAR(255) | NOT NULL |
| contributed_by | UUID | FK → CloudUser, NOT NULL |
| contributed_at | TIMESTAMP | NOT NULL |
| updated_by | UUID | FK → CloudUser, NOT NULL |
| updated_at | TIMESTAMP | NOT NULL |
| deleted_at | TIMESTAMP | nullable |
| deleted_by | UUID | FK → CloudUser, nullable |

- `telegram_id` is required. Every Telegram user has one, and the local Mingly client always has it from Telethon. This makes dedup airtight.
- UNIQUE constraint on `(org_id, telegram_id) WHERE deleted_at IS NULL` — one record per person per org. No duplicates possible.
- `updated_by`/`updated_at`: set on every edit. History tracked via `SharedContactHistory` (see 1.14). `updated_by` initialized to `contributed_by` on creation.
- Soft-delete: `deleted_at`/`deleted_by` set on deletion. Rows retained for audit/cleanup. All queries filter `WHERE deleted_at IS NULL` by default.

### 1.8 SharedContactOrgLink

Many-to-many between SharedContact and SharedOrganization. A contact can be linked to multiple external orgs with different roles and relationship contexts.

| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| shared_contact_id | UUID | FK → SharedContact, NOT NULL |
| shared_org_id | UUID | FK → SharedOrganization, NOT NULL |
| org_id | UUID | FK → Org, NOT NULL (denormalized for query efficiency) |
| position | VARCHAR(255) | nullable |
| relationship_context | VARCHAR(50) | nullable (client, vendor, partner, prospect) |
| linked_by | UUID | FK → CloudUser, NOT NULL |
| linked_at | TIMESTAMP | NOT NULL |
| updated_by | UUID | FK → CloudUser, NOT NULL |
| updated_at | TIMESTAMP | NOT NULL |
| deleted_at | TIMESTAMP | nullable |
| deleted_by | UUID | FK → CloudUser, nullable |

- UNIQUE constraint on `(shared_contact_id, shared_org_id) WHERE deleted_at IS NULL` — one link per contact per external org
- Soft-delete: same pattern as SharedContact
- Example: Alice has two links — PartnerCo (VP Sales, client) and VendorInc (Board Member, partner)

### 1.9 SharedContactGroupChat

Links shared contacts to group chat metadata (participants only, no content).

| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| shared_contact_id | UUID | FK → SharedContact, NOT NULL |
| org_id | UUID | FK → Org, NOT NULL |
| chat_telegram_id | BIGINT | NOT NULL |
| chat_title | VARCHAR(255) | NOT NULL |
| added_by | UUID | FK → CloudUser, NOT NULL |
| added_at | TIMESTAMP | NOT NULL |

- Records that a shared contact is a participant in a specific group chat
- No message content, no DMs

### 1.10 ContactViewLog

| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| shared_contact_id | UUID | FK → SharedContact, NOT NULL |
| viewed_by | UUID | FK → CloudUser, NOT NULL |
| viewed_at | TIMESTAMP | NOT NULL |

Append-only. No deletes.

### 1.11 PersonalContactBackup

| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| user_id | UUID | FK → CloudUser, UNIQUE, NOT NULL |
| encrypted_payload | BYTEA | NOT NULL |
| updated_at | TIMESTAMP | NOT NULL |

- One row per user. Full replace on each backup sync.
- Server stores ciphertext only. Zero-knowledge. No hint field — key is derived from master password, not a separate passphrase.

### 1.12 MagicLinkToken

| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| request_id | UUID | UNIQUE, NOT NULL (returned to client for polling/claiming) |
| claim_secret_hash | VARCHAR(64) | NOT NULL (hashed secret, returned to client only) |
| email | VARCHAR(255) | NOT NULL |
| token_hash | VARCHAR(64) | UNIQUE, NOT NULL |
| purpose | ENUM('login', 'verify_email', 'reverify') | NOT NULL |
| expires_at | TIMESTAMP | NOT NULL |
| used_at | TIMESTAMP | nullable (set when magic link is clicked via `/consume`) |
| claimed_at | TIMESTAMP | nullable (set when desktop app claims tokens via `/claim`) |

- Tokens expire in 15 minutes
- Single-use: `used_at` set on consumption via `/consume`
- Store token hash and claim secret hash only, never plaintext
- `request_id` is returned to the desktop client for claiming; unguessable UUID
- `claim_secret` is a 64-char random hex string, returned to the desktop client alongside `request_id`. Never appears in the magic link email or browser context. Stored as hash in DB. Required via `X-Claim-Secret` header on every `/claim` call — `request_id` alone is insufficient.
- **No token storage**: JWTs and refresh tokens are never stored in the DB. They are minted on-demand by `/claim` when it sees `used_at` is set and `claimed_at` is null. This eliminates the plaintext token exposure window entirely.
- `claimed_at` prevents replay: once tokens are minted and returned, subsequent `/claim` calls return `{status: "consumed"}`

### 1.13 RefreshToken

| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| user_id | UUID | FK → CloudUser, NOT NULL |
| token_hash | VARCHAR(64) | UNIQUE, NOT NULL |
| expires_at | TIMESTAMP | NOT NULL |
| revoked_at | TIMESTAMP | nullable |

### 1.14 SharedContactHistory

Append-only edit history for shared contacts and their org links. One row per field change.

| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| shared_contact_id | UUID | FK → SharedContact, NOT NULL |
| shared_contact_org_link_id | UUID | FK → SharedContactOrgLink, nullable |
| edited_by | UUID | FK → CloudUser, NOT NULL |
| edited_at | TIMESTAMP | NOT NULL |
| field_name | VARCHAR(50) | NOT NULL (e.g., "name", "position", "relationship_context") |
| old_value | TEXT | nullable |
| new_value | TEXT | nullable |

- One row per field changed per edit (an edit updating name + position = 2 rows)
- `shared_contact_org_link_id`: set when the edit is on a link field (position, relationship_context). Null when the edit is on the contact itself (name, telegram_handle).
- Provides full audit trail for future conflict resolution
- Append-only: no updates, no deletes

---

## 2. API Endpoints

Base URL: `/api/v1`

### Versioning & Deprecation Policy

**Versioning scheme:** URL path prefix (`/api/v1`, `/api/v2`). Major version increments only for breaking changes.

**What counts as breaking:**
- Removing or renaming an endpoint
- Removing or renaming a response field
- Changing a field's type
- Adding a new required request field
- Changing auth requirements
- Changing error codes for existing error cases

**What is NOT breaking (can ship in the current version):**
- Adding a new optional request field
- Adding a new response field
- Adding a new endpoint
- Adding a new error code for a new error case

**Deprecation process:**
1. New version (`/api/v2`) ships alongside the old (`/api/v1`)
2. Old version returns a `Deprecation: true` response header and a `Sunset` header with the retirement date
3. Minimum 6 months between deprecation announcement and retirement
4. Desktop client shows a notification when it receives a `Deprecation` header: "Please update Mingly to continue using cloud features."
5. After sunset date, old version returns `410 Gone`

**Practical constraints:**
- The desktop client pins to a specific API version. Client updates are required to move to a new version.
- At most 2 versions alive at the same time (current + deprecated). No long tail of supported versions.

### Standard Error Response Format

All error responses use a consistent JSON structure:

```json
{
  "error": {
    "code": "RESOURCE_NOT_FOUND",
    "message": "Shared contact not found",
    "details": {}
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `error.code` | string | Machine-readable error code (UPPER_SNAKE_CASE) |
| `error.message` | string | Human-readable description |
| `error.details` | object | Optional context (e.g., which field failed validation) |

**Error codes by HTTP status:**

| Status | Code | When |
|--------|------|------|
| 400 | `VALIDATION_ERROR` | Missing/invalid fields. `details` contains per-field errors: `{"fields": {"email": "Invalid format"}}` |
| 400 | `BLOCKED_DOMAIN` | Email domain is on public provider blocklist |
| 401 | `TOKEN_EXPIRED` | JWT has expired |
| 401 | `TOKEN_INVALID` | JWT signature invalid or malformed |
| 403 | `NOT_MEMBER` | User is not an active member of the org |
| 403 | `NOT_ADMIN` | Action requires admin role |
| 403 | `CLAIM_SECRET_INVALID` | Wrong or missing `X-Claim-Secret` header |
| 404 | `RESOURCE_NOT_FOUND` | Entity does not exist (or is soft-deleted) |
| 409 | `DUPLICATE_CONTACT` | Contact with this `telegram_id` already exists in org. `details: {"existing_id": "uuid"}` |
| 409 | `DUPLICATE_ORG` | Org already exists for this domain. `details: {"existing_id": "uuid"}` |
| 409 | `DUPLICATE_LINK` | Contact already linked to this shared org. `details: {"existing_id": "uuid"}` |
| 429 | `RATE_LIMITED` | Too many requests. `details: {"retry_after_seconds": 30}` |
| 500 | `INTERNAL_ERROR` | Unexpected server error. No sensitive details exposed. |

### 2.1 Auth

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| POST | `/auth/magic-link` | Request magic link for login or email verification | None |
| GET | `/auth/magic-link/verify` | Landing page from email link — auto-submits POST to `/consume` | None |
| POST | `/auth/magic-link/consume` | Validate magic link token, mark authenticated (browser endpoint) | None |
| GET | `/auth/magic-link/claim/{request_id}` | Claim session tokens once authenticated (desktop app polling) | None |
| POST | `/auth/refresh` | Exchange refresh token for new JWT | Refresh token |
| POST | `/auth/logout` | Revoke refresh token | JWT |

**POST `/auth/magic-link`**
```json
{
  "email": "martin@gmail.com",
  "purpose": "login" | "verify_email" | "reverify"
}
```
Returns:
```json
{
  "request_id": "uuid-for-polling",
  "claim_secret": "random-64-char-hex",
  "message": "Check your email",
  "expires_in": 900
}
```
- Rate limited: 5/hr per email, 30/hr per IP
- Exponential backoff on successive requests from same email

**GET `/auth/magic-link/verify?token=abc123`**

Landing page served when user clicks the magic link in their email. The email link points here (GET), not directly to `/consume` (POST). This is necessary because:
1. Email links are always GET requests
2. Email security scanners (Microsoft Defender, Proofpoint) pre-fetch GET links but don't execute JavaScript — so the token survives scanning
3. GET with side effects violates HTTP semantics; the actual mutation happens via POST

Returns an HTML page that auto-submits a POST to `/consume`:

```html
<body>
  <p>Connecting your account...</p>
  <form id="f" method="POST" action="/api/v1/auth/magic-link/consume">
    <input type="hidden" name="token" value="abc123">
    <noscript>
      <p>JavaScript is disabled. Click below to continue:</p>
      <button type="submit">Connect my account</button>
    </noscript>
  </form>
  <script>document.getElementById('f').submit()</script>
</body>
```

- JS enabled (99%): auto-submits immediately, user sees "Connected" page
- JS disabled: user sees a "Connect my account" button, clicks to submit manually

**POST `/auth/magic-link/consume`**

Browser-side endpoint. Receives the form POST from the `/verify` landing page. The server:
1. Validates the token hash
2. Sets `used_at` on the `MagicLinkToken` (marks as authenticated)
3. Creates `CloudUser` if new
4. Returns an HTML page: "You're connected! You can close this tab."

**No tokens are issued here.** Authentication is recorded; token minting is deferred to `/claim`.

Request body (form-encoded from the landing page):
```
token=abc123
```
```

**GET `/auth/magic-link/claim/{request_id}`**

Desktop app polling + token claim endpoint. Requires `X-Claim-Secret` header on every call.

```
GET /auth/magic-link/claim/{request_id}
X-Claim-Secret: {claim_secret}
```

Two-factor auth: `request_id` identifies the request, `claim_secret` proves you're the original requester. The secret is only known to the desktop app that initiated the magic link — it never appears in the email or browser. If either value is wrong, returns `403`.

Responsibilities split cleanly:
- `/consume` **authenticates** (validates the magic link)
- `/claim` **issues** (mints and delivers tokens to the waiting desktop client)

Returns (not yet authenticated):
```json
{
  "status": "pending"
}
```

Returns (authenticated, first claim — tokens minted on-demand):
```json
{
  "status": "completed",
  "access_token": "eyJ...",
  "refresh_token": "def456...",
  "expires_in": 3600
}
```

Returns (already claimed — prevents replay):
```json
{
  "status": "consumed"
}
```

Returns (expired — 15 minutes elapsed, no click):
```json
{
  "status": "expired"
}
```

- Tokens are minted **on-demand** at the moment of first claim. Never stored in the DB.
- `claimed_at` is set on first successful claim. Subsequent calls return `consumed`.
- Client polls every 3 seconds. Stops after 15 minutes (matching magic link expiry).
```

### 2.2 User / Emails

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| GET | `/me` | Get current user profile + verified emails | JWT |
| POST | `/me/emails` | Add a new email (triggers verification magic link) | JWT |
| DELETE | `/me/emails/{email_id}` | Remove a verified email (auto-revokes linked org memberships) | JWT |

**DELETE `/me/emails/{email_id}`**

Removing a verified email automatically revokes membership in any org whose domain matches that email. This is a cascading action:

1. Delete the `VerifiedEmail` record
2. Find all `OrgMembership` rows where `org.domain` matches the email's domain and `user_id` is the current user
3. Set `status = 'revoked'` and `revoked_at = now()` on those memberships
4. If the user was the only admin of an affected org, the system should warn before proceeding (confirmation required)

Returns:
```json
{
  "removed_email": "martin@acme.com",
  "revoked_memberships": [
    {"org_id": "uuid", "org_name": "Acme Corp"}
  ]
}
```

### 2.3 Orgs

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| POST | `/orgs` | Create an org (requires verified email on that domain) | JWT |
| GET | `/orgs` | List orgs the user belongs to | JWT |
| GET | `/orgs/{org_id}` | Get org details | JWT + active membership |
| POST | `/orgs/{org_id}/join` | Join or request to join org | JWT |
| GET | `/orgs/{org_id}/members` | List org members | JWT + active membership |
| GET | `/orgs/{org_id}/members/pending` | List pending join requests | JWT + admin |
| PATCH | `/orgs/{org_id}/members/{user_id}` | Update member role, status, or approve/deny | JWT + admin |
| DELETE | `/orgs/{org_id}/members/{user_id}` | Revoke membership | JWT + admin (or self) |

**POST `/orgs`**
```json
{
  "name": "Acme Corp",
  "domain": "acme.com"
}
```
- Validates caller has a verified email on `acme.com`
- Rate limited: 3 org creations per user per month

**POST `/orgs/{org_id}/join`**

Requires a verified email on the org's domain.

Behavior depends on org's `join_policy`:

| `join_policy` | Result | OrgMembership status |
|---------------|--------|---------------------|
| `auto` (default) | Immediate join. Admin notified. | `active` |
| `approval_required` (paid plan) | Join request created. Admin notified. | `pending` |

Returns:
```json
{
  "membership_id": "uuid",
  "status": "active",
  "message": "You've joined Acme Corp"
}
```
or:
```json
{
  "membership_id": "uuid",
  "status": "pending",
  "message": "Your request to join Acme Corp has been sent to the admin for approval"
}
```

**PATCH `/orgs/{org_id}/members/{user_id}`** (admin)

Approve or deny pending requests, update role, or suspend:
```json
{
  "status": "active"
}
```
Valid transitions: `pending → active` (approve), `pending → revoked` (deny), `active → suspended`, `suspended → active`.

### 2.4 Shared Organizations (External)

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| GET | `/orgs/{org_id}/directory` | List all shared orgs + contact counts | JWT + active membership |
| POST | `/orgs/{org_id}/directory` | Create a shared (external) org | JWT + active membership |
| GET | `/orgs/{org_id}/directory/{shared_org_id}` | Get shared org details + contacts | JWT + active membership |
| PATCH | `/orgs/{org_id}/directory/{shared_org_id}` | Update shared org metadata | JWT + active membership |

### 2.5 Shared Contacts

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| GET | `/orgs/{org_id}/contacts` | List all shared contacts (pagination deferred — see post-v0.1) | JWT + active membership |
| POST | `/orgs/{org_id}/contacts` | Contribute a contact (optionally with initial org link) | JWT + active membership |
| GET | `/orgs/{org_id}/contacts/{contact_id}` | Get contact detail + all org links (logs view) | JWT + active membership |
| PATCH | `/orgs/{org_id}/contacts/{contact_id}` | Update contact info (name, telegram_handle) | JWT + active membership |
| DELETE | `/orgs/{org_id}/contacts/{contact_id}` | Soft-delete contact | JWT + admin |
| GET | `/orgs/{org_id}/contacts:export` | Bulk export (CSV/JSON) | JWT + admin only |

**POST `/orgs/{org_id}/contacts`**

Creates the contact. Optionally creates an initial org link in the same request.

```json
{
  "name": "Alice Chen",
  "telegram_handle": "alicechen",
  "telegram_id": 123456789,
  "initial_link": {
    "shared_org_id": "uuid-of-partner-co",
    "position": "VP Sales",
    "relationship_context": "client"
  }
}
```

- `initial_link` is optional. Contact can exist without any org link (e.g., freelancer).
- If `telegram_id` matches an existing contact in this org, returns `409 Conflict` with the existing contact's ID.

**GET `/orgs/{org_id}/contacts/{contact_id}`** — returns contact + all org links. Triggers `ContactViewLog` insert.

### 2.6 Shared Contact Org Links

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| GET | `/orgs/{org_id}/contacts/{contact_id}/links` | List all org links for a contact | JWT + active membership |
| POST | `/orgs/{org_id}/contacts/{contact_id}/links` | Add a new org link | JWT + active membership |
| PATCH | `/orgs/{org_id}/contacts/{contact_id}/links/{link_id}` | Update link (position, relationship_context) | JWT + active membership |
| DELETE | `/orgs/{org_id}/contacts/{contact_id}/links/{link_id}` | Soft-delete a link | JWT + active membership |

**POST `/orgs/{org_id}/contacts/{contact_id}/links`**
```json
{
  "shared_org_id": "uuid-of-vendor-inc",
  "position": "Board Member",
  "relationship_context": "partner"
}
```

- Returns `409 Conflict` if a link to this shared org already exists for this contact.

### 2.7 Group Chat Metadata

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| POST | `/orgs/{org_id}/contacts/{contact_id}/group-chats` | Link a group chat to a contact | JWT + active membership |
| GET | `/orgs/{org_id}/contacts/{contact_id}/group-chats` | List group chats a contact is in | JWT + active membership |
| DELETE | `/orgs/{org_id}/contacts/{contact_id}/group-chats/{id}` | Unlink | JWT + active membership |

### 2.8 Personal Backup

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| PUT | `/me/backup` | Upload encrypted personal contact backup | JWT |
| GET | `/me/backup` | Download encrypted backup | JWT |
| DELETE | `/me/backup` | Delete backup | JWT |

**PUT `/me/backup`**
```json
{
  "encrypted_payload": "<base64-encoded ciphertext>"
}
```

Server stores as-is. Cannot decrypt.

### 2.9 Audit

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| GET | `/orgs/{org_id}/audit/views` | View log for shared contacts | JWT + admin |

---

## 3. Authentication Flow

### 3.1 Link Local App to Cloud (First-Time Setup)

```
Desktop App                     Cloud Server                    Email / Browser
  │                               │                              │
  │  User clicks "Connect to      │                              │
  │  Cloud" in Settings            │                              │
  │                               │                              │
  ├─ POST /auth/magic-link ──────►│                              │
  │  {email, purpose: "login"}    ├─ generate token + request_id │
  │                               │  + claim_secret              │
  │                               ├─ send magic link ───────────►│ (email)
  │  ◄── 200 {request_id,        │  (link has token, NOT secret) │
  │       claim_secret,           │                              │
  │       expires_in: 900}        │                              │
  │                               │                              │
  │  Hold claim_secret in memory  │                              │
  │  UI: "Check your email.       │                              │
  │  Waiting for confirmation..." │                              │
  │  [progress indicator]         │                              │
  │                               │                              │
  ├─ GET /claim/{request_id} ────►│                              │
  │  X-Claim-Secret: {secret}     │                              │
  │  ◄── {status: "pending"}     │                              │
  │  ... (poll every 3s) ...      │                              │
  │                               │                              │
  │                               │  ◄── user clicks link ──────┤ (browser)
  │                               ├─ GET /verify?token=...       │
  │                               ├─ return landing page ───────►│ (browser)
  │                               │                              │ (JS auto-submits)
  │                               │  ◄── POST /consume {token} ─┤ (browser)
  │                               ├─ validate token              │
  │                               ├─ set used_at (authenticated) │
  │                               ├─ create CloudUser if new     │
  │                               ├─ return HTML: "Connected!   │
  │                               │  You can close this tab." ──►│ (browser)
  │                               │                              │
  ├─ GET /claim/{request_id} ────►│                              │
  │  X-Claim-Secret: {secret}     ├─ verify secret hash          │
  │                               ├─ sees used_at set,           │
  │                               │  claimed_at null             │
  │                               ├─ mint JWT + refresh token    │
  │                               ├─ set claimed_at              │
  │  ◄── {status: "completed",   │                              │
  │       access_token,           │                              │
  │       refresh_token}          │                              │
  │                               │                              │
  │  Store tokens in Fernet vault │                              │
  │  UI: "Connected as            │                              │
  │  martin@gmail.com"            │                              │
```

**Timeout handling:**
- Client polls for up to 15 minutes (matching magic link expiry)
- If no click within 15 minutes, server returns `{status: "expired"}`
- App shows: "Link expired. The magic link was not clicked in time. Please try again."
- User can retry immediately (subject to rate limits)

**Error states:**
- Network error during polling → "Connection lost. Retrying..." (auto-retry with backoff)
- Rate limited → "Too many attempts. Please wait X minutes before trying again."

### 3.2 Login (Returning User, App Relaunch)

```
Desktop App                     Cloud Server
  │                               │
  │  App launch → user enters     │
  │  master password → vault      │
  │  unlocked → read stored       │
  │  refresh token                │
  │                               │
  ├─ POST /auth/refresh ─────────►│
  │  {refresh_token}              ├─ validate, rotate token
  │  ◄── 200 {access_token,      │
  │       new_refresh_token}      │
  │                               │
  │  Update refresh token in vault│
  │  Cloud features available     │
```

If refresh token is expired (app closed > 30 days):
- App shows: "Cloud session expired. Please reconnect."
- Falls back to the full magic link flow (3.1)

### 3.3 Add Business Email

```
Desktop App                     Cloud Server                    Email / Browser
  │                               │                              │
  ├─ POST /me/emails ────────────►│                              │
  │  {email: "me@acme.com"}       ├─ check domain not blocked    │
  │                               ├─ generate token + request_id │
  │                               │  + claim_secret              │
  │                               ├─ send verification link ────►│ (email)
  │  ◄── 202 {request_id,        │                              │
  │       claim_secret,           │                              │
  │       message: "pending"}     │                              │
  │                               │                              │
  │  Hold claim_secret in memory  │                              │
  │  UI: "Verification email sent │                              │
  │  to me@acme.com. Waiting..."  │                              │
  │                               │                              │
  │                               │  ◄── user clicks link ──────┤ (browser)
  │                               ├─ GET /verify?token=...       │
  │                               ├─ return landing page ───────►│ (browser)
  │                               │                              │ (JS auto-submits)
  │                               │  ◄── POST /consume {token} ─┤ (browser)
  │                               ├─ validate token              │
  │                               ├─ set used_at                 │
  │                               ├─ create VerifiedEmail record │
  │                               ├─ return HTML: "Email         │
  │                               │  verified!" ────────────────►│ (browser)
  │                               │                              │
  ├─ GET /claim/{request_id} ────►│                              │
  │  X-Claim-Secret: {secret}     │                              │
  │  ◄── {status: "completed"}   │  (no tokens for verify flow) │
  │                               │                              │
  │  UI: "me@acme.com verified.  │                              │
  │  You can now join or create   │                              │
  │  orgs on acme.com."           │                              │
```

Same 15-minute timeout + retry messaging as 3.1.

### 3.4 Unlink Cloud Account

1. Client calls `POST /auth/logout` → server revokes refresh token
2. Client deletes cloud credentials from Fernet vault
3. Personal backup remains on cloud until explicitly deleted via `DELETE /me/backup`
4. UI returns to "Connect to Cloud" state

### 3.5 JWT Structure

```json
{
  "sub": "user-uuid",
  "email": "martin@gmail.com",
  "iat": 1711100000,
  "exp": 1711103600
}
```

- Access token: 1 hour expiry
- Refresh token: 30 days, single-use with rotation
- Signed with RS256 (asymmetric — allows public key verification by other services later)

---

## 4. Access Control

### 4.1 Middleware

Every request to `/orgs/{org_id}/*` passes through membership check middleware:

```python
async def require_active_membership(org_id: UUID, user: CloudUser):
    membership = await get_membership(user.id, org_id)
    if not membership or membership.status != 'active':
        raise HTTPException(403, "Not an active member of this organization")
    return membership
```

### 4.2 Admin-Only Operations

Checked via `membership.role == 'admin'`:

- Revoke/suspend members
- Bulk export contacts
- View audit logs
- Delete shared contacts

### 4.3 Row-Level Filtering

All queries to `SharedContact` and `SharedOrganization` include:

```sql
WHERE org_id IN (
    SELECT org_id FROM org_membership
    WHERE user_id = :current_user AND status = 'active'
)
```

---

## 5. Rate Limiting

Implemented via Redis sliding window counters.

| Scope | Limit | Window |
|-------|-------|--------|
| Magic link per email | 5 | 1 hour |
| Magic link per email | 20 | 24 hours |
| Magic link per IP | 30 | 1 hour |
| Org creation per user | 3 | 30 days |
| API requests per user | 1000 | 1 hour |

Exponential backoff on successive magic link requests from the same email: 0s, 5s, 30s, 2min, 10min.

---

## 6. Encryption (Personal Backup)

### 6.1 Key Derivation

The backup encryption key is derived from the same master password that unlocks the local Fernet vault. No separate passphrase needed.

```
Master password
  → PBKDF2 (100k iterations, SHA-256, salt: "mingly-backup-v1" + user_id)
  → 256-bit backup key
```

- The salt includes `user_id` so two users with the same master password produce different keys
- The derived key is cached in the Fernet vault for convenience (avoids re-deriving on every backup sync)
- On recovery (new device): user enters master password → key is re-derived → backup is decrypted. No extra secrets needed.

### 6.2 Client-Side Encryption

```
Backup key (derived from master password)
  → AES-256-GCM encrypt(contact_data_json) → ciphertext
  → base64(ciphertext) → upload to server
```

### 6.3 Server Responsibilities

- Store `encrypted_payload` as opaque blob
- Never attempt decryption
- Serve ciphertext on download; client decrypts locally

### 6.4 Key Loss

If the user forgets their master password, the backup is unrecoverable. User is warned at setup: "Your cloud backup is encrypted with your master password. If you lose it, your backup cannot be recovered."

### 6.5 Master Password Change

When the user changes their master password:
1. Re-derive the backup key from the new master password
2. Decrypt the existing backup with the old key
3. Re-encrypt with the new key
4. Upload the re-encrypted backup to the cloud

---

## 7. Local Client Integration

### 7.1 Token Storage

Cloud credentials are stored in the existing Fernet vault (`vault.enc`) alongside Telegram credentials:

```python
# Vault structure (after cloud linking)
{
    "telegram_api_id": "...",
    "telegram_api_hash": "...",
    "telegram_phone": "...",
    # Cloud credentials
    "cloud_access_token": "eyJ...",
    "cloud_refresh_token": "def456...",
    "cloud_email": "martin@gmail.com",
    "cloud_user_id": "uuid"
}
```

- Requires master password to unlock → cloud features unavailable until vault is open
- On token refresh, updated tokens are written back to vault immediately

### 7.2 Session Lifecycle

| Scenario | Behavior |
|----------|----------|
| App launch, vault unlocked | Read refresh token from vault → `POST /auth/refresh` → get new JWT. Cloud ready. |
| JWT expires (1hr) while app running | Silent refresh via refresh token. Transparent to user. |
| App closed < 30 days | Refresh token still valid. Silent refresh on next launch. |
| App closed > 30 days | Refresh token expired. Show "Cloud session expired. Please reconnect." Re-run magic link flow. |
| Cloud server unreachable | Local features work normally. Cloud features show "Offline — will sync when connected." |
| User changes master password | Vault re-encrypted with new key. Cloud tokens survive (they're inside the vault). |
| Unlink | Revoke refresh token server-side. Delete cloud fields from vault. |

### 7.3 New Settings in Mingly Desktop

- **Cloud Account**: link/unlink cloud account, manage verified emails
- **Personal Backup**: enable/disable encrypted backup sync (key derived from master password)
- **Org Membership**: view orgs, contribute contacts, browse shared pool

### 7.4 Contact Contribution Flow (Local → Cloud)

1. User opens a local contact → "Share to Org" button
2. Selects target org (if multi-org)
3. Shares contact info: name, TG handle
4. Client calls `POST /orgs/{org_id}/contacts` — creates the contact (or gets `409` if already exists)
5. Optionally links to an external org: selects existing SharedOrganization or creates one, sets position + relationship context
6. Client calls `POST /orgs/{org_id}/contacts/{contact_id}/links`
7. Local contact is tagged with `cloud_shared_to: [org_id]` for tracking
8. Later, user can add more org links to the same contact (e.g., Alice moves companies, or sits on multiple boards)

### 7.5 Backup Sync Flow

1. On enable: client derives backup key from master password → encrypts all personal contacts → uploads
2. Periodic: client re-encrypts and uploads on contact changes (debounced, e.g., every 30 min if dirty)
3. On recovery: user logs in with primary email → downloads encrypted blob → enters master password → derive key → decrypts → restores contacts locally

---

## 8. Project Structure (Cloud Service)

```
cloud/
  api/
    auth.py          # Magic link, JWT, refresh
    users.py         # Profile, verified emails
    orgs.py          # Org CRUD, membership
    directory.py     # SharedOrganization CRUD
    contacts.py      # SharedContact CRUD, view logging
    contact_links.py # SharedContactOrgLink CRUD
    group_chats.py   # Group chat metadata linking
    backup.py        # Personal backup upload/download
    audit.py         # View logs (admin)
  middleware/
    auth.py          # JWT validation
    membership.py    # Org membership check
    rate_limit.py    # Redis-based rate limiting
  db/
    models.py        # SQLAlchemy ORM models
    migrations/      # Alembic migrations
  services/
    email.py         # Magic link email sending
    encryption.py    # Token hashing utilities (server-side)
  config.py          # Environment-based configuration
  main.py            # FastAPI app setup
```
