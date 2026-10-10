import {
  getSessionTimeout,
  setBriefingDay as apiSetBriefingDay,
  setBriefingMaxTokens as apiSetBriefingMaxTokens,
  setBriefingSchedule as apiSetBriefingSchedule,
  setBriefingTime as apiSetBriefingTime,
  setCatchUpSummaryInterval as apiSetCatchUpSummaryInterval,
  setNotifPref as apiSetNotifPref,
  setSessionTimeout as apiSetSessionTimeout,
  setTranslationLanguage as apiSetTranslationLanguage,
} from '../../api/client'
import { setChannelNotifPref } from '../../api/email'
import type { Prefs } from '../../api/types'
import { channelLabel } from '../../components/channels/ChannelIcon'
import { WEEKDAYS, dayLabel } from '../../features/briefing/schedule'
import { useAccounts } from '../../hooks/useAccounts'
import { useSettingField } from '../../hooks/useSettingField'
import { Card, FieldStatus, Section, inputClass, labelClass, selectClass } from './shared'

type NotifPref = 'all' | 'tracked' | 'off'
type BriefingSchedule = 'daily' | 'weekly'

/** Strict 24-hour HH:MM (00:00–23:59), as the backend requires. */
export const BRIEFING_TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/

export function validateBriefingTime(v: string): string | null {
  return BRIEFING_TIME_RE.test(v) ? null : 'Use 24-hour HH:MM (00:00–23:59)'
}

export function validateMaxTokens(v: string): string | null {
  if (!/^\d+$/.test(v.trim())) return 'Enter a whole number'
  const n = Number(v)
  return n < 256 || n > 32000 ? 'Between 256 and 32000' : null
}

export function validateSessionTimeout(v: number): string | null {
  return v === 0 || (Number.isInteger(v) && v >= 5 && v <= 10080) ? null : 'Use Never or 5 minutes to 7 days'
}

const LANGUAGES = [
  'English', 'Chinese (Traditional)', 'Chinese (Simplified)', 'Japanese', 'Korean', 'Spanish', 'French',
  'German', 'Portuguese', 'Russian', 'Arabic', 'Hindi', 'Thai', 'Vietnamese', 'Indonesian', 'Turkish', 'Italian',
]

const CATCHUP_INTERVALS: [number, string][] = [
  [5, 'Every 5 minutes'],
  [15, 'Every 15 minutes'],
  [30, 'Every 30 minutes (default)'],
  [60, 'Every hour'],
  [120, 'Every 2 hours'],
  [360, 'Every 6 hours'],
  [720, 'Every 12 hours'],
  [1440, 'Once a day'],
]

const SESSION_TIMEOUTS: [number, string][] = [
  [15, '15 minutes'],
  [30, '30 minutes'],
  [60, '1 hour'],
  [240, '4 hours'],
  [720, '12 hours (default)'],
  [1440, '1 day'],
  [10080, '7 days'],
  [0, 'Never'],
]

function minutesLabel(m: number): string {
  if (m === 0) return 'Never'
  if (m % 1440 === 0) return `${m / 1440} day${m === 1440 ? '' : 's'}`
  if (m % 60 === 0) return `${m / 60} hour${m === 60 ? '' : 's'}`
  return `${m} minutes`
}

const echoKey = <T,>(key: string) => (r: unknown) => (r as Record<string, T> | undefined)?.[key]

// ---------------------------------------------------------------------------

export function NotificationsSection({ prefs }: { prefs: Prefs | undefined }) {
  const notif = useSettingField<NotifPref>({
    initial: 'tracked',
    serverValue: prefs ? ((prefs.notif_pref as NotifPref | undefined) ?? 'tracked') : undefined,
    set: apiSetNotifPref,
    echo: echoKey<NotifPref>('pref'),
    label: 'notification preference',
  })
  // Connected channels with their own setting get a selector below the global one
  const { accounts } = useAccounts()
  const channelOverrides = NOTIF_DEFAULT_OFF_CHANNELS.filter((ch) => accounts.some((a) => a.channel === ch && a.configured))

  return (
    <Section title="Notifications" description="Get notified when new messages arrive." className="">
      <Card>
        <label className={labelClass} htmlFor="notif-pref">Notify me about</label>
        <select
          id="notif-pref"
          value={notif.value}
          onChange={(e) => notif.setValue(e.target.value as NotifPref, { immediate: true })}
          className={selectClass}
        >
          <option value="all">All new messages</option>
          <option value="tracked">Tracked chats only</option>
          <option value="off">Off</option>
        </select>
        {notif.value !== 'off' && (
          <p className="mt-2 text-xs text-emerald-400">✓ Native notifications enabled</p>
        )}
        {channelOverrides.map((ch) => (
          <ChannelNotifField key={ch} channel={ch} prefs={prefs} />
        ))}
      </Card>
    </Section>
  )
}

// Channels that notify on their own setting; until set, the backend
// (api/settings.py get_notif_pref) keeps them off.
const NOTIF_DEFAULT_OFF_CHANNELS = ['gmail']

