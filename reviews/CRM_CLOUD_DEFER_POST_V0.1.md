# Mingly Cloud CRM — Deferred Decisions & Features (Post v0.1)

Items explicitly deferred from v0.1 scope. Each entry includes context on why it was deferred and what triggers revisiting it.

---

## 1. Sync Conflict Resolution

**Problem**: Two org members update the same `SharedContact` concurrently (e.g., one updates the position, another updates the relationship context).

**Why deferred**: Requires UX design for review/merge workflow. Low probability in early usage with small orgs.

**Trigger to revisit**: When orgs have 5+ active members contributing to the same shared contacts.

**Likely approach**: Last-write-wins for independent fields (position vs. relationship_context). For same-field conflicts, queue a merge request visible to both editors + org admin — similar to a PR review. Field-level timestamps to detect conflicts.

---

## 2. Merge / Dedup UX

**Problem**: Two members contribute the same contact independently, or create `SharedOrganization` entries with slightly different names ("PartnerCo" vs "Partner Co." vs "Partner Company").

**Why deferred**: Same class of problem as conflict resolution. Needs fuzzy matching logic + admin-assisted merge UI.

**Trigger to revisit**: When shared contact pool exceeds ~50 entries per org, duplicates become noticeable.

**Likely approach**:
- `normalized_name` on `SharedOrganization` catches exact dupes at creation time
- Periodic background job flags near-matches (Levenshtein distance, token overlap)
- Admin gets a "merge suggestions" queue: review, confirm, or dismiss
- Merged records retain attribution from both contributors

---

## 3. Billing Model

**Problem**: How to monetize the cloud service.

**Why deferred**: Need to validate product-market fit before pricing decisions. Premature billing adds friction to adoption.

**Trigger to revisit**: When there are 10+ active orgs or clear demand signals.

**Options on the table**:
- Free tier (1 org, N contacts) + paid per-seat
- Per-org flat fee with seat tiers
- Usage-based (contacts stored, API calls)
- Free for personal backup, paid for org features

---

## 4. OAuth Provider Integration

**Problem**: Users on Google Workspace or Microsoft 365 expect "Sign in with Google/Microsoft" rather than magic links.

**Why deferred**: Magic link covers all email providers. OAuth adds implementation complexity (provider registration, callback handling, token management per provider).

**Trigger to revisit**: User feedback requesting it, or enterprise customers requiring SSO/SAML.

**Likely approach**: Add as an alternative login method alongside magic links. Map OAuth-verified email to `VerifiedEmail` record automatically.

---

## 5. Domain Verification (DNS TXT) & Admin Challenge Mechanism

**Problem**: In the claim-and-approve model, the first person with an `@acme.com` email becomes org admin. A rogue employee could claim the org before IT does. Other members with verified domain emails have no way to challenge the admin or claim admin rights.

**Why deferred**: Low risk at small scale. v0.1 has auto-approve with admin notification — the admin is always aware of who joins but there's no mechanism for members to dispute admin authority.

**Trigger to revisit**: Enterprise customers requesting guaranteed admin authority, or a dispute between org members about who should be admin.

**Likely approach**: Two-tier resolution:
1. **Peer challenge**: A member requests admin role via the API. Current admin is notified and can approve or deny. If admin is unresponsive for N days, escalate.
2. **DNS TXT verification**: Ultimate authority. Admin (or challenger) initiates DNS TXT verification (add a record like `mingly-verify=abc123`). Verified domain ownership overrides peer-elected admin status. Unlocks advanced features (enforce re-verification policy, mandatory audit, etc.).

---

## 6. Email Re-Verification Policy

**Problem**: Which re-verification model to enforce — active (90-day), passive (admin-managed), or hybrid.

**Why deferred**: Depends on org size and admin engagement patterns. Premature to enforce a policy before observing real usage.

**Trigger to revisit**: First reported case of a departed employee retaining access.

**Current default**: Passive (admin manually revokes). Hybrid notification system is the likely v1.1 addition.

