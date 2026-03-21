# Mingly Cloud CRM Service — Design Plan

## Purpose

An optional cloud service that adds enterprise CRM capabilities to Mingly's local Telegram client. Enables organizations to share business contacts, maintain visibility into business relationships, and provide contact backup against TG account compromise — while strictly preserving the personal/business boundary.

---

## 1. Identity & Authentication

### Multi-Email Identity

Users authenticate with a primary email (personal) and can verify additional business emails to prove org affiliations.

```
CloudUser
  - primary_email (login, personal — e.g., martin@gmail.com)
  - verified_emails[] (business — e.g., martin@acme.com, martin@sidegig.io)
```

- Primary email is permanent and used for login + personal contact recovery
- Business emails are verified via confirmation link
- Each verified business email domain unlocks the ability to create or join an org tied to that domain

### Public Email Provider Blocklist

Domains like `gmail.com`, `yahoo.com`, `outlook.com`, etc. are blocklisted from org creation. Orgs can only form around private/corporate domains.

---

## 2. Organization Management

### Bottom-Up Org Creation (Claim-and-Approve Model)

- Any user with a verified `@acme.com` email can create the Acme org and becomes its first admin
- Other users with verified `@acme.com` emails can join, or request co-admin status
- Disputes fall back to domain verification (DNS TXT record) as the ultimate authority

### Multi-Org Membership

A user can belong to multiple orgs simultaneously, one per verified business email:

```
martin@gmail.com (primary, personal)
├── martin@acme.com    → Acme Corp (member)
├── martin@sidegig.io  → SideGig LLC (admin)
└── personal contacts  → always accessible
```

### Email Re-Verification

Business email access can lapse (employee leaves company). Options (choose one):

- **Active:** Re-verify every 90 days via confirmation link. Failure suspends membership.
- **Passive:** Admin manually revokes departed members. Simpler, relies on admin hygiene.
- **Hybrid (recommended):** Admin gets notified if a member hasn't re-verified in X days. Admin decides whether to revoke.

---

## 3. Personal / Business Boundary

### Core Principle

> "Your contacts are yours. The org has a shared directory. You choose what goes in."

### Separate Shared Pool (Push Model)

- All local contacts default to **personal** — never synced to any org
- Users explicitly **contribute** contacts into an org's shared pool
- Contributing copies select fields (name, TG handle, position, relationship context) to the cloud
- The local contact remains yours; the shared copy belongs to the org
- No automatic classification — the user always decides

### Personal Backup Layer

Separate from any org, each user has an encrypted personal contact backup:

```
Cloud Account
├── Personal Contact Backup (encrypted, yours forever)
├── Org: Acme Corp (active membership)
│   └── access to Acme's shared contact pool
├── Org: SideGig LLC (active membership)
│   └── access to SideGig's shared contact pool
└── Org: FormerJob Inc (revoked — 2026-01-15)
    └── access: NONE
```

---

## 4. Shared Contact Pool Structure

### External Orgs as First-Class Entities

The shared pool is structured — contacts nest under external organizations. Any org member can create an external org entry bottom-up.

```
Org (Acme — your company)
├── SharedOrganization (PartnerCo)
│   ├── SharedContact: Alice (VP Sales, contributed by Martin)
│   └── SharedContact: Bob (Engineer, contributed by Sarah)
├── SharedOrganization (VendorInc)
│   └── SharedContact: Carol (Account Manager, contributed by Martin)
└── Unaffiliated
    └── SharedContact: Dave (Freelancer, contributed by James)
```

- When contributing a contact, user specifies (or creates) the external org
- Multiple members can contribute contacts to the same external org
- Dedup/merge logic needed for org name normalization ("PartnerCo" vs "Partner Co.")
- Org-level metadata supported (e.g., "PartnerCo is our biggest client in APAC")

---

## 5. Departure & Access Revocation

### Rules

1. Leaving an org **revokes access** to that org's shared contact pool
2. Contacts you contributed **stay in the org pool** (you gave them to the company's CRM — they're theirs now)
3. Your **local copies** of contacts you contributed remain yours
4. Your **personal backup** is always accessible via primary email
5. You **cannot** see notes, context, or contacts added by others after departure

### Recovery After TG Hack (Post-Departure)

