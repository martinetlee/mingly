# Shield Cloud Contract v2 (channel-neutral uploads)

Status: client implemented (`backend/scam/cloud_sync.py`, `backend/scam/client.py`,
`backend/scam/channel_ids.py`); server to do. v1 is
`../../../SHIELD_CLOUD_API_CONTRACT.md` (endpoints, auth, batching, errors and sync
cadence there still apply unless this document changes them).

v1 identifies everything by Telegram id (`telegram_id`, `chat_id`, `tg_message_id`),
so since the multi-channel refactor the client uploads Telegram rows only; Gmail,
Signal and SMS flags, scans, reports, dismissals and feedback stay local. v2 carries
every channel without sending private handles (email addresses, phone numbers,
Signal accounts) in clear.

## 1. Negotiation

### Discovery

```
GET <base>/sync/capabilities
Authorization: Bearer <api_key>

200 {"contract_versions": [1, 2]}
```

- The client uses v2 only when `2` is in `contract_versions`. Otherwise it uses v1.
- `404` / `405` / `501` means a v1 server. The answer is cached for 1 hour.
- Any other failure (5xx, 401, network error, bad JSON) means v1 for now. The
  client asks again after 5 minutes.
- `GET /sync/manifest` may also carry `"contract_versions": [1, 2]` at the top
  level. The client reads it on every sync, refreshes its cache from it and then
  skips the capabilities call. A server that adds the field to the manifest does
  not need the separate endpoint, but should have both.
- The cache is per configured cloud URL. Changing the URL clears it.
- The external detection API (`scam_detection_api_url`, `isThisScam`) is
  discovered on its own, with `GET <detection base>/capabilities` and the same
  response and rules.

### Versioning

- Every v2 request has the header `X-Mingly-Contract: 2`, and every v2 body has
  the top-level field `"contract": 2`. A v1 request has neither.
- v2 uses the same paths as v1 (`/sync/flags`, `/sync/scan-results`, ...). The
  server must route on the header (or on `contract`), because the bodies differ.
- A server that advertises 2 must keep accepting v1 bodies. Clients that have not
  been updated, and clients whose discovery failed, still send v1.
- On a non-2xx response the client keeps the rows queued, exactly as in v1. If a
  server stops supporting v2, the client goes back to v1 within one TTL.

## 2. Identifiers

| Thing | Public-id channels (`telegram`) | Private channels (`gmail`, `signal`, `sms`) |
|---|---|---|
| Person handle (`sender`, `reported`, `related_sender`) | `{"id": "123456789"}` (Telegram user id, string) | `{"handle_hash": "<64 hex>"}` |
| Conversation | `{"kind": "group", "id": "-1001234567890"}` (peer id) | `{"kind": "email_thread", "ref": "<64 hex>"}` |
| Message | `{"id": "555"}` (Telegram message id) | `{"ref": "<64 hex>"}` |

An object has `id` or `ref`/`handle_hash`, never both. `null` means the row was
deleted locally or has no sender.

- `channel` is one of `telegram | gmail | signal | sms`. Treat any other value as
  opaque, because more channels will come.
- `kind` is the v2 conversation kind: `direct | group | channel | email_thread`.
  v1's `"private"` is `"direct"` in v2.
- `handle_hash` = `HMAC-SHA256(key = install_salt, msg = "<channel>:<external_id>")`,
  lowercase hex. `external_id` is trimmed and lowercased first. That external id
  is the email address for Gmail, the E.164 number for SMS, and the ACI UUID (or
  number) for Signal.
- `ref` = `HMAC-SHA256(install_salt, "ref/conversation/<internal id>")`, or
  `"ref/message/<internal id>"` for a message. It is stable for the life of the
  install's database. Use it to upsert re-uploads, the same way v1 used
  `(chat_id, tg_message_id)`.
