// Per-store dashboard — Rule 3/4 redesign.
//
//   Top bar:    name, country, status-light pill, last update timestamp
//   Right now:  occupancy now, queue now, status
//   Today:      unique visitors (w/ trend), peak occupancy, staff %,
//               in/out/net visitors (entry_exit only),
//               top 3 aisles by dwell
//   Charts:     hourly footfall sparkline, alerts-by-type bar chart
//   Heatmap:    composite thumbnail; clicks through to the heatmap page

import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  Badge, Card, PageHeader, Skeleton, StatusLight, Trend,
} from '@/components/ui/Primitives'
import { api } from '@/api/client'

interface LiveResponse {
  store_id: number
  store_name: string
  country: string
  status: 'live' | 'no_data_yet' | 'no_cameras'
  status_light?: 'green' | 'amber' | 'red'
  as_of: string
  camera_count?: number
  zone_capabilities: Record<string, boolean>
  tiles: Record<string, { value: any; visible: boolean; trend?: { direction: 'up'|'down'|'flat'; delta_pct: number | null } }>
}

export default function StoreDashboardPage() {
  const { id } = useParams()
  const storeId = Number(id)
  const [data, setData] = useState<LiveResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    const tick = () =>
      api<LiveResponse>(`/analytics/store/${storeId}/live`)
        .then(d => { if (alive) { setData(d); setError(null); setLoading(false) } })
        .catch(e => { if (alive) { setError(String(e)); setLoading(false) } })
    tick()
    const t = setInterval(tick, 60_000)
    return () => { alive = false; clearInterval(t) }
  }, [storeId])

  if (loading && !data) return <DashboardSkeleton />
  if (error)  return <div className="p-6 text-red-600">Error: {error}</div>
  if (!data)  return <DashboardSkeleton />

  if (data.status === 'no_cameras') {
    return (
      <div className="p-6">
        <PageHeader title={data.store_name} />
        <Card className="p-8 text-center text-slate-500">
          No cameras attached to this store yet.{' '}
          <Link to="/cameras" className="text-sky-600 underline">Attach one</Link>{' '}
          to start collecting data.
        </Card>
      </div>
    )
  }

  if (data.status === 'no_data_yet') {
    return (
      <div className="p-6">
        <PageHeader title={data.store_name} />
        <Card className="p-8 text-center text-slate-500">
          Cameras just attached — collecting first measurements.
          Numbers appear within a minute or two. Refresh shortly.
        </Card>
      </div>
    )
  }

  const t = data.tiles
  return (
    <div className="p-6 space-y-6">
      <PageHeader
        title={data.store_name}
        actions={
          <div className="flex items-center gap-3">
            {data.status_light && <StatusLight value={data.status_light} />}
            <span className="text-xs text-slate-500">
              updated {new Date(data.as_of).toLocaleTimeString()}
            </span>
          </div>
        }
      />

      {/* RIGHT NOW */}
      <section>
        <SectionTitle>Right now</SectionTitle>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Kpi label="People in store" big value={fmtInt(t.occupancy_now?.value)} />
          {t.queue_length_now?.visible && (
            <Kpi label="People in queue" big value={fmtInt(t.queue_length_now.value)} />
          )}
          {t.staff_present_pct_today?.visible && (
            <Kpi label="Staff present today" big value={`${fmtInt(t.staff_present_pct_today.value)}%`} />
          )}
          <Kpi label="Cameras live" value={String(data.camera_count ?? 0)} />
        </div>
      </section>

      {/* TODAY SO FAR */}
      <section>
        <SectionTitle>Today so far</SectionTitle>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Kpi label="Unique visitors today"
               value={fmtInt(t.unique_visitors_today?.value)}
               trendDir={t.unique_visitors_today?.trend?.direction}
               trendPct={t.unique_visitors_today?.trend?.delta_pct ?? null} />
          <Kpi label="Peak occupancy today" value={fmtInt(t.occupancy_peak_today?.value)} />
          {t.queue_wait_avg_today_sec?.visible && (
            <Kpi label="Avg queue wait today"
                 value={`${fmtInt(t.queue_wait_avg_today_sec.value)} sec`} />
          )}
          {t.visitors_net_today?.visible && (
            <Kpi label="Net visitors (in − out)"
                 value={fmtInt(t.visitors_net_today.value)}
                 sub={`${fmtInt(t.visitors_in_today?.value)} in · ${fmtInt(t.visitors_out_today?.value)} out`} />
          )}
        </div>
      </section>

      {/* AISLES + ALERTS + FOOTFALL */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {t.top_aisles?.visible && (
          <Card className="p-4">
            <SectionTitle>Top aisles by dwell time</SectionTitle>
            {(t.top_aisles.value as any[]).length === 0 && (
              <div className="text-slate-400 text-sm">No dwell data yet today.</div>
            )}
            {(t.top_aisles.value as any[]).map((a, i) => (
              <div key={a.zone_id} className="flex items-center justify-between py-1">
                <span className="text-sm">{i + 1}. {a.zone_name}</span>
                <Badge color="sky">{a.avg_dwell_seconds}s</Badge>
              </div>
            ))}
          </Card>
        )}

        <Card className="p-4">
          <SectionTitle>Alerts today by type</SectionTitle>
          <AlertsByType data={(t.alerts_today_by_type?.value as Record<string, number>) || {}} />
        </Card>

        <Card className="p-4">
          <SectionTitle>Hourly footfall today</SectionTitle>
          <Sparkline points={(t.hourly_footfall_today?.value as { hour: number; value: number }[]) || []} />
        </Card>
      </div>

      {/* HEATMAP */}
      {t.heatmap_thumb_url?.visible && (
        <section>
          <SectionTitle>Footfall heatmap</SectionTitle>
          <Card className="p-3">
            <Link to={`/heatmaps/${storeId}`} className="block">
              <img src={t.heatmap_thumb_url.value as string} alt=""
                   onError={(e) => ((e.target as HTMLImageElement).style.display = 'none')}
                   className="w-full max-w-xl rounded border" />
              <div className="text-xs text-slate-500 mt-1">Click to expand · all cameras</div>
            </Link>
          </Card>
        </section>
      )}
    </div>
  )
}

