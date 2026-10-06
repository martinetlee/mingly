import type { ScannedMessage } from '../../api/types'
import type { ParsedTrace } from './shared'

function MessageBlock({ text }: { text: string }) {
  return (
    <div>
      <h4 className="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Message</h4>
      <pre className="whitespace-pre-wrap text-[11px] text-slate-300 bg-slate-900 rounded-lg p-3 border border-slate-700 max-h-32 overflow-y-auto">{text}</pre>
    </div>
  )
}

function DevDetails({ label, children, className = 'text-slate-400', maxH = 'max-h-48' }: { label: string; children: string; className?: string; maxH?: string }) {
  return (
    <details className="text-[10px]">
      <summary className="text-slate-500 cursor-pointer hover:text-slate-400">{label}</summary>
      <pre className={`mt-1 whitespace-pre-wrap text-[11px] ${className} bg-slate-950 rounded p-2 ${maxH} overflow-y-auto`}>{children}</pre>
    </details>
  )
}

/** Expanded pipeline trace for one scanned message. */
export default function TraceDetail({ msg, trace, devMode }: { msg: ScannedMessage; trace: ParsedTrace; devMode: boolean }) {
  if (trace) {
    return (
      <div className="space-y-3 max-w-4xl">
        <div className="flex items-center gap-3 text-[10px] text-slate-500">
          <span>Pipeline v{trace.pipeline_version}</span>
          <span>{trace.status}</span>
          {trace.duration_ms != null && <span>{trace.duration_ms}ms</span>}
          {msg.scam_attempts > 1 && <span>{msg.scam_attempts} attempts</span>}
        </div>

        {msg.text && <MessageBlock text={msg.text} />}

        {msg.scam_reason && (
          <div>
            <h4 className="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Reason</h4>
            <p className="text-xs text-slate-400">{msg.scam_reason}</p>
          </div>
        )}

        {msg.scam_error && (
          <div className="rounded-lg bg-orange-500/10 border border-orange-500/30 p-2">
            <p className="text-xs text-orange-300">Last check error: {msg.scam_error}</p>
          </div>
        )}

        {trace.short_circuit && (
          <div className="rounded-lg bg-amber-500/10 border border-amber-500/30 p-2">
            <p className="text-xs text-amber-300">
              Short-circuited: {trace.short_circuit.match_type || trace.short_circuit.node_id} — {trace.short_circuit.matched_value || trace.short_circuit.condition}
            </p>
          </div>
        )}

        {trace.steps?.map((step: any, i: number) => (
          <div key={i} className="rounded-lg border border-slate-700 bg-slate-900 p-3 space-y-2">
            <div className="flex items-center gap-2">
              <span className={`text-[10px] font-medium ${
                step.status === 'completed' ? 'text-emerald-400' :
                step.status === 'failed' ? 'text-red-400' :
                'text-slate-500'
              }`}>
                {step.status === 'completed' ? '✓' : step.status === 'failed' ? '✗' : '—'}
              </span>
              <span className="text-xs font-medium text-slate-300">{step.node_id}</span>
              <span className="text-[10px] text-slate-500">{step.type}</span>
              {step.model_used && <span className="text-[10px] text-slate-500">{step.model_used}</span>}
              {step.duration_ms != null && <span className="text-[10px] text-slate-500 ml-auto">{step.duration_ms}ms</span>}
            </div>
            {step.model_warning && <p className="text-[10px] text-amber-400">{step.model_warning}</p>}
            {step.error && <p className="text-[10px] text-red-400">{step.error}</p>}
            {devMode && step.prompt_sent && <DevDetails label="Show prompt">{step.prompt_sent}</DevDetails>}
            {devMode && step.input && (
              <DevDetails label="Show input">{typeof step.input === 'string' ? step.input : JSON.stringify(step.input, null, 2)}</DevDetails>
            )}
            {step.tool_calls?.length > 0 && (
              <div className="space-y-1">
                {step.tool_calls.map((tc: any, tci: number) => (
                  <details key={tci} className="text-[10px]">
                    <summary className="text-indigo-400 cursor-pointer hover:text-indigo-300">
                      Tool: {tc.tool}({typeof tc.args === 'object' ? JSON.stringify(tc.args) : tc.args}) <span className="text-slate-500">{tc.duration_ms}ms</span>
                    </summary>
                    <pre className="mt-1 whitespace-pre-wrap text-[10px] text-slate-400 bg-slate-950 rounded p-2 max-h-32 overflow-y-auto border border-indigo-500/20">{JSON.stringify(tc.result, null, 2)}</pre>
                  </details>
                ))}
              </div>
            )}
            {devMode && step.raw_response && <DevDetails label="Show response" maxH="max-h-32">{step.raw_response}</DevDetails>}
            {devMode && step.output && (
              <DevDetails label="Show output" className="text-emerald-400/70" maxH="max-h-24">{JSON.stringify(step.output, null, 2)}</DevDetails>
            )}
          </div>
        ))}
      </div>
    )
  }

  if (msg.llm_prompt || msg.llm_response) {
    return (
      <div className="space-y-3 max-w-4xl">
        {msg.text && <MessageBlock text={msg.text} />}
        {msg.llm_prompt && (
          <div>
            <h4 className="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Prompt sent to LLM</h4>
            <pre className="whitespace-pre-wrap text-[11px] text-slate-400 bg-slate-900 rounded-lg p-3 border border-slate-700 max-h-48 overflow-y-auto">{msg.llm_prompt}</pre>
          </div>
        )}
        {msg.llm_response && (
          <div>
            <h4 className="text-[10px] uppercase tracking-wider text-slate-500 mb-1">LLM Response</h4>
            <pre className="whitespace-pre-wrap text-[11px] text-slate-400 bg-slate-900 rounded-lg p-3 border border-slate-700 max-h-32 overflow-y-auto">{msg.llm_response}</pre>
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="space-y-3 max-w-4xl">
      {msg.text && <MessageBlock text={msg.text} />}
      {msg.scam_error && <p className="text-xs text-orange-300">Last check error: {msg.scam_error}</p>}
      <p className="text-xs text-slate-500">No trace recorded for this message.</p>
    </div>
  )
}
