import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ScamMsgState, WSEvent } from '../../../api/types'

const checkMessagesForScam = vi.fn()
vi.mock('../../../api/client', async (orig) => ({
  ...(await orig<typeof import('../../../api/client')>()),
  checkMessagesForScam: (...a: unknown[]) => checkMessagesForScam(...a),
}))

import { __resetScamChecks, applyScamEvent, getScamCheck, startScamCheck } from '../scamChecks'

const state = (tg: number, effective: boolean): ScamMsgState => ({
  chat_id: -1, tg_message_id: tg, sender_id: 5, scam_score: effective ? 0.9 : 0.1, scam_reason: null, scam_category: null,
  scam_manual_verdict: null, scam_status: 'completed', scam_error: null, effective_is_scam: effective,
})
const base = { request_id: 'r1', source: 'manual' as const, chat_id: -1, sender_ids: [5], pipeline_is_scam: false, confidence: 0.7, reason: '', category: null }

beforeEach(() => {
  __resetScamChecks()
  checkMessagesForScam.mockReset().mockResolvedValue({ status: 'queued', message_count: 1, queued_ids: [10], already_running_ids: [], missing_ids: [] })
})

describe('scam checks', () => {
  it('stays "checking" until the terminal event for THIS message arrives', async () => {
    await startScamCheck(-1, 10)
    expect(getScamCheck(-1, 10)).toEqual({ status: 'checking' })
    applyScamEvent({ ...base, type: 'scam_check_safe', message_ids: [11], messages: [state(11, false)] } as WSEvent)
    applyScamEvent({ ...base, type: 'scam_check_safe', chat_id: -2, message_ids: [10], messages: [] } as WSEvent)
    expect(getScamCheck(-1, 10)).toEqual({ status: 'checking' })
    applyScamEvent({ ...base, type: 'scam_check_safe', message_ids: [10], messages: [state(10, false)] } as WSEvent)
    expect(getScamCheck(-1, 10)).toEqual({ status: 'done', result: 'safe' })
  })

  it('reports the stored effective verdict, not the event type', async () => {
    await startScamCheck(-1, 10)
    // Pipeline says scam, but a manual not_scam keeps it safe
    applyScamEvent({ ...base, type: 'scam_detected', message_ids: [10], messages: [state(10, false)] } as WSEvent)
    expect(getScamCheck(-1, 10)).toEqual({ status: 'done', result: 'safe' })
  })

  it('surfaces errors from the request and from the terminal event', async () => {
    checkMessagesForScam.mockRejectedValueOnce(new Error('Scam service not initialized'))
    await startScamCheck(-1, 10)
    expect(getScamCheck(-1, 10)).toMatchObject({ status: 'error', error: expect.stringMatching(/not initialized/) })
    await startScamCheck(-1, 12)
    applyScamEvent({ ...base, type: 'scam_check_error', request_id: null, message_ids: [12], messages: [], error: 'LLM timeout' } as WSEvent)
    expect(getScamCheck(-1, 12)).toEqual({ status: 'error', error: 'LLM timeout' })
  })

  it('ignores terminal events for messages nobody asked to check', () => {
    applyScamEvent({ ...base, type: 'scam_detected', message_ids: [10], messages: [state(10, true)] } as WSEvent)
    expect(getScamCheck(-1, 10)).toBeNull()
  })
})
