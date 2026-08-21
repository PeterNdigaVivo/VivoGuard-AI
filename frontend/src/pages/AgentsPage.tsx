// Autonomous monitoring agents — operator view of the 10 domain agents +
// watchdog. Shows the latest status per agent, lets an admin trigger a run,
// and expands to that agent's recent report history. Polls every 30s.
import { useEffect, useState, useCallback } from 'react'
import {
  fetchLatest, fetchReports, fetchScorecards, runAgent,
  type AgentLatest, type AgentReport, type AgentScorecard, type AgentStatus,
} from '@/api/agents'

const STATUS_STYLE: Record<string, string> = {
  ok:       'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  warning:  'bg-amber-500/15 text-amber-400 border-amber-500/30',
  critical: 'bg-rose-500/15 text-rose-400 border-rose-500/30',
  unknown:  'bg-slate-500/15 text-slate-400 border-slate-500/30',
}

function StatusPill({ status }: { status: AgentStatus | null }) {
  const s = status ?? 'unknown'
  const cls = STATUS_STYLE[s] ?? STATUS_STYLE.unknown
  return (
    <span className={`inline-block rounded border px-2 py-0.5 text-xs font-semibold uppercase tracking-wide ${cls}`}>
      {s}
    </span>
  )
}

function relTime(iso: string | null): string {
  if (!iso) return 'never'
  const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (secs < 90) return `${Math.round(secs)}s ago`
  if (secs < 5400) return `${Math.round(secs / 60)}m ago`
  if (secs < 172800) return `${Math.round(secs / 3600)}h ago`
  return `${Math.round(secs / 86400)}d ago`
}

function countKeys(obj: unknown): number {
  if (Array.isArray(obj)) return obj.length
  if (obj && typeof obj === 'object') return Object.keys(obj).length
  return 0
}

// The AI reasoning layer stores its diagnosis under findings.ai.
function aiSummary(rep: AgentReport | null | undefined): string | null {
  const f = rep?.findings
  if (f && typeof f === 'object' && 'ai' in f) {
    const ai = (f as Record<string, unknown>).ai
    if (ai && typeof ai === 'object' && 'summary' in ai) {
      const s = (ai as Record<string, unknown>).summary
      return typeof s === 'string' ? s : null
    }
  }
  return null
}

function aiRecommendations(rep: AgentReport | null | undefined): string[] {
  const a = rep?.actions_taken
  if (a && typeof a === 'object' && 'ai_recommendations' in a) {
    const recs = (a as Record<string, unknown>).ai_recommendations
    if (Array.isArray(recs)) return recs.filter((x): x is string => typeof x === 'string')
  }
  return []
}

