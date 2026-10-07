import { useState } from 'react'
import { GitBranch, RefreshCw, X } from 'lucide-react'
import { getScamPipeline, reloadScamPipeline } from '../../api/client'
import { useLatestQuery } from '../../hooks/useLatestQuery'
import { useAsyncAction } from '../../hooks/useAsyncAction'
import { errorMessage } from '../../lib/toast'
import { EmptyState, ErrorState, LoadingRow } from './shared'

interface PipelineData {
  version: string
  updated_at: string
  nodes: { id: string; type: string; config: Record<string, any>; short_circuit?: any; output_forward?: string[]; output_trace_only?: string[] }[]
  edges: { from: string; to: string }[]
  output: string
  tools?: { name: string; script: string; description?: string; script_params?: Record<string, any> }[]
}

/** Developer view of the scam pipeline DAG. */
export default function PipelineTab() {
  const { data, loading, error, refetch, setData } = useLatestQuery('scam-pipeline', () => getScamPipeline() as Promise<PipelineData>)
  const [selectedNode, setSelectedNode] = useState<string | null>(null)
  const reload = useAsyncAction(async () => {
    await reloadScamPipeline()
    setData(await getScamPipeline() as PipelineData)
  }, { errorContext: "Couldn't reload pipeline" })

  if (loading) return <LoadingRow />
  if (!data) {
    return error
      ? <ErrorState message={`Couldn't load pipeline: ${errorMessage(error)}`} onRetry={() => { void refetch() }} />
      : <EmptyState icon={<GitBranch size={32} />} title="Pipeline not loaded" />
  }
  const pipelineData = data

  return (
    <div className="p-4 space-y-4">
      {/* Pipeline header + DAG flow on one line */}
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs font-medium text-slate-300">Pipeline</span>
        <span className="rounded-full bg-cyan-500/20 px-1.5 py-0.5 text-[9px] font-medium text-cyan-300">v{pipelineData.version}</span>
        <button
          disabled={reload.pending}
          onClick={() => { void reload.run() }}
          className="ml-auto flex items-center gap-1 rounded-md bg-slate-700 px-2 py-1 text-[10px] text-slate-300 hover:bg-slate-600 disabled:opacity-40"
        >
          <RefreshCw size={10} className={reload.pending ? 'animate-spin' : ''} /> Reload from disk
        </button>
      </div>

      {/* DAG visualization with SVG edges */}
      {(() => {
        const predecessors: Record<string, string[]> = {}
        pipelineData.nodes.forEach(n => { predecessors[n.id] = [] })
        pipelineData.edges.forEach(e => {
          if (e.from !== '$input' && predecessors[e.to]) predecessors[e.to].push(e.from)
        })
        const nodeLayer: Record<string, number> = {}
        pipelineData.nodes.forEach(n => { if (predecessors[n.id].length === 0) nodeLayer[n.id] = 0 })
        let changed = true
        while (changed) {
          changed = false
          pipelineData.nodes.forEach(n => {
            if (nodeLayer[n.id] !== undefined) return
            const pl = predecessors[n.id].map(p => nodeLayer[p]).filter(l => l !== undefined)
            if (pl.length === predecessors[n.id].length) { nodeLayer[n.id] = Math.max(...pl) + 1; changed = true }
          })
        }
        const maxLayer = Math.max(...Object.values(nodeLayer), 0)
        const layers: string[][] = Array.from({length: maxLayer + 1}, () => [])
        Object.entries(nodeLayer).forEach(([nid, l]) => layers[l].push(nid))

        // Layout constants
        const nodeW = 120, nodeH = 40, layerGap = 80, nodeGap = 16, padX = 20, padY = 20
        const maxNodesInLayer = Math.max(...layers.map(l => l.length), 1)

        // Compute positions: allIds includes $input and $result
        const allIds = ['$input', ...pipelineData.nodes.map(n => n.id), '$result']
        const pos: Record<string, {x: number, y: number}> = {}
        const totalLayers = maxLayer + 3 // $input + node layers + $result
        const hasShortCircuit = pipelineData.nodes.some(n => n.short_circuit)
        const totalHeight = maxNodesInLayer * (nodeH + nodeGap) - nodeGap + padY * 2 + (hasShortCircuit ? 50 : 0)
        const totalWidth = totalLayers * (nodeW + layerGap) - layerGap + padX * 2

        // $input position
        const inputY = totalHeight / 2
        pos['$input'] = { x: padX + nodeW / 2, y: inputY }

        // Node positions
        layers.forEach((layerNodes, li) => {
          const x = padX + (li + 1) * (nodeW + layerGap) + nodeW / 2
          const layerH = layerNodes.length * (nodeH + nodeGap) - nodeGap
          const startY = (totalHeight - layerH) / 2 + nodeH / 2
          layerNodes.forEach((nid, ni) => {
            pos[nid] = { x, y: startY + ni * (nodeH + nodeGap) }
          })
        })

        // $result position
        pos['$result'] = { x: padX + (maxLayer + 2) * (nodeW + layerGap) + nodeW / 2, y: inputY }

        // Build edge list including $input→nodes and output→$result
        const svgEdges: {from: string, to: string}[] = []
        pipelineData.edges.forEach(e => {
          svgEdges.push({ from: e.from === '$input' ? '$input' : e.from, to: e.to })
        })
        svgEdges.push({ from: pipelineData.output, to: '$result' })

        return (
          <div className="overflow-x-auto">
            <svg width={totalWidth} height={totalHeight} className="block">
              <defs>
                <marker id="arrow" viewBox="0 0 10 6" refX="10" refY="3" markerWidth="8" markerHeight="6" orient="auto">
                  <path d="M0,0 L10,3 L0,6" fill="#475569" />
                </marker>
                <marker id="arrow-sc" viewBox="0 0 10 6" refX="10" refY="3" markerWidth="8" markerHeight="6" orient="auto">
                  <path d="M0,0 L10,3 L0,6" fill="#fbbf24" />
                </marker>
              </defs>

              {/* Edges */}
              {svgEdges.map((e, i) => {
                const from = pos[e.from]
                const to = pos[e.to]
                if (!from || !to) return null
                const x1 = from.x + nodeW / 2
                const x2 = to.x - nodeW / 2
                const midX = (x1 + x2) / 2
                return (
                  <path
                    key={i}
                    d={`M${x1},${from.y} C${midX},${from.y} ${midX},${to.y} ${x2},${to.y}`}
                    fill="none"
                    stroke="#475569"
                    strokeWidth={1.5}
                    markerEnd="url(#arrow)"
                  />
                )
              })}

              {/* $input node */}
              <g transform={`translate(${pos['$input'].x - nodeW/2}, ${pos['$input'].y - nodeH/2})`}>
                <rect width={nodeW} height={nodeH} rx={nodeH/2} fill="#1e293b" stroke="#475569" strokeWidth={1} />
                <text x={nodeW/2} y={nodeH/2 - 2} textAnchor="middle" dominantBaseline="middle" fill="#94a3b8" fontSize={10} fontWeight={500}>$input</text>
                <text x={nodeW/2} y={nodeH/2 + 10} textAnchor="middle" dominantBaseline="middle" fill="#64748b" fontSize={8}>Message</text>
              </g>

              {/* Short-circuit bypass arrows (dashed, routed below the graph) */}
              {pipelineData.nodes.map(node => {
                if (!node.short_circuit) return null
                const from = pos[node.id]
                const to = pos['$result']
                if (!from || !to) return null
                const x1 = from.x
                const y1 = from.y + nodeH / 2
                const x2 = to.x
                const y2 = to.y + nodeH / 2
                const bypassY = totalHeight - 12
                return (
                  <g key={`sc-${node.id}`}>
                    <path
                      d={`M${x1},${y1} L${x1},${bypassY - 8} Q${x1},${bypassY} ${x1 + 8},${bypassY} L${x2 - 8},${bypassY} Q${x2},${bypassY} ${x2},${bypassY - 8} L${x2},${y2}`}
                      fill="none" stroke="#fbbf24" strokeWidth={1.2} strokeDasharray="4,3"
                      markerEnd="url(#arrow-sc)"
                    />
                    <text x={(x1 + x2) / 2} y={bypassY - 4} textAnchor="middle" fill="#fbbf24" fontSize={7}>short-circuit</text>
                  </g>
                )
              })}

              {/* Pipeline nodes */}
              {pipelineData.nodes.map(node => {
                const p = pos[node.id]
                if (!p) return null
                const isSelected = selectedNode === node.id
                const isOutput = node.id === pipelineData.output
                const hasShortCircuit = !!node.short_circuit
                const strokeColor = isSelected ? '#22d3ee' : isOutput ? 'rgba(34,211,238,0.5)' : hasShortCircuit ? 'rgba(251,191,36,0.5)' : '#475569'
                const fillColor = isSelected ? 'rgba(34,211,238,0.1)' : isOutput ? 'rgba(34,211,238,0.07)' : '#1e293b'
                const typeColor = node.type === 'llm' ? '#818cf8' : node.type === 'script' ? '#fbbf24' : '#34d399'
                return (
                  <g
                    key={node.id}
                    transform={`translate(${p.x - nodeW/2}, ${p.y - nodeH/2})`}
                    onClick={() => setSelectedNode(isSelected ? null : node.id)}
                    className="cursor-pointer"
                  >
                    <rect width={nodeW} height={nodeH} rx={8} fill={fillColor} stroke={strokeColor} strokeWidth={isSelected ? 2 : 1.5} />
                    <text x={nodeW/2} y={hasShortCircuit ? nodeH/2 - 8 : nodeH/2 - 4} textAnchor="middle" dominantBaseline="middle" fill="#e2e8f0" fontSize={11} fontWeight={500}>{node.id}</text>
                    <text x={nodeW/2} y={hasShortCircuit ? nodeH/2 + 4 : nodeH/2 + 10} textAnchor="middle" dominantBaseline="middle" fill={typeColor} fontSize={9}>{node.type}</text>
                    {hasShortCircuit && (
                      <text x={nodeW/2} y={nodeH/2 + 16} textAnchor="middle" dominantBaseline="middle" fill="#fbbf24" fontSize={7}>⚡ short-circuit</text>
                    )}
                  </g>
                )
              })}

              {/* $result node */}
              <g transform={`translate(${pos['$result'].x - nodeW/2}, ${pos['$result'].y - nodeH/2})`}>
                <rect width={nodeW} height={nodeH} rx={nodeH/2} fill="rgba(16,185,129,0.07)" stroke="rgba(16,185,129,0.5)" strokeWidth={1} />
                <text x={nodeW/2} y={nodeH/2 + 1} textAnchor="middle" dominantBaseline="middle" fill="#34d399" fontSize={10} fontWeight={500}>result</text>
              </g>
            </svg>
          </div>
        )
      })()}

      {/* Selected node detail panel */}
      {selectedNode && (() => {
        const node = pipelineData.nodes.find(n => n.id === selectedNode)
        if (!node) return null
        return (
          <div className="rounded-lg border border-slate-700 bg-slate-800 p-4 space-y-3">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium text-slate-200">{node.id}</span>
              <span className={`rounded-full px-1.5 py-0.5 text-[9px] font-medium ${
                node.type === 'llm' ? 'bg-indigo-500/20 text-indigo-300' :
                node.type === 'script' ? 'bg-amber-500/20 text-amber-300' :
                'bg-emerald-500/20 text-emerald-300'
              }`}>{node.type}</span>
              {node.id === pipelineData.output && <span className="text-[10px] text-cyan-400">output node</span>}
              <button onClick={() => setSelectedNode(null)} className="ml-auto rounded p-1 text-slate-500 hover:bg-slate-700 hover:text-slate-300">
                <X size={13} />
              </button>
            </div>

            {/* Config fields */}
            <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[11px]">
              {node.config.model && <>
                <span className="text-slate-500">Model</span>
                <span className="text-slate-300">{node.config.model}</span>
              </>}
              {node.config.max_tokens && <>
                <span className="text-slate-500">Max tokens</span>
                <span className="text-slate-300">{node.config.max_tokens}</span>
              </>}
              {node.config.temperature != null && <>
                <span className="text-slate-500">Temperature</span>
                <span className="text-slate-300">{node.config.temperature}</span>
              </>}
              {node.config.script && <>
                <span className="text-slate-500">Script</span>
                <span className="text-slate-300 font-mono">{node.config.script}</span>
              </>}
              {node.config.few_shot_examples?.length > 0 && <>
                <span className="text-slate-500">Few-shot examples</span>
                <span className="text-slate-300">{node.config.few_shot_examples.length}</span>
              </>}
            </div>

            {/* Params — from node config.params OR tool definition's script_params */}
            {(() => {
              const nodeParams = node.config.params && Object.keys(node.config.params).length > 0 ? node.config.params : null
              const toolDef = pipelineData.tools?.find(t => t.name === node.config.script)
              const params = nodeParams || toolDef?.script_params
              if (!params || Object.keys(params).length === 0) return null
              return (
                <div>
                  <h4 className="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Parameters</h4>
                  <div className="rounded-lg bg-slate-900 border border-slate-700 p-3 space-y-1.5">
                    {Object.entries(params).map(([key, val]) => (
                      <div key={key} className="flex items-start gap-2">
                        <span className="text-[11px] text-slate-500 font-mono whitespace-nowrap">{key}:</span>
                        <span className="text-[11px] text-slate-300">
                          {Array.isArray(val) ? (
                            <span className="flex flex-wrap gap-1">
                              {(val as string[]).map((v, i) => (
                                <span key={i} className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] font-mono">{String(v)}</span>
                              ))}
                            </span>
                          ) : typeof val === 'number' ? (
                            <span className="font-mono">{val}</span>
                          ) : (
                            String(val)
                          )}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )
            })()}

            {/* Full prompt */}
            {node.config.prompt && (
              <div>
                <h4 className="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Prompt</h4>
                <pre className="whitespace-pre-wrap text-[11px] text-slate-400 bg-slate-900 rounded-lg p-3 border border-slate-700 max-h-64 overflow-y-auto">{node.config.prompt}</pre>
              </div>
            )}

            {/* Few-shot examples */}
            {node.config.few_shot_examples?.length > 0 && (
              <div>
                <h4 className="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Few-shot Examples</h4>
                <div className="space-y-2">
                  {node.config.few_shot_examples.map((ex: any, i: number) => (
                    <div key={i} className="rounded-lg bg-slate-900 border border-slate-700 p-2 space-y-1">
                      <div>
                        <span className="text-[9px] uppercase tracking-wider text-slate-500">Input</span>
                        <pre className="whitespace-pre-wrap text-[10px] text-slate-400 mt-0.5">{ex.input}</pre>
                      </div>
                      <div>
                        <span className="text-[9px] uppercase tracking-wider text-slate-500">Expected output</span>
                        <pre className="whitespace-pre-wrap text-[10px] text-emerald-400/70 mt-0.5">{ex.output}</pre>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Short-circuit */}
            {node.short_circuit && (
              <div>
                <h4 className="text-[10px] uppercase tracking-wider text-amber-400 mb-1">Short-circuit</h4>
                <div className="rounded-lg bg-slate-900 border border-amber-500/30 p-2 space-y-1">
                  <div>
                    <span className="text-[9px] text-slate-500">Condition</span>
                    <p className="text-[11px] text-amber-300 font-mono">{node.short_circuit.condition}</p>
                  </div>
                  <div>
                    <span className="text-[9px] text-slate-500">Result</span>
                    <pre className="text-[10px] text-slate-400 mt-0.5">{typeof node.short_circuit.result === 'string' ? node.short_circuit.result : JSON.stringify(node.short_circuit.result, null, 2)}</pre>
                  </div>
                </div>
              </div>
            )}

          </div>
        )
      })()}
    </div>
  )
}
