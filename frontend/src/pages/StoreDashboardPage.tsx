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
import DateRangePicker, { rangeFor, type DateRange } from '@/components/DateRangePicker'
import { api } from '@/api/client'
import { labelForDetector } from '@/lib/detectorLabels'

interface LiveResponse {
  store_id: number
  store_name: string
  country: string
  // May-2026 redesign: 'closed' (outside business hours) and
  // 'no_cameras_live' (cameras attached but none streaming) are
  // distinct from 'no_cameras' (nothing attached yet). The UI shows
  // different messaging for each — never zero KPIs in any of them.
  status: 'live' | 'no_data_yet' | 'no_cameras' | 'no_cameras_live' | 'closed'
  status_light?: 'green' | 'amber' | 'red'
  as_of: string
  camera_count?: number
  cameras_total?: number
  cameras_online?: number
  is_open?: boolean
  hours_label?: string
  zone_capabilities: Record<string, boolean>
  tiles: Record<string, { value: any; visible: boolean; trend?: { direction: 'up'|'down'|'flat'; delta_pct: number | null } }>
}

// 30s auto-refresh — the spec asks every tile to refresh without a
// page reload. We keep timing in one place so the freshness pill
// matches the actual refresh cadence.
const REFRESH_INTERVAL_MS = 30_000