// ----- small bits -----

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h2 className="text-sm font-semibold text-slate-500 uppercase tracking-wide mb-2">{children}</h2>
}

function Kpi({ label, value, big, sub, trendDir, trendPct }: {
  label: string; value: string; big?: boolean; sub?: string
  trendDir?: 'up' | 'down' | 'flat'; trendPct?: number | null
}) {
  return (
    <Card className="p-4">
      <div className="text-xs text-slate-500">{label}</div>
      <div className={(big ? 'text-3xl' : 'text-2xl') + ' font-semibold mt-1'}>{value}</div>
      {sub && <div className="text-xs text-slate-500 mt-1">{sub}</div>}
      {trendDir && trendPct !== undefined && (
        <div className="mt-1"><Trend direction={trendDir} deltaPct={trendPct} /></div>
      )}
    </Card>
  )
}

function AlertsByType({ data }: { data: Record<string, number> }) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1])
  if (entries.length === 0) {
    return <div className="text-slate-400 text-sm">No alerts today.</div>
  }
  const max = Math.max(...entries.map(e => e[1]))
  return (
    <div className="space-y-1">
      {entries.map(([k, v]) => (
        <div key={k} className="flex items-center gap-2 text-sm">
          <div className="w-32 truncate">{k}</div>
          <div className="flex-1 bg-slate-100 rounded h-4 relative">
            <div className="absolute inset-y-0 left-0 bg-sky-500 rounded"
                 style={{ width: `${(v / max) * 100}%` }} />
          </div>
          <div className="w-8 text-right">{v}</div>
        </div>
      ))}
    </div>
  )
}

function Sparkline({ points }: { points: { hour: number; value: number }[] }) {
  if (points.length < 2) {
    return <div className="text-slate-400 text-sm">Not enough data yet.</div>
  }
  const W = 320, H = 80
  const max = Math.max(...points.map(p => p.value), 1)
  const path = points.map((p, i) => {
    const x = (i / (points.length - 1)) * W
    const y = H - (p.value / max) * H
    return `${i === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${y.toFixed(1)}`
  }).join(' ')
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-24">
      <path d={path} stroke="#0284c7" strokeWidth={2} fill="none" />
      {points.map((p, i) => {
        const x = (i / (points.length - 1)) * W
        const y = H - (p.value / max) * H
        return <circle key={i} cx={x} cy={y} r={1.5} fill="#0284c7" />
      })}
    </svg>
  )
}

function DashboardSkeleton() {
  return (
    <div className="p-6 space-y-4">
      <Skeleton className="h-8 w-1/3" />
      <div className="grid grid-cols-4 gap-3">
        {[1,2,3,4].map(i => <Skeleton key={i} className="h-24" />)}
      </div>
      <Skeleton className="h-32" />
      <div className="grid grid-cols-3 gap-3">
        {[1,2,3].map(i => <Skeleton key={i} className="h-40" />)}
      </div>
    </div>
  )
}

function fmtInt(v: any): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '0'
  return String(Math.round(Number(v) || 0))
}