/** One connected channel's own notification setting (e.g. email, off by default). */
function ChannelNotifField({ channel, prefs }: { channel: string; prefs: Prefs | undefined }) {
  const perChannel = prefs?.notif_pref_channels
  const id = `notif-pref-${channel}`
  const field = useSettingField<NotifPref>({
    initial: 'off',
    serverValue: prefs ? (perChannel?.[channel] ?? 'off') : undefined,
    set: (v) => setChannelNotifPref(channel, v),
    echo: echoKey<NotifPref>('pref'),
    label: `${channelLabel(channel)} notification preference`,
  })
  return (
    <div className="mt-4">
      <label className={labelClass} htmlFor={id}>{channelLabel(channel)}</label>
      <select
        id={id}
        value={field.value}
        onChange={(e) => field.setValue(e.target.value as NotifPref, { immediate: true })}
        className={selectClass}
      >
        <option value="all">All new messages</option>
        <option value="tracked">Tracked conversations only</option>
        <option value="off">Off</option>
      </select>
      <p className="mt-1.5 text-xs text-slate-500">Set separately; off until you turn it on.</p>
    </div>
  )
}

export function TranslationSection({ prefs }: { prefs: Prefs | undefined }) {
  const lang = useSettingField<string>({
    initial: 'English',
    serverValue: prefs ? (prefs.translation_language ?? 'English') : undefined,
    set: apiSetTranslationLanguage,
    echo: echoKey<string>('language'),
    label: 'translation language',
  })
  const options = LANGUAGES.includes(lang.value) ? LANGUAGES : [lang.value, ...LANGUAGES]

  return (
    <Section title="Translation" description="Default language for translating messages." className="mt-8">
      <Card>
        <label className={labelClass} htmlFor="translation-lang">Translate to</label>
        <select
          id="translation-lang"
          value={lang.value}
          onChange={(e) => lang.setValue(e.target.value, { immediate: true })}
          className={selectClass}
        >
          {options.map((l) => <option key={l} value={l}>{l}</option>)}
        </select>
      </Card>
    </Section>
  )
}

export function BriefingSection({ prefs }: { prefs: Prefs | undefined }) {
  const schedule = useSettingField<BriefingSchedule>({
    initial: 'daily',
    serverValue: prefs ? (prefs.briefing_schedule ?? 'daily') : undefined,
    set: apiSetBriefingSchedule,
    echo: echoKey<BriefingSchedule>('schedule'),
    label: 'briefing frequency',
  })
  const day = useSettingField<string>({
    initial: 'monday',
    serverValue: prefs ? (prefs.briefing_day ?? 'monday') : undefined,
    set: apiSetBriefingDay,
    echo: echoKey<string>('day'),
    label: 'briefing day',
  })
  const time = useSettingField<string>({
    initial: '09:00',
    serverValue: prefs ? (prefs.briefing_time ?? '09:00') : undefined,
    set: apiSetBriefingTime,
    echo: echoKey<string>('time'),
    validate: validateBriefingTime,
    label: 'briefing time',
  })
  const maxTokens = useSettingField<string>({
    initial: '4000',
    serverValue: prefs ? String(prefs.briefing_max_tokens ?? 4000) : undefined,
    set: (v) => apiSetBriefingMaxTokens(Number(v)),
    echo: (r) => {
      const n = (r as { max_tokens?: number } | undefined)?.max_tokens
      return n != null ? String(n) : undefined
    },
    validate: validateMaxTokens,
    debounceMs: 800,
    label: 'max tokens per summary',
  })

  const weekly = schedule.value === 'weekly'
  const when = weekly ? `each ${dayLabel(day.value)}` : 'each day'

  return (
    <Section
      title="Briefing Schedule"
      description="How often the Intelligence → Briefing tab auto-updates chat summaries. The update runs at the configured time, or on first app open after that time."
    >
      <Card className="flex flex-col gap-4">
        <div>
          <label className={labelClass} htmlFor="briefing-schedule">Update frequency</label>
          <select
            id="briefing-schedule"
            value={schedule.value}
            onChange={(e) => schedule.setValue(e.target.value as BriefingSchedule, { immediate: true })}
            className={selectClass}
          >
            <option value="daily">Daily (last 24 hours)</option>
            <option value="weekly">Weekly (last 7 days)</option>
          </select>
          <p className="mt-2 text-xs text-slate-500">
            {weekly
              ? `Summarises the past week of messages ${when}.`
              : 'Summarises the past day of messages each day.'}
          </p>
        </div>
        {weekly && (
          <div>
            <label className={labelClass} htmlFor="briefing-day">Day of the week</label>
            <select
              id="briefing-day"
              value={day.value}
              onChange={(e) => day.setValue(e.target.value, { immediate: true })}
              className={selectClass}
            >
              {WEEKDAYS.map((d) => <option key={d} value={d}>{dayLabel(d)}</option>)}
            </select>
          </div>
        )}
        <div>
          <label className={labelClass} htmlFor="briefing-time">Run time (local time, 24-hour)</label>
          <input
            id="briefing-time"
            type="time"
            value={time.value}
            onChange={(e) => time.setValue(e.target.value)}
            onBlur={time.flush}
            className={`${inputClass} cursor-pointer`}
          />
          <FieldStatus saving={time.saving} invalid={time.invalid} />
          <p className="mt-2 text-xs text-slate-500">
            The briefing will auto-run at this time {when}, or on first app open after this time.
          </p>
        </div>
        <div>
          <label className={labelClass} htmlFor="briefing-max-tokens">Max tokens per summary</label>
          <div className="flex items-center gap-3">
            <input
              id="briefing-max-tokens"
              type="number"
              inputMode="numeric"
              min={256}
              max={32000}
              step={256}
              value={maxTokens.value}
              onChange={(e) => maxTokens.setValue(e.target.value)}
              onBlur={maxTokens.flush}
              onKeyDown={(e) => { if (e.key === 'Enter') maxTokens.flush() }}
              className={`w-32 ${inputClass}`}
            />
            <div className="flex gap-1.5">
              {[1024, 2048, 4000, 8000].map((preset) => (
                <button
                  key={preset}
                  type="button"
                  onClick={() => maxTokens.setValue(String(preset), { immediate: true })}
                  className={`rounded-lg px-2.5 py-1.5 text-xs font-medium transition ${
                    maxTokens.value === String(preset)
                      ? 'bg-blue-600 text-white'
                      : 'bg-slate-700 text-slate-400 hover:bg-slate-600 hover:text-slate-200'
                  }`}
                >
                  {preset >= 1000 ? `${preset / 1000}k` : preset}
                </button>
              ))}
            </div>
          </div>
          <FieldStatus saving={maxTokens.saving} invalid={maxTokens.invalid} />
          <p className="mt-2 text-xs text-slate-500">
            Higher values allow longer, more detailed summaries but increase generation time. Reasoning models need 8k+ tokens to think before answering.
          </p>
        </div>
      </Card>
    </Section>
  )
}