export default function StoreDashboardPage() {
  const { id } = useParams()
  const storeId = Number(id)
  const [data, setData] = useState<LiveResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [range, setRange] = useState<DateRange>(() => rangeFor('today'))
  // Wall-clock time of the last successful refresh — drives the
  // "Last updated: Xs ago" pill via a 1Hz re-render below.
  const [lastFetchedAt, setLastFetchedAt] = useState<number | null>(null)
  const [, forceTick] = useState(0)

  useEffect(() => {
    let alive = true
    const tick = () => {
      const q = new URLSearchParams({ since: range.since, until: range.until })
      api<LiveResponse>(`/analytics/store/${storeId}/live?${q}`)
        .then(d => {
          if (alive) {
            setData(d); setError(null); setLoading(false)
            setLastFetchedAt(Date.now())
          }
        })
        .catch(e => { if (alive) { setError(String(e)); setLoading(false) } })
    }
    tick()
    const t = setInterval(tick, REFRESH_INTERVAL_MS)
    // Force a 1Hz re-render so the "Xs ago" counter updates between
    // refreshes — keeps the indicator honest without extra fetches.
    const c = setInterval(() => forceTick(n => n + 1), 1000)
    return () => { alive = false; clearInterval(t); clearInterval(c) }
  }, [storeId, range.since, range.until])

  if (loading && !data) return <DashboardSkeleton />
  if (error)  return <div className="p-6 text-red-600">Error: {error}</div>
  if (!data)  return <DashboardSkeleton />

  // Freshness pill — green <30s, amber 30–60s, red >60s. The pill
  // also re-renders every second via forceTick() so operators see
  // the counter tick UP between fetches.
  const ageSec = lastFetchedAt ? Math.floor((Date.now() - lastFetchedAt) / 1000) : null
  const ageColor = ageSec === null ? 'slate'
                 : ageSec < 30 ? 'green'
                 : ageSec < 60 ? 'amber' : 'red'
  const freshness = (
    <span className={'inline-flex items-center gap-1.5 text-xs px-2 py-1 rounded-full ' +
      (ageColor === 'green' ? 'bg-emerald-50 text-emerald-700' :
       ageColor === 'amber' ? 'bg-amber-50 text-amber-700' :
       ageColor === 'red'   ? 'bg-red-50 text-red-700' :
                              'bg-slate-100 text-slate-500')}>
      <span className={'w-2 h-2 rounded-full ' +
        (ageColor === 'green' ? 'bg-emerald-500 animate-pulse' :
         ageColor === 'amber' ? 'bg-amber-500' :
         ageColor === 'red'   ? 'bg-red-500' : 'bg-slate-400')} />
      {ageSec === null ? 'loading…' : `updated ${ageSec}s ago`}
    </span>
  )

  // Only one true blocker remains: a store with no cameras attached
  // at all. Everything else (closed, cameras down, no data yet) falls
  // through to the regular dashboard with a contextual banner —
  // store managers do their end-of-day review AFTER hours, so the
  // tiles need to stay visible.
  if (data.status === 'no_cameras') {
    return (
      <div className="p-6">
        <PageHeader title={data.store_name} actions={freshness} />
        <Card className="p-8 text-center text-slate-500">
          No cameras attached to this store yet.{' '}
          <Link to="/cameras" className="text-sky-600 underline">Attach one</Link>{' '}
          to start collecting data.
        </Card>
      </div>
    )
  }

  // Subtle banner — replaces the old full-screen "Store closed" /
  // "No cameras online" / "No data yet" blockers. Renders above
  // the normal dashboard so today's accumulated numbers stay visible.
  const banner = (() => {
    if (data.status === 'closed') {
      return {
        tone: 'amber' as const,
        text: `🌙 Outside business hours${data.hours_label ? ` (${data.hours_label})` : ''} — showing today's data`,
      }
    }
    if (data.status === 'no_cameras_live') {
      return {
        tone: 'red' as const,
        text: `📵 ${data.cameras_total ?? 0} cameras attached but none streaming — values below may be stale`,
      }
    }
    if (data.status === 'no_data_yet') {
      return {
        tone: 'amber' as const,
        text: '⏳ Cameras just attached — numbers will appear within a minute or two',
      }
    }
    return null
  })()
  // Did the dashboard close? Used to dim "Right Now" tiles since the
  // values they hold are last-known, not live.
  const closed = data.status === 'closed'

  const t = data.tiles
  return (
    <div className="p-6 space-y-6">
      <PageHeader
        title={data.store_name}
        actions={
          <div className="flex items-center gap-3">
            <DateRangePicker value={range} onChange={setRange} />
            {data.status_light && <StatusLight value={data.status_light} />}
            {freshness}
          </div>
        }
      />
      <div className="text-xs text-slate-500 -mt-3 flex flex-wrap items-center gap-3">
        <span>Showing <strong>{range.label}</strong>. Trend arrows compare with the prior same-length window.</span>
        {data.hours_label && (
          <span>· Hours today: <strong>{data.hours_label}</strong></span>
        )}
        <span>
          · Cameras:{' '}
          <strong className={(data.cameras_online ?? 0) === 0 ? 'text-red-600' : 'text-emerald-700'}>
            {data.cameras_online ?? 0}/{data.cameras_total ?? data.camera_count ?? 0} live
          </strong>
        </span>
      </div>

      {/* Contextual banner — subtle, never blocks. */}
      {banner && (
        <div className={'rounded-md border px-3 py-2 text-sm ' +
          (banner.tone === 'red'
            ? 'bg-red-50 border-red-200 text-red-800'
            : 'bg-amber-50 border-amber-200 text-amber-800')}>
          {banner.text}
        </div>
      )}

      {/* RIGHT NOW — values are last-known when the store is closed.
          The dimmed prop greys the tile and prints a small "Store
          closed" pill so operators know this isn't a live reading. */}
      <section>
        <SectionTitle>Right now</SectionTitle>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Kpi label="People in store" big dimmed={closed} value={fmtInt(t.occupancy_now?.value)} />
          {t.queue_length_now?.visible && (
            <Kpi label="People in queue" big dimmed={closed} value={fmtInt(t.queue_length_now.value)} />
          )}
          {t.staff_present_pct_today?.visible && (
            <Kpi label="Staff present today" big dimmed={closed} value={`${fmtInt(t.staff_present_pct_today.value)}%`} />
          )}
          <Kpi label="Cameras live" value={`${data.cameras_online ?? 0}/${data.cameras_total ?? data.camera_count ?? 0}`} />
        </div>
      </section>

      {/* TODAY SO FAR */}
      <section>
        <SectionTitle>Today so far</SectionTitle>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {/* Every tile in this section now carries trend vs the prior
              same-length window via t.<key>.trend. */}
          <Kpi label="Unique visitors"
               value={fmtInt(t.unique_visitors_today?.value)}
               trendDir={t.unique_visitors_today?.trend?.direction}
               trendPct={t.unique_visitors_today?.trend?.delta_pct ?? null} />
          <Kpi label="Peak occupancy"
               value={fmtInt(t.occupancy_peak_today?.value)}
               trendDir={t.occupancy_peak_today?.trend?.direction}
               trendPct={t.occupancy_peak_today?.trend?.delta_pct ?? null} />
          {t.queue_wait_avg_today_sec?.visible && (
            <Kpi label="Avg queue wait"
                 value={`${fmtInt(t.queue_wait_avg_today_sec.value)} sec`}
                 trendDir={t.queue_wait_avg_today_sec?.trend?.direction}
                 trendPct={t.queue_wait_avg_today_sec?.trend?.delta_pct ?? null} />
          )}
          {t.visitors_net_today?.visible && (
            <Kpi label="Net visitors (in − out)"
                 value={fmtInt(t.visitors_net_today.value)}
                 sub={`${fmtInt(t.visitors_in_today?.value)} in · ${fmtInt(t.visitors_out_today?.value)} out`}
                 trendDir={t.visitors_net_today?.trend?.direction}
                 trendPct={t.visitors_net_today?.trend?.delta_pct ?? null} />
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

      {/* SECTION 3 — THIS WEEK (7-day bars + detector activity table) */}
      <WeekSection storeId={storeId} />

      {/* SECTION 4 — ALERTS & INCIDENTS (filtered to this store) */}
      <AlertsFeedSection storeId={storeId} />
    </div>
  )
}

// ----- small bits -----

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h2 className="text-sm font-semibold text-slate-500 uppercase tracking-wide mb-2">{children}</h2>
}

function Kpi({ label, value, big, sub, trendDir, trendPct, dimmed }: {
  label: string; value: string; big?: boolean; sub?: string
  trendDir?: 'up' | 'down' | 'flat'; trendPct?: number | null
  // When true, greys the tile and shows a small "Store closed" pill
  // — used on the Right Now row so operators see last-known values
  // while still understanding the store isn't currently trading.
  dimmed?: boolean
}) {
  return (
    <Card className="p-4 relative">
      <div className="text-xs text-slate-500">{label}</div>
      <div className={(big ? 'text-3xl' : 'text-2xl')
                       + ' font-semibold mt-1 '
                       + (dimmed ? 'text-slate-400' : '')}>{value}</div>
      {sub && <div className="text-xs text-slate-500 mt-1">{sub}</div>}
      {trendDir && trendPct !== undefined && (
        <div className="mt-1"><Trend direction={trendDir} deltaPct={trendPct} /></div>
      )}
      {dimmed && (
        <span className="absolute top-2 right-2 text-[10px] px-1.5 py-0.5 rounded
                         bg-slate-200 text-slate-600">
          Store closed
        </span>
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
          {/* Operators see "Loss Prevention", not "shrinkage" */}
          <div className="w-36 truncate" title={k}>{labelForDetector(k)}</div>
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

// ----- SECTION 3: THIS WEEK -----

interface WeekSummary {
  days: { date: string; weekday: string; value: number; source: string }[]
  best_day:  { date: string; weekday: string; value: number } | null
  worst_day: { date: string; weekday: string; value: number } | null
  weekday_avg_occupancy: { weekday: string; value: number }[]
  top_hours: { hour: number; label: string; value: number }[]
  detector_activity: {
    detector: string; events_today: number; events_week: number
    needs_zone: boolean; zone_present: boolean
    status: 'active' | 'needs_setup'
  }[]
}

function WeekSection({ storeId }: { storeId: number }) {
  const [d, setD] = useState<WeekSummary | null>(null)
  useEffect(() => {
    api<WeekSummary>(`/analytics/store/${storeId}/week-summary`)
      .then(setD).catch(() => setD(null))
  }, [storeId])
  if (!d) return null
  const maxBar = Math.max(...d.days.map(x => x.value), 1)

  return (
    <section className="space-y-3">
      <SectionTitle>This week</SectionTitle>

      {/* 7-day bars + best/worst summary */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Card className="lg:col-span-2 p-4">
          <div className="text-sm font-medium mb-2">Visitors — last 7 days</div>
          <div className="flex items-end gap-1 h-32">
            {d.days.map(day => (
              <div key={day.date} className="flex-1 flex flex-col items-center justify-end" title={`${day.date}: ${day.value}`}>
                <div className="text-[10px] text-slate-500 mb-0.5">{day.value > 0 ? Math.round(day.value) : ''}</div>
                <div className="w-full bg-sky-500 rounded-t"
                     style={{ height: `${(day.value / maxBar) * 100}%`,
                              minHeight: day.value > 0 ? '4px' : '1px',
                              opacity: day.value > 0 ? 1 : 0.2 }} />
                <div className="text-[10px] text-slate-500 mt-0.5">{day.weekday}</div>
              </div>
            ))}
          </div>
        </Card>
        <Card className="p-4 text-sm space-y-2">
          {d.best_day && (
            <div>
              <div className="text-xs text-slate-500">Best day this week</div>
              <div className="font-semibold">{d.best_day.weekday} — {Math.round(d.best_day.value)} visitors</div>
            </div>
          )}
          {d.worst_day && (
            <div>
              <div className="text-xs text-slate-500">Quietest day</div>
              <div className="font-semibold">{d.worst_day.weekday} — {Math.round(d.worst_day.value)} visitors</div>
            </div>
          )}
          {d.top_hours.length > 0 && (
            <div>
              <div className="text-xs text-slate-500 mt-1">Busiest hours</div>
              {d.top_hours.map((h, i) => (
                <div key={h.hour} className="flex justify-between">
                  <span>{i + 1}. {h.label}</span>
                  <span className="text-slate-500">{h.value}</span>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>

      {/* Detector activity table */}
      <Card className="p-4 overflow-x-auto">
        <div className="text-sm font-medium mb-2">Detector activity</div>
        <table className="w-full text-sm">
          <thead className="text-xs text-slate-500 uppercase">
            <tr>
              <th className="text-left py-1">Detector</th>
              <th className="text-right py-1 w-28">Events today</th>
              <th className="text-right py-1 w-28">Events this week</th>
              <th className="text-left py-1 w-32">Status</th>
            </tr>
          </thead>
          <tbody>
            {d.detector_activity.map(row => (
              <tr key={row.detector} className="border-t border-slate-100">
                <td className="py-1">{labelForDetector(row.detector)}</td>
                <td className="text-right py-1">{row.events_today}</td>
                <td className="text-right py-1">{row.events_week}</td>
                <td className="py-1">
                  {row.status === 'active'
                    ? <span className="text-emerald-600 text-xs">✅ Active</span>
                    : <span className="text-amber-600 text-xs">⚙️ Needs zone</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </section>
  )
}

// ----- SECTION 4: ALERTS & INCIDENTS -----

interface AlertRow {
  id: number
  status: 'open' | 'confirmed' | 'dismissed'
  severity: 'critical' | 'warning' | 'info'
  created_at: string
  camera_id?: number
  camera_name?: string | null
  detection_type?: string | null
  thumbnail_path?: string | null
}

function AlertsFeedSection({ storeId }: { storeId: number }) {
  const [alerts, setAlerts] = useState<AlertRow[] | null>(null)
  const load = () => api<AlertRow[]>(`/alerts?store_id=${storeId}&limit=20`)
    .then(setAlerts).catch(() => setAlerts([]))
  useEffect(() => {
    load()
    // Auto-refresh the alerts feed at the same 30s cadence as the
    // rest of the dashboard so operators see new incidents promptly.
    const t = setInterval(load, 30_000)
    return () => clearInterval(t)
  }, [storeId])

  async function act(id: number, action: 'confirm' | 'dismiss') {
    try {
      await api(`/alerts/${id}/${action}`, { method: 'POST' })
      load()
    } catch (e) { console.error(e) }
  }

  if (alerts === null) return null

  return (
    <section>
      <SectionTitle>Alerts &amp; incidents</SectionTitle>
      <Card className="p-3">
        {alerts.length === 0 ? (
          <div className="text-slate-400 text-sm p-3">No alerts in the last window.</div>
        ) : (
          <div className="divide-y divide-slate-100">
            {alerts.map(a => (
              <div key={a.id} className="flex items-center gap-3 py-2">
                {a.thumbnail_path
                  ? <img src={a.thumbnail_path} alt=""
                         className="w-16 h-16 object-cover rounded bg-slate-100" />
                  : <div className="w-16 h-16 bg-slate-100 rounded flex items-center justify-center text-2xl">🛡️</div>}
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <SeverityBadge sev={a.severity} />
                    <span className="font-medium truncate">
                      {a.detection_type ? labelForDetector(a.detection_type) : 'Alert'}
                    </span>
                  </div>
                  <div className="text-xs text-slate-500 truncate">
                    {a.camera_name ?? `Camera ${a.camera_id ?? '?'}`} ·{' '}
                    {new Date(a.created_at).toLocaleTimeString()}
                  </div>
                </div>
                <div className="flex gap-1">
                  {a.status === 'open' && (
                    <>
                      <button onClick={() => act(a.id, 'confirm')}
                              className="text-xs px-2 py-1 rounded bg-emerald-100 text-emerald-700 hover:bg-emerald-200">
                        Confirm
                      </button>
                      <button onClick={() => act(a.id, 'dismiss')}
                              className="text-xs px-2 py-1 rounded bg-slate-100 text-slate-600 hover:bg-slate-200">
                        Dismiss
                      </button>
                    </>
                  )}
                  {a.status !== 'open' && (
                    <span className="text-xs text-slate-400">{a.status}</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </section>
  )
}

function SeverityBadge({ sev }: { sev: AlertRow['severity'] }) {
  const map: Record<AlertRow['severity'], { label: string; cls: string }> = {
    critical: { label: '🔴 Critical', cls: 'bg-red-100 text-red-700' },
    warning:  { label: '🟡 Warning',  cls: 'bg-amber-100 text-amber-700' },
    info:     { label: '🔵 Info',     cls: 'bg-sky-100 text-sky-700' },
  }
  const m = map[sev] || map.info
  return (
    <span className={'text-[10px] px-1.5 py-0.5 rounded ' + m.cls}>{m.label}</span>
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
