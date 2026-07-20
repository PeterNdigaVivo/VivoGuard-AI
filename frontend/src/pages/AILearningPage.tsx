// AI Learning Dashboard — single page powered by GET /training/dashboard.
// Read-only; never touches live inference. Polls every 60 s so the
// numbers move as operators triage alerts and the orchestrator runs.

import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge, Card, PageHeader } from '@/components/ui/Primitives'
import { api } from '@/api/client'

interface ReviewRow {
  detection_type: string
  correct: number
  false: number
  alerts_total: number
  review_rate: number | null
  false_rate: number | null
}
interface PoolRow {
  detection_type: string
  positives: number
  negatives: number
}
interface DriftPoint {
  detection_type: string
  day: string
  precision: number | null
  fp_rate: number | null
  alerts_total: number
}
interface ModelRow {
  id: number
  name: string
  version: string
  deployed: boolean
  is_chain: boolean
  detector_type: string | null
  classes: string[]
  map50: number | null
  created_at: string | null
}
interface JobRow {
  id: number
  model_name: string
  status: string
  current_epoch: number
  total_epochs: number
  best_map50: number | null
  incremental: boolean
  origin: string | null
  created_at: string | null
  completed_at: string | null
  error_message: string | null
}
interface Dashboard {
  window_days: number
  totals: {
    alerts_total: number; correct_total: number; false_total: number
    review_rate: number | null
  }
  review_breakdown: ReviewRow[]
  feedback_pools: PoolRow[]
  drift_series: DriftPoint[]
  model_registry: ModelRow[]
  recent_jobs: JobRow[]
}

