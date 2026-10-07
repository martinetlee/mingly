import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, act, waitFor } from '@testing-library/react'
import { ApiError } from '../../lib/http'
import { resetToasts, subscribeToasts, type ToastItem } from '../../lib/toast'
import { ConfirmProvider } from '../../components/ui'

vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  setBriefingMaxTokens: vi.fn(),
  setBriefingTime: vi.fn(),
  setBriefingSchedule: vi.fn(),
  setBriefingDay: vi.fn(),
  getSessionTimeout: vi.fn(),
  setSessionTimeout: vi.fn(),
  getMcpSettings: vi.fn(),
  generateMcpKey: vi.fn(),
  setMcpEnabled: vi.fn(),
  revealMcpKey: vi.fn(),
}))

import {
  setBriefingMaxTokens, setBriefingTime, setBriefingSchedule, getSessionTimeout, setSessionTimeout,
  getMcpSettings, generateMcpKey,
} from '../../api/client'
import {
  BriefingSection, SecuritySection, validateBriefingTime, validateMaxTokens, validateSessionTimeout,
} from '../settings/PreferenceSections'
import McpSection from '../settings/McpSection'

let toasts: ToastItem[] = []
let unsub: () => void = () => {}

beforeEach(() => {
  vi.clearAllMocks()
  resetToasts()
  unsub = subscribeToasts((t) => { toasts = t })
})
afterEach(() => {
  unsub()
  cleanup()
  vi.useRealTimers()
})

const http400 = (detail: string) => new ApiError(detail, { status: 400, code: 'http', path: '/api/settings/prefs/x' })

describe('validators', () => {
  it('briefing time is strict 24h HH:MM', () => {
    expect(validateBriefingTime('09:30')).toBeNull()
    expect(validateBriefingTime('23:59')).toBeNull()
    for (const bad of ['99:99', '9:30', '24:00', '', '09:3', ' 09:30']) expect(validateBriefingTime(bad)).not.toBeNull()
  })
  it('max tokens and session timeout ranges', () => {
    expect(validateMaxTokens('256')).toBeNull()
    expect(validateMaxTokens('80')).not.toBeNull()
    expect(validateMaxTokens('')).not.toBeNull()
    expect(validateSessionTimeout(0)).toBeNull()
    expect(validateSessionTimeout(5)).toBeNull()
    expect(validateSessionTimeout(10080)).toBeNull()
    expect(validateSessionTimeout(3)).not.toBeNull()
    expect(validateSessionTimeout(20000)).not.toBeNull()
  })
})

describe('BriefingSection', () => {
  it('max tokens: typing is not clamped and nothing is sent until the value is valid', async () => {
    vi.useFakeTimers()
    vi.mocked(setBriefingMaxTokens).mockResolvedValue({ success: true, max_tokens: 8000 })
    render(<BriefingSection prefs={{ briefing_max_tokens: 4000 }} />)
    const input = screen.getByLabelText('Max tokens per summary') as HTMLInputElement
    expect(input.value).toBe('4000')

    // Typing 8000 key by key: 8 → 80 → 800 → 8000; no clamp to 256, no POST per keystroke
    for (const v of ['8', '80', '800', '8000']) {
      fireEvent.change(input, { target: { value: v } })
      expect(input.value).toBe(v)
    }
    expect(setBriefingMaxTokens).not.toHaveBeenCalled()
    await act(async () => { vi.advanceTimersByTime(1000) })
    expect(setBriefingMaxTokens).toHaveBeenCalledTimes(1)
    expect(setBriefingMaxTokens).toHaveBeenCalledWith(8000)
  })

  it('max tokens: an invalid value reverts on blur without saving', async () => {
    render(<BriefingSection prefs={{ briefing_max_tokens: 4000 }} />)
    const input = screen.getByLabelText('Max tokens per summary') as HTMLInputElement
    fireEvent.change(input, { target: { value: '80' } })
    expect(screen.getByRole('alert').textContent).toMatch(/between 256 and 32000/i)
    fireEvent.blur(input)
    expect(input.value).toBe('4000')
    expect(setBriefingMaxTokens).not.toHaveBeenCalled()
  })

  it('a rejected save reverts the field and shows an error toast', async () => {
    vi.mocked(setBriefingTime).mockRejectedValue(http400('Invalid time. Use 24-hour HH:MM (00:00-23:59).'))
    render(<BriefingSection prefs={{ briefing_time: '09:00' }} />)
    const input = screen.getByLabelText(/run time/i) as HTMLInputElement
    fireEvent.change(input, { target: { value: '10:15' } })
    fireEvent.blur(input)
    await waitFor(() => expect(setBriefingTime).toHaveBeenCalledWith('10:15'))
    await waitFor(() => expect(input.value).toBe('09:00'))
    expect(toasts.some((t) => t.kind === 'error' && t.message.includes("Couldn't save briefing time"))).toBe(true)
  })

  it('weekly schedule text uses briefing_day instead of a hard-coded Monday', async () => {
    vi.mocked(setBriefingSchedule).mockResolvedValue({ success: true, schedule: 'weekly' })
    render(<BriefingSection prefs={{ briefing_schedule: 'weekly', briefing_day: 'thursday' }} />)
    expect(screen.getAllByText(/each Thursday/).length).toBeGreaterThan(0)
    expect(screen.queryByText(/Monday morning/)).toBeNull()
    expect((screen.getByLabelText('Day of the week') as HTMLSelectElement).value).toBe('thursday')
  })
})

describe('SecuritySection', () => {
  it('loads and saves the session idle timeout (0 = never)', async () => {
    vi.mocked(getSessionTimeout).mockResolvedValue({ minutes: 720 })
    vi.mocked(setSessionTimeout).mockResolvedValue({ success: true, minutes: 0 })
    render(<SecuritySection />)
    const select = screen.getByLabelText(/ask for the password after inactivity/i) as HTMLSelectElement
    await waitFor(() => expect(select.disabled).toBe(false))
    expect(select.value).toBe('720')
    fireEvent.change(select, { target: { value: '0' } })
    await waitFor(() => expect(setSessionTimeout).toHaveBeenCalledWith(0))
    expect(select.value).toBe('0')
  })
})

describe('McpSection', () => {
  it('regenerating an existing key asks through the shared confirm dialog, not window.confirm', async () => {
    const nativeConfirm = vi.spyOn(window, 'confirm')
    vi.mocked(getMcpSettings).mockResolvedValue({ enabled: true, key_set: true, url: 'http://127.0.0.1:8765/mcp', vault_unlocked: true })
    vi.mocked(generateMcpKey).mockResolvedValue({ key: 'new-key-123' })
    render(<ConfirmProvider><McpSection /></ConfirmProvider>)

    fireEvent.click(await screen.findByRole('button', { name: 'Regenerate' }))
    expect(nativeConfirm).not.toHaveBeenCalled()
    expect(generateMcpKey).not.toHaveBeenCalled()
    expect(screen.getByText(/regenerate the mcp access key/i)).toBeTruthy()

    const dialogButtons = screen.getAllByRole('button', { name: 'Regenerate' })
    fireEvent.click(dialogButtons[dialogButtons.length - 1])
    await waitFor(() => expect(generateMcpKey).toHaveBeenCalledTimes(1))
    expect(await screen.findByText('new-key-123')).toBeTruthy()
    nativeConfirm.mockRestore()
  })
})