---

## 7. CAPTCHA Escalation

**Problem**: Rate limiting alone may not stop sophisticated abuse of magic link or org creation endpoints.

**Why deferred**: No abuse data yet. Adding CAPTCHA prematurely hurts UX.

**Trigger to revisit**: Observed abuse patterns that bypass rate limits.

---

## 8. Domain Age / Reputation Check

**Problem**: Prevent throwaway domains (registered hours ago) from creating orgs.

**Why deferred**: Requires external WHOIS/reputation API dependency. Over-engineering for v0.1.

**Trigger to revisit**: Observed spam org creation from disposable domains.

**Likely approach**: WHOIS lookup on org creation; reject domains < 30 days old. Or integrate a service like Cloudflare Radar / Domain reputation API.

---

## 9. Mobile Client

**Problem**: If Mingly adds a mobile app, does the cloud API need changes?

**Why deferred**: No mobile plans currently.

**Note**: The REST API is client-agnostic by design. Main considerations for mobile would be: push notifications for magic links, offline sync for shared contacts, bandwidth-efficient backup sync (delta uploads instead of full replace).

---

## 10. Advanced Audit Features

**Problem**: v0.1 logs contact views only. Orgs may want richer audit: who edited what, export history, membership change log.

**Why deferred**: View logging covers the primary accountability use case. Richer audit adds storage and complexity.

**Trigger to revisit**: Enterprise/compliance customer requests.

**Likely approach**: Event sourcing pattern — append-only `AuditEvent` table with actor, action, resource, timestamp, diff payload.

---

## 11. Shared Contact Notes / Activity

**Problem**: CRM value increases if org members can attach notes to shared contacts ("Met at conference", "Interested in product X", "Follow up Q2").

**Why deferred**: Not in v0.1 scope. Contact contribution is the baseline; annotation is a layer on top.

**Trigger to revisit**: Once shared contact pool is actively used and members want to collaborate on relationship context beyond the initial contribution.

**Likely approach**: `SharedContactNote` table (contact_id, author, content, created_at). Visible to all active org members. Deleted on membership revocation? Or retained as org property? — needs its own design decision.

---

## 12. Cloud Web Dashboard & Pagination

**Problem**: The v0.1 API has list endpoints (`GET /orgs/{org_id}/contacts`, `GET /orgs/{org_id}/directory`, `GET /orgs/{org_id}/audit/views`) but no pagination spec. The desktop client doesn't need to browse the full shared pool — it only contributes contacts and views individual records. Pagination becomes necessary when a web dashboard is built for browsing, searching, and managing the org's shared pool.

**Why deferred**: No web dashboard in v0.1. The desktop client doesn't paginate. Designing pagination without a consumer is premature.

**Trigger to revisit**: When a cloud web dashboard is planned.

**Likely approach**: Cursor-based pagination (`?cursor=eyJ...&limit=20`). Stable under concurrent writes (multiple org members editing), no offset-scan performance issues. Default page size 20, max 100. Cursor-based preferred over offset because CRM contact lists grow and get modified concurrently — offset pagination skips/duplicates items when data changes between pages.

---

## 13. ContactViewLog Retention Policy

**Problem**: `ContactViewLog` is append-only and grows unbounded. For a 10-member org viewing 20 contacts/day, that's ~73k rows/year. For a 100-member org, ~730k rows/year. Manageable for Postgres but grows forever with no cleanup.

**Why deferred**: At v0.1 scale (small orgs, few members), growth is negligible. Not worth adding a retention job before there's data to warrant it.

**Trigger to revisit**: When any single org's view log exceeds ~500k rows, or when storage costs become noticeable.

**Likely approach**: Configurable retention window (default 12 months). A scheduled job deletes rows older than the window. Optionally aggregate old logs before deletion (e.g., roll up into monthly summary: "Martin viewed 47 contacts in January 2027") for long-term audit without row-level detail.
