/** Preferences, LLM keys, Ollama, MCP and feature switches. */

/**
 * GET /api/settings/prefs — the backend returns only these whitelisted keys,
 * each present only if set. API keys live in the vault and are never returned.
 */
export interface Prefs {
  notif_pref?: string
  /** Per-channel notification setting (email channels are 'off' until set) */
  notif_pref_channels?: Record<string, 'all' | 'tracked' | 'off'>
  /** Email senders whose remote images load automatically */
  email_remote_images_senders?: string[]
  briefing_schedule?: 'daily' | 'weekly'
  briefing_time?: string            // "HH:MM" (24h)
  briefing_day?: string             // 'monday' … 'sunday'
  briefing_max_tokens?: number
  briefing_last_run?: string
  catchup_summary_interval_minutes?: number
  translation_language?: string
  session_idle_timeout_minutes?: number
}

/** GET /api/llm/keys — whether a provider API key is stored in the vault */
export interface LlmKeyStatus {
  anthropic: boolean
  openai: boolean
}

export type LlmKeyProvider = keyof LlmKeyStatus

export interface OllamaStatus {
  running: boolean
  /** Orchestrator model name when the server is up but the model is not installed */
  missing_model: string | null
}

export interface McpSettings {
  enabled: boolean
  key_set: boolean
  url: string
  vault_unlocked: boolean
  /** Opt-in: MCP clients may write person notes, fields, positions, tags and review marks */
  write_enabled?: boolean
}

/** GET /api/features — backend feature switches the UI follows */
export interface Features {
  /** Shield (scam detection). Off while its backend is retired. */
  shield: boolean
}