- `install_salt` is 32 random bytes. It is created on first v2 use and stored in
  the client's encrypted vault (`shield_cloud_handle_salt`). It never leaves the
  device. A vault reset creates a new salt, so the cloud then sees a new install.

### Privacy decisions

1. **Telegram ids stay in clear.** v1 already sends them. They are what the
   known-scammer signatures and `areFlaggedScams` match on, and Telegram user and
   chat ids are platform identifiers, not contact details. Hashing them would
   break cross-install scammer correlation that already works and would protect
   nothing new.
2. **Emails, phone numbers and Signal accounts are hashed with a per-install
   salt.** These are personal contact details. The phone-number space is small
   enough to enumerate, so an unsalted hash, or a hash with a salt shared by all
   installs, would be reversible by anyone holding it, including the cloud.
   **Consequence: the cloud cannot correlate the same email or phone scammer
   across installs.** Within one install, the same handle always gives the same
   hash, so the cloud can count repeats and link a flag to its scans, reports and
   dismissals. Cross-install correlation for private channels has to come from
   content signals, such as domains and links inside the text the user allows,
   not from identities. Uploading sender email domains in clear for flagged
   scammers is possible later as a v2.x opt-in. It is not part of v2.0.
3. **Conversation and message ids of private channels are opaque refs.** A Gmail
   thread id or an SMS chat id says nothing useful to the cloud, and a Gmail
   thread id can be looked up by anyone with access to that mailbox.
4. **Message content is opt-in per channel** (section 3). Email bodies are off by
   default.

## 3. Content opt-in

The pref `shield_cloud_upload_content` (`{channel: bool}`, in prefs.json) applies
under v2. When no value is set, the defaults are:

| channel | default | why |
|---|---|---|
| telegram | on | what v1 uploads today |
| signal, sms | on | chat messages, same as Telegram |
| gmail | **off** | email bodies are long, personal and often confidential |
| any other | off | |

When a channel's content is off, the client sends `"content_included": false` and
`null` for these fields:

- flags and dismissals: `flagged_message_text`
- scan results: `pipeline_input`
- dismissed scans: `pipeline_input`, `llm_prompt`, `llm_response`. `trace` is
  cut down to its structure: `status`, `pipeline_version`, `duration_ms`, and
  `steps[]` with only `node_id`, `type`, `status`, `duration_ms`, `model_name`,
  `provider_name`, `max_tokens` and `temperature`.

Verdict metadata is always sent: score, reason, category, status, error, manual
verdict, `effective_is_scam`, dates, pipeline version and `models_used`. `reason`
is the model's one-line explanation and may paraphrase the message.

Two things do not follow the pref:

- **Reports** carry the messages the user chose to include, whatever the pref.
  Submitting a report is the consent.
- **`isThisScam`** (external detection mode) always sends the text it is asked to
  judge, because judging that text is the request. The pref governs what is
  uploaded for retention and learning. It does not govern a detection service the
  user picked explicitly.

There is no settings UI for the pref yet. The defaults apply.

## 4. Payloads

The sections below list every v1 upload with its v2 form. Batch sizes, response
codes and response bodies are the same as in v1.

### `POST /sync/flags`

```json
{
  "contract": 2,
  "flags": [{
    "channel": "gmail",
    "sender": {"handle_hash": "9f2c…"},
    "source": "local",
    "status": "active",
    "confidence": 0.92,
    "reason": "Invoice fraud pattern",
    "category": "phishing",
    "content_included": false,
    "flagged_message_text": null,
    "flagged_at": "2026-03-31T10:00:00",
    "dismissed_at": null
  }]
}
```

This replaces v1's `telegram_id`. `status` and `dismissed_at` are the fields the
v1 client already sends. A flag whose status changes is uploaded again. Upsert it
on `(install, channel, sender)`.

### `POST /sync/scan-results` (verdicts)