- Login with primary email → access personal backup → recover personal contacts
- Active org memberships → access those shared pools
- Former org memberships → no access, no data

---

## 6. Data Model (Cloud)

```
CloudUser
  - id
  - primary_email
  - created_at

VerifiedEmail
  - id
  - user → CloudUser
  - email
  - domain (extracted, indexed)
  - verified_at
  - last_reverified_at

Org
  - id
  - name
  - domain (unique — one org per domain)
  - created_at
  - created_by → CloudUser

OrgMembership
  - id
  - user → CloudUser
  - org → Org
  - role: admin | member
  - status: active | suspended | revoked
  - joined_at
  - revoked_at (nullable)

SharedOrganization
  - id
  - org → Org (parent — the user's company)
  - name (external org name)
  - normalized_name (for dedup)
  - metadata (JSON — relationship type, region, notes)
  - created_by → CloudUser
  - created_at

SharedContact
  - id
  - org → Org (parent — the user's company)
  - shared_organization → SharedOrganization (nullable — for unaffiliated contacts)
  - telegram_handle
  - name
  - position
  - relationship_context (client, vendor, partner, prospect, etc.)
  - contributed_by → CloudUser
  - contributed_at

PersonalContactBackup
  - id
  - user → CloudUser
  - encrypted_payload (contact data, encrypted with user's key)
  - updated_at
```

### Access Control

Every query to `SharedContact` or `SharedOrganization` filters through:

```
OrgMembership WHERE user = current_user AND org = target_org AND status = 'active'
```

Revoked/suspended = invisible, not deleted.

---

## 7. API & Auth

### REST + Magic Link + JWT

- **API style**: REST. Consistent with the existing local FastAPI backend. Clear resource-based routes. Custom endpoints (e.g., `/orgs/{id}/full-directory`) for nested fetches where needed.
- **Auth**: Magic Link + JWT.
  - User enters email → receives a login link → clicks it → server issues a JWT
  - No passwords to store. Email verification is inherent in the login flow.
  - Re-verification (90-day org membership check) reuses the same magic link mechanism
  - JWT for stateless session management; refresh token rotation for long sessions
  - OAuth ("Sign in with Google/Microsoft") as a future add-on, not required for v1

---

## 8. Decisions

### Encryption Model — User-Held (Zero-Knowledge)

Personal backup is encrypted client-side before upload. The server never sees plaintext contact data. User holds the key (derived from a passphrase or stored locally). If the user loses the key, the backup is unrecoverable — this is the tradeoff for true privacy.

### Chat Metadata Sharing — Group Chats Only

The org shared pool may include context from **group chats only**. No personal DM content or metadata is ever shared with the org. This preserves the personal/business boundary — group chats are inherently shared spaces; DMs are not.

### Audit Trail — View Logging, No Bulk Export

- No bulk export of shared contacts unless performed by an **org admin**
- When a user views a specific shared contact's page, that view is logged (who, when)
- Provides accountability without surveillance overhead

### Sync Conflict Resolution — Deferred

When two members update the same shared contact's info, a conflict resolution mechanism is needed (analogous to merging a PR — review and pick). Design deferred to implementation phase.

### Merge/Dedup UX — Deferred

Same class of problem as sync conflict resolution (two members contribute the same contact or create the same external org under slightly different names). Will be addressed together with conflict resolution. Likely involves fuzzy matching on org names + admin-assisted merge.

### Billing Model — Deferred

To be determined. Options include free tier, per-seat, per-org, or usage-based.

---

## 9. Resolved Questions

- **Key recovery**: Lost key = lost backup. No escrow for v1. Users are warned at setup.
- **Group chat context granularity**: Membership/participants only. No topic or summary content.
- **Mobile client**: No plans currently. REST API is client-agnostic if this changes.

### Rate Limiting & Abuse Prevention

**Magic links:**
- Per-email rate limit: 5 requests/hour, 20/day
- Per-IP rate limit: 30 requests/hour
- Exponential backoff on successive requests from the same email

**Org creation:**
- Already gated by verified business email on a private domain (public email providers blocklisted)
- One org per domain (enforced at DB level)
- Per-user cooldown: max 3 org creations per month

**v2 (if abuse materializes):**
- CAPTCHA escalation after repeated magic link attempts
- Domain age/reputation check (reject domains < 30 days old via WHOIS)