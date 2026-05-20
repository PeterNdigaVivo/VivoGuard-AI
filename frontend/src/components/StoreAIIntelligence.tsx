// AI intelligence — compact panels for the store dashboard.
// One file, four self-fetching components. All auto-refresh every 60s.

import { useEffect, useState } from 'react'
import { Card } from '@/components/ui/Primitives'
import { api } from '@/api/client'


// ----- AnomaliesPanel -------------------------------------------------

interface AnomaliesPayload {
  anomalies: { headline: string; hour: number; z_score: number }[]
  note: string
}

export function AnomaliesPanel({ storeId }: { storeId: number }) {
  const [data, setData] = useState<AnomaliesPayload | null>(null)
  useEffect(() => {
    const tick = () => api<AnomaliesPayload>(`/analytics/store/${storeId}/anomalies`)
      .then(setData).catch(() => setData(null))
    tick()
    const t = setInterval(tick, 60_000)
    return () => clearInterval(t)
  }, [storeId])
  if (!data || data.anomalies.length === 0) return null
  return (
    <Card className="p-4 bg-amber-50 border-amber-200">
      <div className="text-sm font-semibold text-amber-900 mb-2">
        ⚠️ Anomalies detected today
      </div>
      <div className="space-y-1 text-xs text-amber-900">
        {data.anomalies.slice(0, 5).map((a, i) => (
          <div key={i}>{a.headline}</div>
        ))}
      </div>
    </Card>
  )
}


// ----- StaffingPredictionPanel ---------------------------------------

interface StaffRec {
  weekday: string; hour: number; expected_visitors: number
  recommended_staff: number; headline: string
}