```json
{
  "contract": 2,
  "scan_results": [{
    "channel": "telegram",
    "conversation": {"kind": "group", "id": "-1001234567890"},
    "message": {"id": "555"},
    "sender": {"id": "123456789"},
    "scam_score": 0.92,
    "scam_reason": "Phishing link detected",
    "scam_category": "phishing",
    "scan_status": "completed",
    "scan_error": null,
    "manual_verdict": null,
    "effective_is_scam": true,
    "date": "2026-03-31T09:30:00",
    "updated_at": "2026-03-31T09:31:00",
    "pipeline_version": "3.1.0",
    "models_used": [{"node_id": "llm_judge", "model_name": "qwen2.5:7b", "provider": "ollama", "max_tokens": 200, "temperature": 0}],
    "content_included": true,
    "pipeline_input": "Hey, I need to verify your wallet..."
  }]
}
```

This replaces v1's `chat_id`, `tg_message_id` and `sender_id`. The rows selected
are the same as in v1: scam, failed, manual verdicts, and later changes to
anything already uploaded. Upsert on `(install, channel, conversation, message)`.

### `POST /sync/scan-results` (dismissed scans)

These have the same fields as above, plus `"scan_status": "dismissed"`,
`"dismissed": true`, `llm_prompt`, `llm_response` and `trace`. All of them are
subject to the content rules in section 3.

### `POST /sync/dismissals` (dismissed scammer flags)

```json
{
  "contract": 2,
  "dismissals": [{
    "channel": "sms",
    "sender": {"handle_hash": "41ab…"},
    "source": "local",
    "confidence": 0.85,
    "reason": "Suspicious message pattern",
    "category": "phishing",
    "content_included": true,
    "flagged_message_text": "Your parcel is held…",
    "flagged_at": "2026-03-31T10:00:00Z"
  }]
}
```

### `POST /sync/reports`

```json
{
  "contract": 2,
  "reports": [{
    "report_id": "uuid",
    "channel": "gmail",
    "reported": {"handle_hash": "9f2c…"},
    "conversation": {"kind": "email_thread", "ref": "c1d0…"},
    "reporter_verdict": "scam",
    "scam_category": "phishing",
    "custom_category": null,
    "messages": [
      {"position": 0, "role": "reported", "included": true, "sender": {"handle_hash": "9f2c…"},
       "sender_name": "Billing Dept", "text": "Please wire…", "date": "2026-03-31T09:00:00Z"},
      {"position": 1, "role": "reporter", "included": false, "sender": null}
    ],
    "local_detection": {"scores": [
      {"message": {"ref": "77e1…"}, "scam_score": 0.9, "scam_reason": "…", "scam_category": "phishing",
       "scam_status": "completed", "manual_verdict": null, "effective_is_scam": true}
    ]},
    "created_at": "2026-03-31T10:00:00"
  }]
}
```

Differences from v1:

- `messages` and `local_detection` are JSON values. In v1 they were JSON-encoded
  strings (`messages_json`, `local_detection_json`).
- Each message's `telegram_id` becomes `sender`, and each score's
  `tg_message_id` becomes `message`.
- `chat_type` is gone. Use `conversation.kind`.
- A message the user did not include has no `text`, `sender_name` or `date`
  (unchanged from v1).

### `POST /sync/feedback`

```json
{
  "contract": 2,
  "feedback": [{
    "id": 12,
    "channel": "gmail",
    "feedback_type": "false_positive",
    "message": "This was a real invoice",
    "related_conversation": {"kind": "email_thread", "ref": "c1d0…"},
    "related_message": {"ref": "77e1…"},
    "related_sender": {"handle_hash": "9f2c…"},
    "created_at": "2026-03-31T10:00:00"
  }]
}
```

`channel` is `null` for general feedback with no related row. `id` is the
client's local id and is unique per install only.

### `POST /sync/analytics`

The body is the same as v1 plus `"contract": 2`. The one change is
`chat_types`, which uses the v2 kinds: `{"direct": 200, "group": 900, "channel": 100}`.
In v1 the first key was `"private"`. Analytics are not broken down by channel
yet.

