interface ShortcutGroup {
  title: string
  shortcuts: { keys: string; description: string }[]
}

const GROUPS: ShortcutGroup[] = [
  {
    title: 'Global',
    shortcuts: [
      { keys: '⌘K', description: 'Open global search' },
      { keys: '⌘1-7', description: 'Navigate to sidebar pages' },
      { keys: 'Esc', description: 'Close modal / panel' },
      { keys: '?', description: 'Show this help' },
    ],
  },
  {
    title: 'Chats',
    shortcuts: [
      { keys: 'j / ↓', description: 'Next chat' },
      { keys: 'k / ↑', description: 'Previous chat' },
      { keys: '/', description: 'Focus search' },
      { keys: 'i', description: 'Toggle info panel' },
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
  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />
      <div className="relative w-full max-w-md rounded-xl border border-slate-700/80 bg-slate-800 shadow-2xl overflow-hidden">
        <div className="flex items-center justify-between border-b border-slate-700/50 px-4 py-3">
          <h2 className="text-sm font-semibold text-gray-100">Keyboard Shortcuts</h2>
          <kbd className="rounded border border-slate-600 bg-slate-700 px-1.5 py-0.5 text-[10px] text-slate-400">
            ESC
          </kbd>
        </div>
        <div className="max-h-[60vh] overflow-y-auto p-4 space-y-5">
          {GROUPS.map((group) => (
            <div key={group.title}>
              <h3 className="text-[10px] font-medium uppercase tracking-wider text-slate-500 mb-2">
                {group.title}
              </h3>
              <div className="space-y-1">
                {group.shortcuts.map((s) => (
                  <div key={s.keys} className="flex items-center justify-between py-1">
                    <span className="text-xs text-slate-400">{s.description}</span>
                    <kbd className="rounded border border-slate-600 bg-slate-700 px-1.5 py-0.5 text-[10px] text-slate-300 font-mono">
                      {s.keys}
                    </kbd>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
