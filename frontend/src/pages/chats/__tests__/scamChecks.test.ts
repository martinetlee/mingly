import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ScamMsgState, WSEvent } from '../../../api/types'

const checkMessagesForScam = vi.fn()
const setMessageVerdict = vi.fn()
const setSenderVerdict = vi.fn()
vi.mock('../../../api/client', async (orig) => ({
  ...(await orig<typeof import('../../../api/client')>()),
  checkMessagesForScam: (...a: unknown[]) => checkMessagesForScam(...a),
  setMessageVerdict: (...a: unknown[]) => setMessageVerdict(...a),
  setSenderVerdict: (...a: unknown[]) => setSenderVerdict(...a),
}))
const toastSuccess = vi.fn()
const reportError = vi.fn()
vi.mock('../../../lib/toast', async (orig) => ({
  ...(await orig<typeof import('../../../lib/toast')>()),
  toast: { success: (...a: unknown[]) => toastSuccess(...a), info: vi.fn(), error: vi.fn() },
  reportError: (...a: unknown[]) => reportError(...a),
}))

import { __resetScamChecks, applyScamEvent, applyScamStatesTo, getScamCheck, startScamCheck, submitNotScamVerdict } from '../scamChecks'

const state = (id: number, effective: boolean): ScamMsgState => ({
  conversation_id: 1, message_id: id, identity_id: 5, scam_score: effective ? 0.9 : 0.1, scam_reason: null, scam_category: null,
  scam_manual_verdict: null, scam_status: 'completed', scam_error: null, effective_is_scam: effective,
})
const base = { request_id: 'r1', source: 'manual' as const, conversation_id: 1, identity_ids: [5], pipeline_is_scam: false, confidence: 0.7, reason: '', category: null }

beforeEach(() => {
  __resetScamChecks()
  checkMessagesForScam.mockReset().mockResolvedValue({ status: 'queued', message_count: 1, queued_ids: [10], already_running_ids: [], missing_ids: [] })
})

describe('scam checks', () => {
  it('stays "checking" until the terminal event for THIS message arrives', async () => {
    await startScamCheck(1, 10)
    expect(getScamCheck(1, 10)).toEqual({ status: 'checking' })
    applyScamEvent({ ...base, type: 'scam_check_safe', message_ids: [11], messages: [state(11, false)] } as WSEvent)
    applyScamEvent({ ...base, type: 'scam_check_safe', conversation_id: 2, message_ids: [10], messages: [] } as WSEvent)
    expect(getScamCheck(1, 10)).toEqual({ status: 'checking' })
    applyScamEvent({ ...base, type: 'scam_check_safe', message_ids: [10], messages: [state(10, false)] } as WSEvent)
    expect(getScamCheck(1, 10)).toEqual({ status: 'done', result: 'safe' })
  })

  it('reports the stored effective verdict, not the event type', async () => {
    await startScamCheck(1, 10)
    // Pipeline says scam, but a manual not_scam keeps it safe
    applyScamEvent({ ...base, type: 'scam_detected', message_ids: [10], messages: [state(10, false)] } as WSEvent)
    expect(getScamCheck(1, 10)).toEqual({ status: 'done', result: 'safe' })
  })

  it('surfaces errors from the request and from the terminal event', async () => {
    checkMessagesForScam.mockRejectedValueOnce(new Error('Scam service not initialized'))
    await startScamCheck(1, 10)
    expect(getScamCheck(1, 10)).toMatchObject({ status: 'error', error: expect.stringMatching(/not initialized/) })
    await startScamCheck(1, 12)
    applyScamEvent({ ...base, type: 'scam_check_error', request_id: null, message_ids: [12], messages: [], error: 'LLM timeout' } as WSEvent)
    expect(getScamCheck(1, 12)).toEqual({ status: 'error', error: 'LLM timeout' })
  })

  it('ignores terminal events for messages nobody asked to check', () => {
    applyScamEvent({ ...base, type: 'scam_detected', message_ids: [10], messages: [state(10, true)] } as WSEvent)
    expect(getScamCheck(1, 10)).toBeNull()
  })
})

describe('applyScamStatesTo (panes holding their own messages, e.g. Mail)', () => {
  const msgs = [{ id: 10, effective_is_scam: false }, { id: 11, effective_is_scam: false }]
  it('merges the stored state into the matching messages of that conversation', () => {
    const next = applyScamStatesTo(msgs, 1, [state(10, true)])
    expect(next).not.toBe(msgs)
    expect(next[0].effective_is_scam).toBe(true)
    expect(next[1]).toBe(msgs[1])
  })
  it('returns the same array when nothing matches (another conversation, other ids, no entries)', () => {
    expect(applyScamStatesTo(msgs, 2, [state(10, true)])).toBe(msgs)
    expect(applyScamStatesTo(msgs, 1, [state(99, true)])).toBe(msgs)
    expect(applyScamStatesTo(msgs, 1, undefined)).toBe(msgs)
  })
})

describe('submitNotScamVerdict', () => {
  const m = { id: 10, conversation_id: 1, sender: { identity_id: 5 } } as Parameters<typeof submitNotScamVerdict>[0]
  beforeEach(() => { setMessageVerdict.mockReset(); setSenderVerdict.mockReset(); toastSuccess.mockReset(); reportError.mockReset() })

  it('message scope: one message not_scam, resolves to the stored states', async () => {
    setMessageVerdict.mockResolvedValue({ messages: [state(10, false)], flags: [] })
    await expect(submitNotScamVerdict(m, 'message')).resolves.toEqual([state(10, false)])
    expect(setMessageVerdict).toHaveBeenCalledWith([{ message_id: 10, conversation_id: 1 }], 'not_scam')
    expect(toastSuccess).toHaveBeenCalledWith('Marked as not a scam')
  })
  it('sender scope dismisses the sender identity', async () => {
    setSenderVerdict.mockResolvedValue({ messages: [], flags: [] })
    await submitNotScamVerdict(m, 'sender')
    expect(setSenderVerdict).toHaveBeenCalledWith(5, 'not_scam')
    expect(toastSuccess).toHaveBeenCalledWith('Sender dismissed')
  })
  it('a failure is reported and resolves null', async () => {
    setMessageVerdict.mockRejectedValue(new Error('boom'))
    await expect(submitNotScamVerdict(m, 'message')).resolves.toBeNull()
    expect(reportError).toHaveBeenCalled()
  })
})
