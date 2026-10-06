import { useCallback } from 'react'
import { setMessageVerdict, setSenderVerdict } from '../../api/client'
import type { ScamManualVerdict, ScamVerdictResponse } from '../../api/types'
import { useAsyncAction } from '../../hooks/useAsyncAction'
import { applyFlagChanges } from '../../hooks/useFlaggedSenders'
import { toast } from '../../lib/toast'
import { useConfirm } from '../../components/ui'
import { invalidateShieldData } from './invalidate'

type MessageRef = { chat_id: number; tg_message_id: number }

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`

/**
 * The one way Shield changes a verdict. Message scope and sender scope both
 * go through the unified verdict endpoints; afterwards the flagged-senders
 * store takes the response's flag changes and every Shield list refetches.
 * `onRows` lets the caller patch its rows from the stored states right away.
 */
export function useVerdictActions(onRows?: (r: ScamVerdictResponse) => void) {
  const confirm = useConfirm()

  const settle = useCallback((r: ScamVerdictResponse) => {
    applyFlagChanges(r.flags)
    onRows?.(r)
    invalidateShieldData()
  }, [onRows])

  const messageAction = useAsyncAction(async (msgs: MessageRef[], verdict: ScamManualVerdict) => {
    if (!msgs.length) return
    const r = await setMessageVerdict(msgs.map(({ chat_id, tg_message_id }) => ({ chat_id, tg_message_id })), verdict)
    settle(r)
    const what = plural(r.count, 'message')
    toast.success(
      verdict === 'scam' ? `Marked ${what} as scam`
        : verdict === 'not_scam' ? `Marked ${what} as not scam`
          : `Cleared the manual verdict on ${what}`,
    )
    const dismissed = r.flags.filter((f) => f.status !== 'active').length
    if (verdict === 'not_scam' && dismissed) toast.info(`${plural(dismissed, 'sender')} unflagged (no scam messages left)`)
    return r
  }, { errorContext: "Couldn't update verdict" })

  const senderAction = useAsyncAction(async (telegramId: number, verdict: ScamManualVerdict) => {
    const r = await setSenderVerdict(telegramId, verdict)
    settle(r)
    return r
  }, { errorContext: "Couldn't update sender" })

  /** "Unflag user" / "Dismiss sender": the same sender-scope not_scam verdict everywhere. */
  const dismissSender = useCallback(async (telegramId: number, name?: string | null) => {
    const out: { r?: ScamVerdictResponse } = {}
    const ok = await confirm({
      title: `Unflag ${name || `user ${telegramId}`}?`,
      message: 'Their unmarked flagged messages will be marked not scam, and automatic scans will not flag them again. Messages you marked yourself keep your verdict.',
      confirmLabel: 'Unflag',
      // Runs inside the dialog: spinner, single-flight, inline error + retry
      onConfirm: async () => {
        out.r = await setSenderVerdict(telegramId, 'not_scam')
        settle(out.r)
      },
    })
    const r = out.r
    if (ok && r) toast.success(r.count ? `Unflagged; ${plural(r.count, 'message')} marked not scam` : 'Unflagged')
  }, [confirm, settle])

  const runSender = senderAction.run
  const flagSender = useCallback(async (telegramId: number) => {
    const r = await runSender(telegramId, 'scam')
    if (r) toast.success('Sender flagged as scammer')
  }, [runSender])

  return {
    setMessages: messageAction.run,
    dismissSender,
    flagSender,
    pending: messageAction.pending || senderAction.pending,
  }
}