export default function AILearningPage() {
  const [data, setData] = useState<Dashboard | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [days, setDays] = useState(14)
  const [busy, setBusy] = useState(false)

  async function load() {
    try {
      const d = await api<Dashboard>(`/training/dashboard?days=${days}`)
      setData(d); setErr(null)
    } catch (e) { setErr(String(e)) }
  }
  useEffect(() => {
    load()
    const t = setInterval(load, 60_000)
    return () => clearInterval(t)
     // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [days])

  async function runOrchestrator(dryRun: boolean) {
    setBusy(true)
    try {
      const r = await api(`/training/orchestrator/run?dry_run=${dryRun}`, { method: 'POST' })
      window.alert((dryRun ? 'Dry run: ' : 'Queued: ') + JSON.stringify(r, null, 2))
      load()
    } catch (e) {
      window.alert('Orchestrator failed: ' + e)
    } finally { setBusy(false) }
  }

  // Drift series → one chart series per detection_type.
  const driftByType = useMemo(() => {
    if (!data) return new Map<string, DriftPoint[]>()
    const m = new Map<string, DriftPoint[]>()
    for (const p of data.drift_series) {
      if (!m.has(p.detection_type)) m.set(p.detection_type, [])
      m.get(p.detection_type)!.push(p)
    }
    return m
  }, [data])

  return (
    <div className="space-y-3">
      <PageHeader title="🧠 AI Learning" />
      <p className="text-sm text-slate-500 dark:text-slate-300 -mt-4">
        Self-improving training pipeline — operator feedback in, better models out.
      </p>

      <div className="flex flex-wrap items-center gap-3 text-sm">
        <label className="text-slate-600 dark:text-slate-300">Window:</label>
        <select value={days} onChange={e => setDays(Number(e.target.value))}
                className="px-2 py-1 rounded border border-slate-300 dark:border-slate-800">
          <option value={7}>last 7 days</option>
          <option value={14}>last 14 days</option>
          <option value={30}>last 30 days</option>
          <option value={60}>last 60 days</option>
        </select>
        <button onClick={() => runOrchestrator(true)} disabled={busy}
                className="px-3 py-1 rounded bg-slate-100 dark:bg-slate-800 hover:bg-slate-200 dark:hover:bg-slate-700 disabled:opacity-50">
          Preview weekly orchestrator
        </button>
        <button onClick={() => runOrchestrator(false)} disabled={busy}
                className="px-3 py-1 rounded bg-emerald-100 text-emerald-800 hover:bg-emerald-200 disabled:opacity-50">
          ▶ Run weekly orchestrator
        </button>
        {/* Sprint entry point — gives operators a one-click path from
            "I see my training pool is too small" to the labelling UI. */}
        <Link to="/sprint"
              className="px-3 py-1 rounded bg-sky-100 text-sky-800 hover:bg-sky-200 ml-auto">
          🎯 Open labelling sprint →
        </Link>
      </div>

      {err && <Card className="p-3 text-rose-700">Failed to load: {err}</Card>}

      {data && (
        <>
          {/* Top-line counters */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            <Stat label="Alerts in window"   value={data.totals.alerts_total} />
            <Stat label="✅ Correct"          value={data.totals.correct_total}
                  tone="emerald" />
            <Stat label="❌ False"            value={data.totals.false_total}
                  tone="rose" />
            <Stat label="Review rate"        value={fmtPct(data.totals.review_rate)} />
          </div>

          {/* Per-detection-type review breakdown */}
          <Card className="p-3">
            <h3 className="font-semibold mb-2 text-slate-800 dark:text-slate-100">Reviews by alert type</h3>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-slate-500 dark:text-slate-300">
                  <th className="py-1">Detection type</th>
                  <th>Alerts</th><th>Correct</th><th>False</th>
                  <th>Review rate</th><th>False rate</th>
                </tr>
              </thead>
              <tbody>
                {data.review_breakdown.map(r => (
                  <tr key={r.detection_type} className="border-t border-slate-100 dark:border-slate-800">
                    <td className="py-1 font-medium">{r.detection_type}</td>
                    <td>{r.alerts_total}</td>
                    <td className="text-emerald-700">{r.correct}</td>
                    <td className="text-rose-700">{r.false}</td>
                    <td>{fmtPct(r.review_rate)}</td>
                    <td>{fmtPct(r.false_rate)}</td>
                  </tr>
                ))}
                {data.review_breakdown.length === 0 && (
                  <tr><td colSpan={6} className="py-2 text-slate-500 dark:text-slate-300">
                    No alerts in this window.</td></tr>
                )}
              </tbody>
            </table>
          </Card>

          {/* Feedback pool sizes */}
          <Card className="p-3">
            <h3 className="font-semibold mb-2 text-slate-800 dark:text-slate-100">
              Training pools (positives + hard negatives)
            </h3>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-slate-500 dark:text-slate-300">
                  <th className="py-1">Detection type</th>
                  <th>Positives</th><th>Hard negatives</th><th>Ratio</th>
                </tr>
              </thead>
              <tbody>
                {data.feedback_pools.map(p => {
                  const ratio = p.positives ? (p.negatives / p.positives).toFixed(2) : '∞'
                  return (
                    <tr key={p.detection_type} className="border-t border-slate-100 dark:border-slate-800">
                      <td className="py-1 font-medium">{p.detection_type}</td>
                      <td>{p.positives}</td><td>{p.negatives}</td>
                      <td>{ratio}</td>
                    </tr>
                  )
                })}
                {data.feedback_pools.length === 0 && (
                  <tr><td colSpan={4} className="py-2 text-slate-500 dark:text-slate-300">
                    No feedback pools yet. Mark some alerts ✅/❌ on the
                    Alerts page to start populating.</td></tr>
                )}
              </tbody>
            </table>
          </Card>

          {/* Drift series — sparkline-style table for now; replace with
              a real chart when the dashboard graduates. */}
          <Card className="p-3">
            <h3 className="font-semibold mb-2 text-slate-800 dark:text-slate-100">
              Daily precision / FP-rate ({data.window_days}d)
            </h3>
            {driftByType.size === 0 && (
              <div className="text-sm text-slate-500 dark:text-slate-300">
                No model metrics yet — they appear once alerts have been triaged.
              </div>
            )}
            <div className="space-y-2">
              {Array.from(driftByType.entries()).map(([t, pts]) => {
                const latest = pts[pts.length - 1]
                return (
                  <div key={t} className="text-sm">
                    <div className="flex items-center justify-between">
                      <span className="font-medium">{t}</span>
                      <span className="text-slate-500 dark:text-slate-300">
                        latest: {fmtPct(latest?.precision)} precision ·{' '}
                        {fmtPct(latest?.fp_rate)} FP rate ·{' '}
                        {latest?.alerts_total ?? 0} alerts
                      </span>
                    </div>
                    <Sparkline values={pts.map(p => p.precision ?? 0)} />
                  </div>
                )
              })}
            </div>
          </Card>

          {/* Model registry */}
          <Card className="p-3">
            <h3 className="font-semibold mb-2 text-slate-800 dark:text-slate-100">Model registry</h3>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-slate-500 dark:text-slate-300">
                  <th className="py-1">Name</th><th>Version</th>
                  <th>Status</th><th>Chain</th><th>mAP50</th><th>Created</th>
                </tr>
              </thead>
              <tbody>
                {data.model_registry.slice(0, 20).map(m => (
                  <tr key={m.id} className="border-t border-slate-100 dark:border-slate-800">
                    <td className="py-1 font-medium">{m.name}</td>
                    <td>{m.version}</td>
                    <td>{m.deployed
                      ? <Badge color="green">production</Badge>
                      : <Badge color="slate">candidate</Badge>}
                    </td>
                    <td>{m.is_chain ? '✓' : ''}</td>
                    <td>{m.map50 != null ? m.map50.toFixed(3) : '—'}</td>
                    <td className="text-slate-500 dark:text-slate-300">
                      {m.created_at ? new Date(m.created_at).toLocaleDateString() : ''}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>

          {/* Recent training jobs */}
          <Card className="p-3">
            <h3 className="font-semibold mb-2 text-slate-800 dark:text-slate-100">Recent training jobs</h3>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-slate-500 dark:text-slate-300">
                  <th className="py-1">Job</th><th>Model</th>
                  <th>Status</th><th>Mode</th>
                  <th>Epochs</th><th>Best mAP50</th>
                </tr>
              </thead>
              <tbody>
                {data.recent_jobs.map(j => (
                  <tr key={j.id} className="border-t border-slate-100 dark:border-slate-800">
                    <td className="py-1">#{j.id}</td>
                    <td>{j.model_name}</td>
                    <td>{j.status}</td>
                    <td>{j.incremental ? 'fine-tune' : 'full'}
                        {j.origin === 'weekly_orchestrator' ? ' · weekly' : ''}</td>
                    <td>{j.current_epoch}/{j.total_epochs}</td>
                    <td>{j.best_map50 != null ? j.best_map50.toFixed(3) : '—'}</td>
                  </tr>
                ))}
                {data.recent_jobs.length === 0 && (
                  <tr><td colSpan={6} className="py-2 text-slate-500 dark:text-slate-300">
                    No training jobs yet.</td></tr>
                )}
              </tbody>
            </table>
          </Card>
        </>
      )}
    </div>
  )
}


function Stat({ label, value, tone }: {
  label: string
  value: string | number
  tone?: 'emerald' | 'rose'
}) {
  const cls =
    tone === 'emerald' ? 'text-emerald-700' :
    tone === 'rose'    ? 'text-rose-700'    : 'text-slate-800 dark:text-slate-100'
  return (
    <Card className="p-3">
      <div className="text-xs text-slate-500 dark:text-slate-300">{label}</div>
      <div className={`text-2xl font-bold ${cls}`}>{value}</div>
    </Card>
  )
}


function fmtPct(v: number | null | undefined): string {
  if (v == null) return '—'
  return `${(v * 100).toFixed(1)}%`
}


function Sparkline({ values }: { values: number[] }) {
  if (!values.length) return null
  const w = 240, h = 28
  const min = Math.min(...values), max = Math.max(...values)
  const range = (max - min) || 1
  const pts = values.map((v, i) => {
    const x = (i / Math.max(1, values.length - 1)) * w
    const y = h - ((v - min) / range) * h
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
  return (
    <svg width={w} height={h} className="text-emerald-600">
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth={1.5} />
    </svg>
  )
}
