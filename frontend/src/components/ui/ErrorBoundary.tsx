import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'
import { AlertTriangle, RotateCcw } from 'lucide-react'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

/**
 * Catches render errors in a page so one broken component shows a
 * recoverable message instead of blanking the whole app. Give it a `key`
 * that changes on navigation so moving to another page clears the error.
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Page crashed:', error, info.componentStack)
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children
    return (
      <div role="alert" className="flex flex-1 items-center justify-center p-8">
        <div className="max-w-md rounded-lg border border-slate-700 bg-slate-800 p-6 text-center">
          <AlertTriangle className="mx-auto mb-3 h-8 w-8 text-amber-400" aria-hidden />
          <h2 className="mb-2 text-lg font-semibold text-slate-100">This page hit an error</h2>
          <p className="mb-4 break-words text-sm text-slate-400">{error.message || String(error)}</p>
          <button
            type="button"
            onClick={() => this.setState({ error: null })}
            className="inline-flex items-center gap-2 rounded-md bg-slate-700 px-3 py-1.5 text-sm text-slate-100 hover:bg-slate-600"
          >
            <RotateCcw className="h-4 w-4" aria-hidden />
            Try again
          </button>
        </div>
      </div>
    )
  }
}
