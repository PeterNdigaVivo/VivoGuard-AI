// AI Progress — mission-control view of how the AI is learning. Polls
// /training/progress every 60s and renders 7 sections (model curve, dataset
// health, per-type accuracy, feedback impact, next-training countdowns,
// simulation status, learning velocity). Dark-mode compatible; charts via
// recharts.
import { useEffect, useState } from 'react'
import {
  LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, ReferenceLine, Cell,
} from 'recharts'
import { Card, PageHeader, Skeleton } from '@/components/ui/Primitives'
import { labelForDetector } from '@/lib/detectorLabels'
import { api } from '@/api/client'

const GREEN = '#22c55e', AMBER = '#f59e0b', RED = '#ef4444', BLUE = '#3b82f6'
const AXIS = '#94a3b8'

interface Model {
  id: number; version: string; map50: number | null; precision: number | null
  recall: number | null; created_at: string | null; deployed: boolean
  detection_type: string | null
}
interface DatasetRow {
  name: string; detection_type: string | null; image_count: number
  positive_count: number; negative_count: number
  last_trained: string | null; suspended: boolean
}
interface Progress {
  models: Model[]
  datasets: DatasetRow[]
  feedback: {
    true_alerts_total: number; false_alerts_total: number; this_week: number
    by_detection_type: Record<string, { confirmed: number; dismissed: number }>
  }
  next_training: {
    detection_type: string; current_images: number; threshold: number
    progress_pct: number; images_needed: number
  }[]
  simulation: {
    last_run: string | null; crops_today: number; cameras_active: number
    staff_crops_total: number; customer_crops_total: number; pipeline_active: boolean
  }
  velocity: {
    images_per_day_7d: number; models_per_week: number
    avg_map50_improvement: number | null
    projected_uniform_accuracy_weeks: number | null
  }
}

function healthColor(n: number): string {
  return n > 200 ? GREEN : n >= 30 ? AMBER : RED
}
function mapColor(m: number | null): string {
  if (m == null) return AXIS
  return m > 0.8 ? GREEN : m >= 0.3 ? AMBER : RED
}
function relTime(iso: string | null): string {
  if (!iso) return 'never'
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 90) return `${Math.round(s)}s ago`
  if (s < 5400) return `${Math.round(s / 60)}m ago`
  if (s < 172800) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