export default function AgentsPage() {
  const [agents, setAgents] = useState<AgentLatest[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [history, setHistory] = useState<AgentReport[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [scorecards, setScorecards] = useState<Record<string, AgentScorecard>>({})

  const load = useCallback(async () => {
    try {
      const [{ agents }, { scorecards }] = await Promise.all([fetchLatest(), fetchScorecards()])
      setAgents(agents)
      setScorecards(Object.fromEntries(scorecards.map((card) => [card.agent_name, card])))
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load agents')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    const id = window.setInterval(load, 30_000)
    return () => window.clearInterval(id)
  }, [load])

  const openHistory = useCallback(async (name: string) => {
    setSelected(name)
    setHistory([])
    try {
      const { reports } = await fetchReports({ agent: name, limit: 50 })
      setHistory(reports)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load history')
    }
  }, [])

  const trigger = useCallback(async (name: string) => {
    setBusy(name)
    try {
      await runAgent(name)
      // Give the worker a moment, then refresh.
      window.setTimeout(load, 2000)
    } catch (e) {
      setError(e instanceof Error ? e.message : `Failed to run ${name}`)
    } finally {
      setBusy(null)
    }
  }, [load])

  const summary = agents.reduce(
    (acc, a) => {
      const s = a.report?.status ?? 'unknown'
      acc[s] = (acc[s] ?? 0) + 1
      return acc
    },
    {} as Record<string, number>,
  )

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="flex items-baseline justify-between mb-6">
        <div>
          <h1 className="text-2xl font-semibold">Monitoring Agents</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            Accountable monitoring agents, isolated simulation and watchdog. Auto-refreshing every 30s.
          </p>
        </div>
        <div className="flex gap-2 text-xs">
          <span className="text-emerald-600 dark:text-emerald-400">{summary.ok ?? 0} ok</span>
          <span className="text-amber-600 dark:text-amber-400">{summary.warning ?? 0} warning</span>
          <span className="text-rose-600 dark:text-rose-400">{summary.critical ?? 0} critical</span>
        </div>
      </div>

      {error && (
        <div className="mb-4 rounded border border-rose-500/30 bg-rose-500/10 px-4 py-2 text-sm text-rose-300">
          {error}
        </div>
      )}
      {loading && <p className="text-slate-500 dark:text-slate-400">Loading…</p>}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {agents.map((a) => {
          const rep = a.report
          const gaps = countKeys(rep?.gaps)
          const scorecard = scorecards[a.agent_name]
          return (
            <div
              key={a.agent_name}
              className="rounded-lg border border-slate-200 bg-white p-4 flex flex-col gap-2 cursor-pointer hover:border-slate-400 dark:border-slate-700 dark:bg-slate-900/50 dark:hover:border-slate-500"
              onClick={() => openHistory(a.agent_name)}
            >
              <div className="flex items-center justify-between">
                <span className="font-medium">{a.agent_name}</span>
                <StatusPill status={rep?.status ?? null} />
              </div>
              <div className="text-xs text-slate-500 dark:text-slate-400 flex justify-between">
                <span>last run {relTime(rep?.run_at ?? null)}</span>
                <span>{rep?.duration_ms != null ? `${rep.duration_ms}ms` : '—'}</span>
              </div>
              {scorecard && (
                <div className={`rounded px-2 py-1 text-xs ${scorecard.compliant ? 'bg-emerald-500/10 text-emerald-500' : 'bg-rose-500/10 text-rose-500'}`}>
                  SLA {scorecard.score.toFixed(1)}% · {scorecard.completed_runs}/{scorecard.expected_runs} runs
                  <div className="text-[11px] opacity-80">Owner: {scorecard.owner}</div>
                  {!scorecard.compliant && <div>{scorecard.breaches.join(' · ')}</div>}
                </div>
              )}
              {aiSummary(rep) && (
                <p className="text-xs text-slate-600 dark:text-slate-300 italic leading-snug">
                  🤖 {aiSummary(rep)}
                </p>
              )}
              <div className="text-xs text-slate-500">
                {gaps > 0 ? `${gaps} gap${gaps === 1 ? '' : 's'}` : 'no gaps'}
                {rep?.error_message ? ` · ${rep.error_message.slice(0, 60)}` : ''}
              </div>
              <button
                className="mt-1 self-start rounded bg-slate-200 text-slate-700 px-2 py-1 text-xs hover:bg-slate-300 disabled:opacity-50 dark:bg-slate-700 dark:text-slate-100 dark:hover:bg-slate-600"
                disabled={busy === a.agent_name}
                onClick={(e) => { e.stopPropagation(); trigger(a.agent_name) }}
              >
                {busy === a.agent_name ? 'Queuing…' : 'Run now'}
              </button>
            </div>
          )
        })}
      </div>

      {selected && (
        <div className="mt-8">
          <h2 className="text-lg font-semibold mb-3">
            {selected} — recent reports
          </h2>
          <div className="overflow-x-auto rounded-lg border border-slate-200 dark:border-slate-700">
            <table className="w-full text-sm">
              <thead className="bg-slate-100 text-left text-xs uppercase text-slate-500 dark:bg-slate-800/60 dark:text-slate-400">
                <tr>
                  <th className="px-3 py-2">Run at</th>
                  <th className="px-3 py-2">Status</th>
                  <th className="px-3 py-2">Duration</th>
                  <th className="px-3 py-2">AI analysis</th>
                  <th className="px-3 py-2">Gaps</th>
                </tr>
              </thead>
              <tbody>
                {history.map((r) => (
                  <tr key={r.id} className="border-t border-slate-200 dark:border-slate-800 align-top">
                    <td className="px-3 py-2 whitespace-nowrap text-slate-700 dark:text-slate-300">
                      {r.run_at ? new Date(r.run_at).toLocaleString() : '—'}
                    </td>
                    <td className="px-3 py-2"><StatusPill status={r.status} /></td>
                    <td className="px-3 py-2 text-slate-500 dark:text-slate-400">
                      {r.duration_ms != null ? `${r.duration_ms}ms` : '—'}
                    </td>
                    <td className="px-3 py-2 text-slate-700 dark:text-slate-300 max-w-md">
                      {aiSummary(r)
                        ? <p className="italic">🤖 {aiSummary(r)}</p>
                        : <span className="text-slate-500">rule-based (no LLM)</span>}
                      {aiRecommendations(r).length > 0 && (
                        <ul className="mt-1 list-disc pl-4 text-xs text-slate-500 dark:text-slate-400">
                          {aiRecommendations(r).map((rec, i) => <li key={i}>{rec}</li>)}
                        </ul>
                      )}
                    </td>
                    <td className="px-3 py-2 text-amber-600 dark:text-amber-300">
                      <pre className="whitespace-pre-wrap break-words max-w-xs text-xs">
                        {r.gaps ? JSON.stringify(r.gaps, null, 1) : '—'}
                      </pre>
                    </td>
                  </tr>
                ))}
                {history.length === 0 && (
                  <tr><td className="px-3 py-4 text-slate-500" colSpan={5}>No reports yet.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