export function CatchUpSection({ prefs }: { prefs: Prefs | undefined }) {
  const interval = useSettingField<number>({
    initial: 30,
    serverValue: prefs ? (prefs.catchup_summary_interval_minutes ?? 30) : undefined,
    set: apiSetCatchUpSummaryInterval,
    echo: echoKey<number>('interval_minutes'),
    label: 'catch-up summary interval',
  })
  const options = CATCHUP_INTERVALS.some(([m]) => m === interval.value)
    ? CATCHUP_INTERVALS
    : [[interval.value, `Every ${minutesLabel(interval.value)}`] as [number, string], ...CATCHUP_INTERVALS]

  return (
    <Section
      title="Catch-Up Summary Schedule"
      description="How often the background job regenerates catch-up card summaries. You can also manually trigger a refresh from the Catch Up tab."
    >
      <Card>
        <label className={labelClass} htmlFor="catchup-interval">Summary refresh interval</label>
        <select
          id="catchup-interval"
          value={interval.value}
          onChange={(e) => interval.setValue(Number(e.target.value), { immediate: true })}
          className={selectClass}
        >
          {options.map(([m, label]) => <option key={m} value={m}>{label}</option>)}
        </select>
        <p className="mt-2 text-xs text-slate-500">
          Summaries are generated for all active catch-up items at this interval.
          Shorter intervals mean fresher summaries but use more resources.
        </p>
      </Card>
    </Section>
  )
}

export function SecuritySection() {
  const timeout = useSettingField<number>({
    initial: 720,
    get: () => getSessionTimeout().then((r) => r.minutes),
    set: apiSetSessionTimeout,
    echo: echoKey<number>('minutes'),
    validate: validateSessionTimeout,
    label: 'session timeout',
  })
  const options = SESSION_TIMEOUTS.some(([m]) => m === timeout.value)
    ? SESSION_TIMEOUTS
    : [[timeout.value, minutesLabel(timeout.value)] as [number, string], ...SESSION_TIMEOUTS]

  return (
    <Section
      title="Security"
      description="Your Telegram credentials, session and API keys are encrypted with your master password. Messages and other app data are stored unencrypted in a local database on this computer."
    >
      <Card>
        <label className={labelClass} htmlFor="session-timeout">Ask for the password after inactivity</label>
        <select
          id="session-timeout"
          value={timeout.value}
          disabled={!timeout.loaded}
          onChange={(e) => timeout.setValue(Number(e.target.value), { immediate: true })}
          className={selectClass}
        >
          {options.map(([m, label]) => <option key={m} value={m}>{label}</option>)}
        </select>
        <FieldStatus saving={timeout.saving} invalid={timeout.invalid} />
        <p className="mt-2 text-xs text-slate-500">
          After this long without activity, this window must be unlocked again. Background sync keeps running.
        </p>
      </Card>
    </Section>
  )
}