### `POST /isThisScam` (external detection API)

```json
{
  "contract": 2,
  "request_id": "uuid",
  "channel": "gmail",
  "conversation": {"kind": "email_thread", "name": "Invoice 4411"},
  "messages": [{"message_id": 10, "sender": {"handle_hash": "9f2c…"}, "sender_name": "Billing Dept",
                "text": "…", "date": "2026-03-31T09:00:00Z"}],
  "target_message_ids": [10],
  "profiles": [{"sender": {"handle_hash": "9f2c…"}, "name": "Billing Dept", "bio": "", "is_contact": false}]
}
```

Differences from v1:

- `sender_id` and `telegram_id` become `sender`.
- `chat_metadata` becomes `conversation` (`type` → `kind`, in v2 kinds), and
  `channel` moves to the top level.
- `user_telegram_id` is dropped.
- `message_id` values are still only consistent within one request.
- `GET /result/{id}` and `POST /followup/{id}` are unchanged.

A v1 detection server keeps receiving Telegram scans exactly as before the
multi-channel refactor: Telegram message ids in `message_id` /
`target_message_ids`, Telegram sender ids in `sender_id` / `telegram_id`
(Telethon's `sender_id`: the user, or the marked peer id of the channel or chat
the message was posted as), and `chat_metadata` = `{chat_id, name, type}`. The
`chat_history` follow-up answers with the same Telegram ids. Other channels have
none of these ids, so a v1 server gets internal message ids that are consistent
within one request, `sender_id: null`, and
`chat_metadata` = `{name, type, channel}`. The local pipeline's prompt keeps
the Telegram header too (`Chat: <name> (<type>)`, `Profile - ID:<telegram id>`).

`POST /areFlaggedScams` is unchanged (`{"telegram_ids": [...]}`). Hashes are
per install, so a global known-scammer lookup only makes sense for public-id
channels.

## 5. Client behaviour summary

| Server advertises | Telegram rows | Gmail / Signal / SMS rows |
|---|---|---|
| nothing, `[1]`, or an error | v1 payloads, byte for byte as before | stay local and unsynced (as today) |
| `[…, 2]` | v2 payloads | v2 payloads, which drain the backlog the v1 period built up |

- Rows are marked synced exactly as in v1. Flags are marked only if they did not
  change during the POST. Scans are marked only if `scam_updated_at` did not
  change meanwhile. Reports, dismissals and feedback get `synced_at` or status.
- Rows already uploaded under v1 are not sent again under v2.
- Dismissals are queued with internal ids. For Telegram rows they also record
  the Telegram ids when they are created (`cloud_v1`), so a dismissal uploaded
  after its message or sender was deleted still carries them, in v1 and in v2
  (`{"id": …}`). A dismissal whose conversation or sender is gone and has no such
  snapshot can never be uploaded. It is retired: `cloud_status` becomes
  `skipped` with a `cloud_skipped_reason`, and `synced_at` is set. Uploaded rows
  get `cloud_status = synced`. Rows of other channels that still
  exist wait for a v2 server, as described above.
- If the vault is locked, there is no salt and v2 uploads fail for that cycle.
  Sync only runs while the vault is unlocked anyway.

## 6. Server checklist

1. Serve `GET /sync/capabilities` → `{"contract_versions": [1, 2]}`, and add
   `contract_versions` to `GET /sync/manifest`.
2. Route on `X-Mingly-Contract`. Keep the v1 handlers.
3. Store rows keyed by `(install, channel, id|ref|handle_hash)`. Treat
   `handle_hash` and `ref` as opaque, scoped to the API key or install, and never
   join them across installs.
4. Treat `content_included: false` as "withheld by the user", not "empty".
5. For detection, accept v2 on `POST /isThisScam` and advertise it on the
   detection service's own `GET /capabilities`.
