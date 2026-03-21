# Scam Detection External API — Technical Specification

The external service must implement 3 HTTP endpoints. All requests include an `Authorization: Bearer <api_key>` header.

## 1. `POST /areFlaggedScams`

Batch-check whether Telegram users are known scammers.

### Request

```json
{
  "telegram_ids": [123456789, 987654321]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `telegram_ids` | `int[]` | List of Telegram user IDs to check |

### Response

```json
{
  "flagged": [
    {
      "telegram_id": 987654321,
      "confidence": 0.95,
      "reason": "Known advance-fee fraud operator"
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `flagged` | `object[]` | Array of flagged users. Empty array if none are scammers. |
| `flagged[].telegram_id` | `int` | The Telegram user ID |
| `flagged[].confidence` | `float` | Confidence score, 0.0–1.0 |
| `flagged[].reason` | `string` | Human-readable explanation |

---

## 2. `POST /isThisScam`

Analyze messages for scam patterns. May return an immediate verdict, an async processing acknowledgment, or a follow-up data request.

### Request

```json
{
  "messages": [
    {
      "telegram_id": 42,
      "sender_id": 123456789,
      "sender_name": "John",
      "text": "Send me 1 BTC and I'll return 10!"
    }
  ],
  "user_telegram_id": 111222333,
  "profiles": [
    {
      "telegram_id": 123456789,
      "name": "John"
    }
  ],
  "chat_metadata": {
    "name": "Crypto Group",
    "type": "group"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `messages` | `object[]` | Messages to analyze |
| `messages[].telegram_id` | `int` | Message ID within the chat |
| `messages[].sender_id` | `int` | Telegram ID of the sender |
| `messages[].sender_name` | `string\|null` | Display name of the sender |
| `messages[].text` | `string\|null` | Message text content |
| `user_telegram_id` | `int` | Telegram ID of the app user (the person requesting the check) |
| `profiles` | `object[]` | Profile data for participants in the conversation |
| `profiles[].telegram_id` | `int` | Telegram ID |
| `profiles[].name` | `string` | Display name |
| `chat_metadata` | `object` | Metadata about the chat |
| `chat_metadata.name` | `string` | Chat/group name |
| `chat_metadata.type` | `string` | `"private"`, `"group"`, or `"channel"` |

### Response — Immediate Verdict

```json
{
  "is_scam": true,
  "confidence": 0.92,
  "reason": "Classic cryptocurrency doubling scam"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `is_scam` | `bool` | Whether the messages are a scam |
| `confidence` | `float` | Confidence score, 0.0–1.0 |
| `reason` | `string` | Human-readable explanation |

### Response — Async Processing

The verdict will be delivered later via the callback webhook.

```json
{
  "status": "processing",
  "request_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | `string` | `"processing"` |
| `request_id` | `string` | UUID to correlate the callback |

### Response — Follow-up Data Request

The service needs more context before making a decision.

```json
{
  "status": "need_more_data",
  "data_type": "chat_history",
  "params": {
    "limit": 100
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | `string` | `"need_more_data"` |
| `data_type` | `string` | Type of data requested (see table below) |
| `params` | `object` | Parameters for the data request |

#### Supported `data_type` values

| Value | Description | Params | Mingly responds with |
|-------|-------------|--------|---------------------|
| `chat_history` | Full message history for the chat | `limit` (int, max messages) | Array of `{telegram_id, sender_id, sender_name, text, date}` |

---

## 3. `POST /followup/{request_id}`

Continue a multi-step analysis. Mingly sends the data that was requested in the `need_more_data` response.

### Request

```json
{
  "data": [
    {
      "telegram_id": 42,
      "sender_id": 123456789,
      "sender_name": "John",
      "text": "Hello there",
      "date": "2026-03-21T09:00:00"
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `data` | `any` | The requested data, format depends on `data_type` |

### Response

Same format as `isThisScam` — either a final verdict, another `need_more_data`, or `processing`.

---

## Callback Webhook

For async results (`status: "processing"`), the external service delivers the verdict by calling Mingly's callback endpoint:

```
POST {mingly_callback_url}/api/scam/callback/{request_id}
```

### Verdict Callback

```json
{
  "is_scam": true,
  "confidence": 0.88,
  "reason": "Pattern matches known romance scam"
}
```

### Follow-up Data Request Callback

```json
{
  "status": "need_more_data",
  "data_type": "chat_history",
  "params": {
    "limit": 200
  }
}
```

---

## Interaction Lifecycle

```
Mingly                          External API
  │                                  │
  ├── POST /areFlaggedScams ────────►│
  │◄── {flagged: [...]} ────────────┤
  │                                  │
  ├── POST /isThisScam ────────────►│
  │◄── {is_scam, confidence, reason}┤  (immediate verdict)
  │        OR                        │
  │◄── {status: "processing"} ──────┤  (async — verdict via callback)
  │        OR                        │
  │◄── {status: "need_more_data"} ──┤  (needs more context)
  │                                  │
  ├── POST /followup/{id} ─────────►│  (Mingly sends requested data)
  │◄── {is_scam, ...} ─────────────┤  (final verdict)
  │        OR                        │
  │◄── {status: "need_more_data"} ──┤  (another round)
  │                                  │
  │  ... for async flow ...          │
  │                                  │
  │◄── POST /api/scam/callback/{id} ┤  (external calls Mingly back)
  │                                  │
```

## App Closure & Resilience

- All pending requests are persisted in SQLite before the API call is made.
- On app restart, requests less than 24 hours old are automatically re-issued.
- Requests older than 24 hours are marked as `expired`.
- The external API **must treat duplicate `request_id` values as idempotent** — returning the same result without re-processing.

## Authentication

- Mingly sends: `Authorization: Bearer <api_key>` on all outbound requests.
- The callback endpoint (`/api/scam/callback/{request_id}`) on Mingly is currently protected by session token auth. For production, this should be changed to HMAC signature verification or a shared secret.

## Error Handling

- HTTP 4xx/5xx responses from the external API cause the request to be marked as `failed` in Mingly's database.
- Failed requests are **not** automatically retried (only `pending` and `awaiting_followup` are retried on restart).
- Mingly applies a 30-second timeout for `areFlaggedScams` and 60-second timeout for `isThisScam` and `followup`.
