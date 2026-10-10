/**
 * A small, subtle mark for the channel a conversation or identity lives on.
 * Unknown channels get a neutral icon — never assume Telegram.
 */
import { Mail, MessageCircle, MessagesSquare, Send, Smartphone } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import type { ChannelName } from '../../api/types'

const CHANNELS: Record<string, { icon: LucideIcon; label: string }> = {
  telegram: { icon: Send, label: 'Telegram' },
  gmail: { icon: Mail, label: 'Gmail' },
  signal: { icon: MessageCircle, label: 'Signal' },
  sms: { icon: Smartphone, label: 'SMS' },
}

export function channelLabel(channel: ChannelName | null | undefined): string {
  if (!channel) return 'Unknown channel'
  return CHANNELS[channel]?.label ?? channel.charAt(0).toUpperCase() + channel.slice(1)
}

export default function ChannelIcon({
  channel,
  size = 10,
  className = 'text-slate-500',
}: {
  channel: ChannelName | null | undefined
  size?: number
  className?: string
}) {
  const Icon = (channel && CHANNELS[channel]?.icon) || MessagesSquare
  const label = channelLabel(channel)
  return (
    <span title={label} aria-label={label} className="inline-flex flex-shrink-0">
      <Icon size={size} className={className} aria-hidden />
    </span>
  )
}