export default function AIProgressPage() {
  const [data, setData] = useState<Progress | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    const load = () => api<Progress>('/training/progress')
      .then(d => { if (alive) { setData(d); setError(null) } })
      .catch(e => { if (alive) setError(String(e)) })
    load()
    const t = setInterval(load, 60_000)
    return () => { alive = false; clearInterval(t) }
  }, [])

  if (error) return (
    <div className="p-6"><PageHeader title="AI Progress" />
      <Card className="p-6 text-sm text-red-600 dark:bg-slate-900 dark:border-slate-800">
        Could not load progress. {error}</Card></div>
  )
  if (!data) return (
    <div className="p-6"><PageHeader title="AI Progress" />
      <div className="grid gap-4 md:grid-cols-2">
        {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-56" />)}
      </div></div>
  )

  const curve = data.models
    .filter(m => m.map50 != null)
    .map(m => ({ version: m.version, map50: m.map50, deployed: m.deployed }))
  const dsBars = [...data.datasets]
    .sort((a, b) => b.image_count - a.image_count)
    .map(d => ({ name: d.name.replace('feedback-', ''), count: d.image_count }))

  // Per-type accuracy: newest deployed-or-latest model per detection_type.
  const byType = new Map<string, Model>()
  for (const m of data.models) {
    const t = m.detection_type || 'other'
    const cur = byType.get(t)
    if (!cur || m.deployed || (m.id > cur.id && !cur.deployed)) byType.set(t, m)
  }

  const totalFeedback = data.feedback.true_alerts_total + data.feedback.false_alerts_total
  const modelsFromFeedback = data.models.filter(m => m.detection_type).length

  return (
    <div className="p-6 space-y-6">
      <PageHeader title="AI Progress" />
      <p className="-mt-4 text-sm text-slate-500 dark:text-slate-400">
        Mission control — how the AI is learning for you. Auto-refreshing every 60s.
      </p>

      {/* SECTION 1 — model learning curve */}
      <Card className="p-4 dark:bg-slate-900 dark:border-slate-800">
        <div className="font-semibold text-slate-800 dark:text-slate-100">AI Model Learning Curve</div>
        <div className="text-xs text-slate-500 dark:text-slate-400 mb-3">
          How the AI improved over {data.models.length} training runs
        </div>
        <ResponsiveContainer width="100%" height={260}>
          <LineChart data={curve} margin={{ top: 8, right: 16, bottom: 8, left: -8 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#334155" opacity={0.3} />
            <XAxis dataKey="version" tick={{ fill: AXIS, fontSize: 11 }} />
            <YAxis domain={[0, 1]} tick={{ fill: AXIS, fontSize: 11 }} />
            <Tooltip contentStyle={{ background: '#0f172a', border: '1px solid #334155', color: '#e2e8f0' }} />
            <Line type="monotone" dataKey="map50" stroke={BLUE} strokeWidth={2}
              dot={(p: any) => {
                const filled = curve[p.index]?.deployed
                return <circle key={p.index} cx={p.cx} cy={p.cy} r={4}
                  fill={filled ? BLUE : '#0f172a'} stroke={BLUE} strokeWidth={2} />
              }} />
          </LineChart>
        </ResponsiveContainer>
      </Card>

      {/* SECTION 2 — dataset health */}
      <Card className="p-4 dark:bg-slate-900 dark:border-slate-800">
        <div className="font-semibold text-slate-800 dark:text-slate-100 mb-3">Training Data by Detector</div>
        <ResponsiveContainer width="100%" height={Math.max(160, dsBars.length * 34)}>
          <BarChart data={dsBars} layout="vertical" margin={{ left: 24, right: 24 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#334155" opacity={0.3} />
            <XAxis type="number" tick={{ fill: AXIS, fontSize: 11 }} />
            <YAxis type="category" dataKey="name" width={140} tick={{ fill: AXIS, fontSize: 11 }} />
            <Tooltip contentStyle={{ background: '#0f172a', border: '1px solid #334155', color: '#e2e8f0' }} />
            <ReferenceLine x={30} stroke={AMBER} strokeDasharray="4 4" label={{ value: 'min 30', fill: AMBER, fontSize: 10 }} />
            <ReferenceLine x={200} stroke={GREEN} strokeDasharray="4 4" label={{ value: 'target 200', fill: GREEN, fontSize: 10 }} />
            <Bar dataKey="count" radius={[0, 4, 4, 0]}>
              {dsBars.map((d, i) => <Cell key={i} fill={healthColor(d.count)} />)}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </Card>

      {/* SECTION 3 — accuracy by type */}
      <Card className="p-4 dark:bg-slate-900 dark:border-slate-800">
        <div className="font-semibold text-slate-800 dark:text-slate-100 mb-3">Detection Accuracy by Type</div>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {[...byType.entries()].map(([t, m]) => {
            const status = (m.map50 ?? 0) > 0.8 ? ['🟢', 'Trained']
              : (m.map50 ?? 0) >= 0.3 ? ['🟡', 'Learning'] : ['🔴', 'No Data']
            return (
              <div key={t} className="rounded-lg border border-slate-200 dark:border-slate-700 p-3">
                <div className="flex items-center justify-between">
                  <span className="font-medium text-slate-800 dark:text-slate-100">{labelForDetector(t)}</span>
                  <span className="text-xs font-bold px-2 py-0.5 rounded-full text-white"
                    style={{ background: mapColor(m.map50) }}>
                    {m.map50 != null ? m.map50.toFixed(3) : '—'}
                  </span>
                </div>
                <div className="text-xs text-slate-500 dark:text-slate-400 mt-1">
                  P {m.precision != null ? m.precision.toFixed(2) : '—'} ·
                  R {m.recall != null ? m.recall.toFixed(2) : '—'}
                </div>
                <div className="text-xs mt-1 text-slate-600 dark:text-slate-300">{status[0]} {status[1]}</div>
              </div>
            )
          })}
        </div>
      </Card>

      {/* SECTION 4 — feedback impact */}
      <Card className="p-4 dark:bg-slate-900 dark:border-slate-800">
        <div className="font-semibold text-slate-800 dark:text-slate-100 mb-3">Operator Feedback Impact</div>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <Kpi label="Total feedback clicks" value={totalFeedback} color={BLUE} />
          <Kpi label="True alerts confirmed" value={data.feedback.true_alerts_total} color={GREEN} />
          <Kpi label="False alerts dismissed" value={data.feedback.false_alerts_total} color={AMBER} />
          <Kpi label="Models trained" value={modelsFromFeedback} color={BLUE} />
        </div>
        <div className="text-sm text-slate-600 dark:text-slate-300 mt-3">
          Your feedback has trained <strong>{modelsFromFeedback}</strong> models and
          {' '}contributed <strong>{data.feedback.this_week}</strong> labels this week.
        </div>
      </Card>

      {/* SECTION 5 — next training */}
      <Card className="p-4 dark:bg-slate-900 dark:border-slate-800">
        <div className="font-semibold text-slate-800 dark:text-slate-100 mb-3">Next Training Countdown</div>
        <div className="space-y-3">
          {data.next_training.map(n => {
            const critical = n.detection_type === 'uniform_compliance' && n.current_images < 15
            return (
              <div key={n.detection_type}>
                <div className={'text-sm flex justify-between ' +
                  (critical ? 'text-red-600 dark:text-red-400 font-semibold'
                            : 'text-slate-700 dark:text-slate-200')}>
                  <span>{labelForDetector(n.detection_type)}: {n.current_images}/{n.threshold} images</span>
                  <span>{n.images_needed > 0
                    ? `${n.images_needed} more clicks triggers training`
                    : 'ready to train'}</span>
                </div>
                <div className="mt-1 h-2 rounded-full bg-slate-100 dark:bg-slate-700 overflow-hidden">
                  <div className="h-full rounded-full"
                    style={{ width: `${n.progress_pct}%`, background: critical ? RED : BLUE }} />
                </div>
              </div>
            )
          })}
        </div>
      </Card>

      {/* SECTION 6 — simulation */}
      <Card className="p-4 dark:bg-slate-900 dark:border-slate-800">
        <div className="font-semibold text-slate-800 dark:text-slate-100 mb-2">Simulation Pipeline</div>
        {data.simulation.pipeline_active ? (
          <>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 text-sm">
              <Kpi label="Staff uniform crops" value={data.simulation.staff_crops_total} color={GREEN} />
              <Kpi label="Customer crops" value={data.simulation.customer_crops_total} color={BLUE} />
              <Kpi label="Cameras mined today" value={data.simulation.cameras_active} color={BLUE} />
              <Kpi label="Crops today" value={data.simulation.crops_today} color={GREEN} />
            </div>
            <div className="text-xs text-slate-500 dark:text-slate-400 mt-2">
              Last mined {relTime(data.simulation.last_run)} · the AI is automatically
              learning uniforms from your live cameras.
            </div>
          </>
        ) : (
          <div className="text-sm text-slate-600 dark:text-slate-300">
            🛰️ The crop-mining pipeline hasn't produced any crops yet — staff and
            customer training crops will appear here after the first 2-hourly run.
          </div>
        )}
      </Card>

      {/* SECTION 7 — velocity */}
      <Card className="p-4 dark:bg-slate-900 dark:border-slate-800">
        <div className="font-semibold text-slate-800 dark:text-slate-100 mb-3">Learning Velocity</div>
        <div className="grid grid-cols-3 gap-3">
          <Kpi label="Images / day (7d avg)" value={data.velocity.images_per_day_7d} color={BLUE} />
          <Kpi label="Models this week" value={data.velocity.models_per_week} color={GREEN} />
          <Kpi label="Avg map50 / run"
            value={data.velocity.avg_map50_improvement != null
              ? `+${data.velocity.avg_map50_improvement.toFixed(3)}` : '—'} color={GREEN} small />
        </div>
        <div className="mt-3 rounded-lg px-3 py-2.5 text-sm bg-blue-50 text-blue-800 dark:bg-slate-800 dark:text-slate-200">
          {data.velocity.projected_uniform_accuracy_weeks != null
            ? <>📈 At the current rate, uniform detection reaches <strong>90% accuracy in
                ~{data.velocity.projected_uniform_accuracy_weeks} weeks</strong>.</>
            : <>Not enough training history yet to project uniform-detection accuracy.</>}
        </div>
      </Card>
    </div>
  )
}

function Kpi({ label, value, color, small }: {
  label: string; value: number | string; color: string; small?: boolean
}) {
  return (
    <div className="rounded-lg border border-slate-200 dark:border-slate-700 p-3">
      <div className="text-[11px] uppercase tracking-wide text-slate-400">{label}</div>
      <div className={(small ? 'text-lg' : 'text-2xl') + ' font-bold mt-1'} style={{ color }}>
        {value}
      </div>
    </div>
  )
}
