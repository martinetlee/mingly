import { useEffect, useState } from 'react'
import { generateMcpKey, getMcpSettings, revealMcpKey, setMcpEnabled as apiSetMcpEnabled, setMcpWriteEnabled } from '../../api/client'
import { useConfirm } from '../../components/ui'
import { useAsyncAction } from '../../hooks/useAsyncAction'
import { reportError } from '../../lib/toast'
import { Card, Section, Switch, copyText } from './shared'
import ArchiveCard from './ArchiveCard'

const smallButton = 'rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-xs font-medium text-slate-300 hover:bg-slate-600 disabled:opacity-50'

export default function McpSection() {
  const confirm = useConfirm()
  const [enabled, setEnabled] = useState(false)
  const [keySet, setKeySet] = useState(false)
  const [url, setUrl] = useState('')
  const [key, setKey] = useState<string | null>(null)
  const [writeEnabled, setWriteEnabled] = useState(false)

  useEffect(() => {
    getMcpSettings()
      .then((s) => {
        setEnabled(s.enabled)
        setKeySet(s.key_set)
        setUrl(s.url)
        setWriteEnabled(!!s.write_enabled)
      })
      .catch((e) => reportError(e, 'Failed to load MCP settings'))
  }, [])

  const toggle = useAsyncAction(async (next: boolean) => {
    const res = await apiSetMcpEnabled(next)
    setEnabled(res.enabled)
    setKeySet(res.key_set)
  }, { errorContext: "Couldn't update the MCP server" })

  // Optimistic; a failed write reverts the switch (useAsyncAction toasts the error)
  const toggleWrite = useAsyncAction(async (next: boolean) => {
    setWriteEnabled(next)
    try {
      const res = await setMcpWriteEnabled(next)
      setWriteEnabled(res.write_enabled)
    } catch (e) {
      setWriteEnabled(!next)
      throw e
    }
  }, { errorContext: "Couldn't update agent write access" })

  const generate = useAsyncAction(async () => {
    const res = await generateMcpKey()
    setKey(res.key)
    setKeySet(true)
  }, { errorContext: "Couldn't generate an MCP key" })

  const reveal = useAsyncAction(async () => {
    const res = await revealMcpKey()
    setKey(res.key)
  }, { errorContext: "Couldn't reveal the MCP key" })

  const busy = toggle.pending || generate.pending || reveal.pending

  const handleGenerate = () => {
    if (!keySet) { void generate.run(); return }
    void confirm({
      title: 'Regenerate the MCP access key?',
      message: 'Every connected MCP client is disconnected and must be reconfigured with the new key.',
      destructive: true,
      confirmLabel: 'Regenerate',
      onConfirm: async () => {
        const res = await generateMcpKey()
        setKey(res.key)
        setKeySet(true)
      },
    })
  }

  const addCommand = (k: string) => `claude mcp add --transport http mingly ${url} --header "Authorization: Bearer ${k}"`

  return (
    <Section
      title="MCP Server"
      description="Let external AI tools (Claude Code, other MCP clients) read your chats (including hidden and archived ones; deactivated groups excluded), messages, tags, contacts, and organizations — and, if you allow it, record notes about people. Local-only, and active only while Mingly is running and unlocked. Nothing ever sends Telegram messages."
    >
      <Card>
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="text-sm text-slate-200">Enable MCP server</p>
            <p className="text-xs text-slate-400 mt-0.5">Off by default. Requires an access key.</p>
          </div>
          <Switch checked={enabled} disabled={busy} onChange={(next) => void toggle.run(next)} label="Enable MCP server" />
        </div>

        <div className="mt-4 flex items-center justify-between gap-3 border-t border-slate-700 pt-4">
          <div>
            <p className="text-sm text-slate-200">Allow agent to write notes and context</p>
            <p className="text-xs text-slate-400 mt-0.5">
              Read tools are always available to a connected client. Write tools are opt-in and off by default: adding
              and updating its own person notes, setting important / relationship status / Twitter / LinkedIn (never
              your own contact notes), adding positions, tagging chats, and marking DMs reviewed. Agent notes appear on
              each person's Profile, where you can edit or delete them. Telegram itself is never modified.
            </p>
          </div>
          <Switch
            checked={writeEnabled}
            disabled={toggleWrite.pending}
            onChange={(next) => void toggleWrite.run(next)}
            label="Allow agent to write notes and context"
          />
        </div>

        {enabled && (
          <div className="mt-4 space-y-3 border-t border-slate-700 pt-4">
            <div>
              <label className="mb-1.5 block text-xs font-medium text-slate-400">Server URL</label>
              <div className="flex items-center gap-2">
                <code className="flex-1 truncate rounded-lg border border-slate-600 bg-slate-900 px-3 py-2 text-xs text-slate-300">{url}</code>
                <button type="button" onClick={() => void copyText(url, 'URL copied')} className={smallButton}>Copy</button>
              </div>
              <p className="mt-1 text-[11px] text-slate-500">
                The port can change between launches if the default is taken — re-check this URL after restarting Mingly.
              </p>
            </div>

            <div>
              <label className="mb-1.5 block text-xs font-medium text-slate-400">Access key</label>
              {key ? (
                <div className="flex items-center gap-2">
                  <code className="flex-1 truncate rounded-lg border border-slate-600 bg-slate-900 px-3 py-2 text-xs text-emerald-300">{key}</code>
                  <button type="button" onClick={() => void copyText(key, 'Key copied')} className={smallButton}>Copy</button>
                  <button type="button" onClick={() => setKey(null)} className={smallButton}>Hide</button>
                </div>
              ) : (
                <div className="flex items-center gap-2">
                  <span className="flex-1 text-xs text-slate-400">
                    {keySet ? 'A key is set (stored encrypted in the vault).' : 'No key generated yet.'}
                  </span>
                  {keySet && (
                    <button type="button" onClick={() => void reveal.run()} disabled={busy} className={smallButton}>
                      Reveal
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={handleGenerate}
                    disabled={busy}
                    className="rounded-lg bg-indigo-600 px-3 py-2 text-xs font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
                  >
                    {keySet ? 'Regenerate' : 'Generate key'}
                  </button>
                </div>
              )}
            </div>

            {keySet && (
              <div>
                <label className="mb-1.5 block text-xs font-medium text-slate-400">Connect Claude Code</label>
                <div className="flex items-start gap-2">
                  <code className="flex-1 break-all rounded-lg border border-slate-600 bg-slate-900 px-3 py-2 text-[11px] leading-relaxed text-slate-300">
                    {addCommand('<key>')}
                  </code>
                  <button type="button" onClick={() => void copyText(addCommand(key ?? '<key>'), 'Command copied')} className={smallButton}>
                    Copy
                  </button>
                </div>
              </div>
            )}

            {!keySet && <p className="text-xs text-amber-400">Generate an access key so clients can connect.</p>}
          </div>
        )}
      </Card>
      <ArchiveCard />
    </Section>
  )
}
