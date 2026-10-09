/**
 * The one "couldn't load" state for a pane or list: the error in red and a
 * Retry button. Use it wherever a load failed and there is nothing to show
 * (never an empty state, which would read as "there is nothing").
 */
export default function LoadError({ message, onRetry, className = '' }: {
  /** What failed and why, e.g. "Couldn't load chats: offline" */
  message: string
  onRetry?: () => void
  className?: string
}) {
  return (
    <div role="alert" className={`flex flex-col items-center justify-center gap-2 px-4 py-6 text-center ${className}`} data-testid="load-error">
      <p className="text-sm text-rose-400">{message}</p>
      {onRetry && (
        <button type="button" onClick={onRetry} className="text-xs text-indigo-400 hover:text-indigo-300">Retry</button>
      )}
    </div>
  )
}
