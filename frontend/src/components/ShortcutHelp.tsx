import Modal from './ui/Modal'
import { LOCK_ACTION } from '../lib/nav'
import { useNav } from '../context/FeaturesContext'

interface ShortcutGroup {
  title: string
  shortcuts: { keys: string; description: string }[]
}

const GROUPS: ShortcutGroup[] = [
  {
    title: 'Global',
    shortcuts: [
      { keys: '⌘K', description: 'Open global search' },
      { keys: 'Esc', description: 'Close modal / panel' },
      { keys: '?', description: 'Show this help' },
      { keys: LOCK_ACTION.keys, description: 'Lock the vault' },
    ],
  },
  {
    title: 'Chats',
    shortcuts: [
      { keys: 'j / ↓', description: 'Next chat' },
      { keys: 'k / ↑', description: 'Previous chat' },
      { keys: '/', description: 'Focus search' },
      { keys: 'i', description: 'Toggle info panel' },
      { keys: '⌘F', description: 'Search in chat' },
      { keys: 'n', description: 'New Telegram conversation' },
    ],
  },
  {
    title: 'Mail',
    shortcuts: [
      { keys: 'j / k', description: 'Next / previous thread' },
      { keys: '/', description: 'Filter threads' },
      { keys: 'c', description: 'Compose' },
      { keys: 'r', description: 'Reply' },
      { keys: '⇧R', description: 'Reply all' },
      { keys: 'e', description: 'Archive / move to Inbox' },
    ],
  },
  {
    title: 'Calendar',
    shortcuts: [
      { keys: 't', description: 'Today' },
      { keys: '← / →', description: 'Previous / next range' },
      { keys: 'a', description: 'Agenda view' },
      { keys: 'w', description: 'Week view' },
    ],
  },
  {
    title: 'Contacts',
    shortcuts: [
      { keys: 'j / ↓', description: 'Next contact' },
      { keys: 'k / ↑', description: 'Previous contact' },
      { keys: '/', description: 'Focus search' },
    ],
  },
]

export default function ShortcutHelp({ open, onClose }: { open: boolean; onClose: () => void }) {
  // Navigation is generated from the visible sidebar so it can't drift from Layout's bindings
  const { shortcuts: navShortcuts } = useNav()
  const groups: ShortcutGroup[] = [
    GROUPS[0],
    { title: 'Navigation', shortcuts: navShortcuts.map(({ keys, item }) => ({ keys, description: item.label })) },
    ...GROUPS.slice(1),
  ]
  return (
    <Modal open={open} onClose={onClose} title="Keyboard Shortcuts" size="md">
      <div className="space-y-5 p-4">
        {groups.map((group) => (
          <div key={group.title}>
            <h3 className="mb-2 text-[10px] font-medium uppercase tracking-wider text-slate-500">
              {group.title}
            </h3>
            <div className="space-y-1">
              {group.shortcuts.map((s) => (
                <div key={s.keys} className="flex items-center justify-between py-1">
                  <span className="text-xs text-slate-400">{s.description}</span>
                  <kbd className="rounded border border-slate-600 bg-slate-700 px-1.5 py-0.5 font-mono text-[10px] text-slate-300">
                    {s.keys}
                  </kbd>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </Modal>
  )
}