export function StaffingPredictionPanel({ storeId }: { storeId: number }) {
  const [data, setData] = useState<{ recommendations: StaffRec[]; note?: string } | null>(null)
  useEffect(() => {
    api<typeof data>(`/analytics/store/${storeId}/staffing-prediction`)
      .then(d => setData(d as any)).catch(() => setData(null))
  }, [storeId])
  if (!data) return null
  // Pick the top 5 busiest predicted slots by visitor count.
  const top = (data.recommendations || [])
    .slice()
    .sort((a, b) => b.expected_visitors - a.expected_visitors)
    .slice(0, 5)
  return (
    <Card className="p-4">
      <div className="text-sm font-semibold text-slate-700 mb-2">
        🧠 Predictive staffing — busiest upcoming slots
      </div>
      {top.length === 0 ? (
        <div className="text-xs text-slate-500">{data.note ?? 'Not enough data yet.'}</div>
      ) : (
        <ul className="text-xs text-slate-700 space-y-1">
          {top.map((r, i) => (
            <li key={i} className="flex items-center gap-2">
              <span className="text-sky-500">›</span>
              <span className="flex-1">{r.headline}</span>
              <span className="text-slate-500 tabular-nums">{r.expected_visitors} visitors</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}


// ----- HealthScorePanel -----------------------------------------------

interface HealthPayload {
  score: number | null
  last_week_score: number
  delta: number
  headline: string
  components: { name: string; weight: number; value: number }[]
}

export function HealthScorePanel({ storeId }: { storeId: number }) {
  const [data, setData] = useState<HealthPayload | null>(null)
  useEffect(() => {
    const tick = () => api<HealthPayload>(`/analytics/store/${storeId}/health-score`)
      .then(setData).catch(() => setData(null))
    tick()
    const t = setInterval(tick, 60_000)
    return () => clearInterval(t)
  }, [storeId])
  if (!data || data.score === null) return null
  const rag = data.score >= 80 ? 'emerald' : data.score >= 60 ? 'amber' : 'red'
  return (
    <Card className="p-4">
      <div className="flex items-baseline gap-3">
        <div className={'text-4xl font-semibold ' +
          (rag === 'emerald' ? 'text-emerald-600' :
           rag === 'amber'   ? 'text-amber-600' : 'text-red-600')}>
          {data.score}<span className="text-base text-slate-400">/100</span>
        </div>
        <div className="text-sm text-slate-600">
          Store health score{' '}
          <span className={data.delta > 0 ? 'text-emerald-600' :
                            data.delta < 0 ? 'text-red-600' : 'text-slate-400'}>
            ({data.delta > 0 ? '↑' : data.delta < 0 ? '↓' : '→'}{' '}
             from {data.last_week_score} last week)
          </span>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1 text-xs text-slate-600">
        {data.components.map(c => (
          <div key={c.name} className="flex items-center gap-2">
            <span className="w-28 truncate">{c.name}</span>
            <div className="flex-1 bg-slate-100 rounded h-1.5">
              <div className="h-1.5 bg-sky-500 rounded" style={{ width: `${c.value}%` }} />
            </div>
            <span className="w-8 text-right tabular-nums">{Math.round(c.value)}</span>
            <span className="w-8 text-right text-slate-400">×{c.weight}%</span>
          </div>
        ))}
      </div>
    </Card>
  )
}


// ----- LossPreventionPanel -------------------------------------------

interface LPPayload {
  total_alerts_this_week: number
  peak_hour: { hour: number; count: number } | null
  hotspots: { camera_name: string; count: number }[]
  headline_time: string | null
  headline_camera: string | null
  headline_staff: string | null
}

export function LossPreventionPanel({ storeId }: { storeId: number }) {
  const [data, setData] = useState<LPPayload | null>(null)
  useEffect(() => {
    api<LPPayload>(`/analytics/store/${storeId}/loss-prevention`)
      .then(setData).catch(() => setData(null))
  }, [storeId])
  if (!data || data.total_alerts_this_week === 0) return null
  return (
    <Card className="p-4">
      <div className="text-sm font-semibold text-slate-700 mb-2">
        🕵️ Loss prevention intelligence — last 7 days
      </div>
      <div className="text-xs text-slate-700 space-y-1">
        <div><strong>{data.total_alerts_this_week}</strong> shrinkage / loitering / weapon alerts this week.</div>
        {data.headline_time && <div>⏰ {data.headline_time}</div>}
        {data.headline_camera && <div>📍 {data.headline_camera}</div>}
        {data.headline_staff && <div>👥 {data.headline_staff}</div>}
      </div>
    </Card>
  )
}


// ----- BehaviourTrendsPanel ------------------------------------------

interface TrendsPayload {
  avg_visit_seconds: { this_week: number; last_week: number; delta_pct: number | null }
  top_zones_this_week: { zone: string; visits: number }[]
  conversion_funnel: { this_week_pct: number; last_week_pct: number; headline: string }
}

export function BehaviourTrendsPanel({ storeId }: { storeId: number }) {
  const [data, setData] = useState<TrendsPayload | null>(null)
  useEffect(() => {
    api<TrendsPayload>(`/analytics/store/${storeId}/behaviour-trends`)
      .then(setData).catch(() => setData(null))
  }, [storeId])
  if (!data) return null
  const v = data.avg_visit_seconds
  return (
    <Card className="p-4">
      <div className="text-sm font-semibold text-slate-700 mb-2">
        📈 Customer behaviour trends — this week
      </div>
      <div className="text-xs text-slate-700 space-y-1">
        <div>
          Avg visit duration:{' '}
          <strong>{Math.floor((v.this_week || 0) / 60)}m {(v.this_week || 0) % 60}s</strong>
          {v.delta_pct !== null && (
            <span className={(v.delta_pct ?? 0) > 0 ? ' text-emerald-600' :
                              (v.delta_pct ?? 0) < 0 ? ' text-red-600' : ' text-slate-400'}>
              {' '}({(v.delta_pct ?? 0) > 0 ? '↑' : (v.delta_pct ?? 0) < 0 ? '↓' : '→'}{' '}
              {Math.abs(v.delta_pct ?? 0)}% vs last week)
            </span>
          )}
        </div>
        <div>{data.conversion_funnel.headline}</div>
        {data.top_zones_this_week.length > 0 && (
          <div>
            Top zones:{' '}
            {data.top_zones_this_week.slice(0, 3).map((z, i) => (
              <span key={z.zone}>{i > 0 ? ' · ' : ''}<strong>{z.zone}</strong> ({z.visits})</span>
            ))}
          </div>
        )}
      </div>
    </Card>
  )
}


// ----- bundle for the dashboard --------------------------------------

export default function StoreAIIntelligence({ storeId }: { storeId: number }) {
  return (
    <div className="space-y-3">
      <AnomaliesPanel storeId={storeId} />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        <HealthScorePanel storeId={storeId} />
        <StaffingPredictionPanel storeId={storeId} />
        <LossPreventionPanel storeId={storeId} />
        <BehaviourTrendsPanel storeId={storeId} />
      </div>
    </div>
  )
}
