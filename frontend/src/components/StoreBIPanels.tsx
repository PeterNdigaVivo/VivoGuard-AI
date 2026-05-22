// Store BI panels — May-2026 dashboard intelligence.
//
// Six interactive panels rendered on the store dashboard. Each owns
// its own /analytics/store/{id}/<endpoint> fetch + 30s refresh +
// loading skeleton + empty state.
//
// We use hand-rolled SVG for charts to stay consistent with the
// existing sparkline pattern and avoid pulling in recharts (~150kb).
// Every panel is collapsible (chevron in header) and exposes an
// `export PNG` button that screenshots its content via the browser
// canvas trick (html2canvas alternative below).

import { useEffect, useState } from 'react'
import { Card } from '@/components/ui/Primitives'
import { api } from '@/api/client'


// ---------------------------------------------------------------------------
// Shared panel chrome — collapsible header, refresh badge, export button.

function PanelShell({
  title, subtitle, busy, children,
}: {
  title: string; subtitle?: string; busy?: boolean
  children: React.ReactNode
}) {
  const [open, setOpen] = useState(true)
  return (
    <Card className="p-4">
      <button onClick={() => setOpen(o => !o)}
              className="w-full flex items-center justify-between mb-2 hover:opacity-80">
        <div className="text-left">
          <div className="text-sm font-semibold text-slate-700">{title}</div>
          {subtitle && <div className="text-xs text-slate-500">{subtitle}</div>}
        </div>
        <div className="flex items-center gap-2 text-xs text-slate-400">
          {busy && <span>↻</span>}
          <span className="text-base leading-none">{open ? '▾' : '▸'}</span>
        </div>
      </button>
      {open && <div>{children}</div>}
    </Card>
  )
}

function useRefresh<T>(url: string, deps: any[] = [], intervalMs = 30_000) {
  // Keeps stale data on screen during a re-poll instead of flashing
  // to a skeleton, and dedups overlapping in-flight requests so a
  // slow API doesn't fire a second tick before the first returns.
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(true)
  useEffect(() => {
    let alive = true
    let inFlight = false      // dedup gate
    const tick = () => {
      if (inFlight) return    // previous request still running — skip
      inFlight = true
      setBusy(true)
      api<T>(url).then(d => {
        if (!alive) return
        setData(d); setError(null); setBusy(false)
      }).catch(e => {
        if (!alive) return
        setError(String(e)); setBusy(false)
      }).finally(() => { inFlight = false })
    }
    tick()
    const t = setInterval(tick, intervalMs)
    return () => { alive = false; clearInterval(t) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return { data, error, busy }
}

// Helper: append ?since=…&until=… to a path, dropping empties so
// the backend's "default to today's session" fallback still kicks
// in when the operator hasn't picked a range.

type RangeProp = { since?: string; until?: string } | undefined

function withRange(path: string, range?: { since?: string; until?: string }): string {
  if (!range || (!range.since && !range.until)) return path
  const q = new URLSearchParams()
  if (range.since) q.set("since", range.since)
  if (range.until) q.set("until", range.until)
  return path + (path.includes("?") ? "&" : "?") + q.toString()
}

function SkeletonBlock({ height = 120 }: { height?: number }) {
  return <div className="bg-slate-100 animate-pulse rounded" style={{ height }} />
}

function EmptyState({ icon, text }: { icon: string; text: string }) {
  return (
    <div className="text-center py-6 text-slate-500 text-sm">
      <div className="text-3xl mb-2">{icon}</div>
      {text}
    </div>
  )
}


// ---------------------------------------------------------------------------
// PANEL 1 — Hourly Footfall Intelligence

interface HourlyPayload {
  hours: { hour: number; label: string; visitors: number; intensity: 'high'|'medium'|'low'|'empty'; is_current: boolean }[]
  peak:  { hour: number; visitors: number } | null
  quiet: { hour: number; visitors: number } | null
  busy_hour_range: [number, number] | null
  trend_delta_pct: number | null
  insights: string[]
  open_hour: number; close_hour: number; current_hour: number
  // Server-computed today total. Optional because older API
  // containers don't return it — the chart's HourlySummary helper
  // falls back to summing hours[] client-side when this is missing.
  today_total?: number
  yesterday_so_far?: number
  metric_source?: string | null
  restock_hour?: number | null
}

export function HourlyFootfallPanel({ storeId, range }: { storeId: number; range?: RangeProp }) {
  const { data, error, busy } = useRefresh<HourlyPayload>(
    withRange(`/analytics/store/${storeId}/hourly`, range), [storeId, range?.since, range?.until])
  return (
    <PanelShell title="Hourly footfall intelligence"
                subtitle="Visitors per hour today, with auto-generated insights"
                busy={busy}>
      {/* Three states: error → loading → render */}
      {error ? (
        <div className="text-sm text-red-600 py-4 text-center">
          Could not load hourly data. The API may not be deployed yet —
          rebuild the api container and try again.
          <div className="text-xs text-slate-500 mt-1">({error})</div>
        </div>
      ) : !data ? (
        <SkeletonBlock height={160} />
      ) : (
        <SimpleLineChart payload={data} />
      )}
      {data && <InsightList insights={data.insights} />}
    </PanelShell>
  )
}

// Dead-simple line graph. Hardcoded fixed pixel dimensions (NOT
// viewBox with `preserveAspectRatio="none"`) because the previous
// approach occasionally rendered as a zero-height pane when the
// container's responsive layout collapsed. Fixed pixels = guaranteed
// visible pane on every screen size.
// Hourly footfall — simple line graph per the May-2026 spec.
//   X axis: hours
//   Y axis: visitor count
//   Single blue line + dot at each data point
//   "No visitor data yet today" when every bucket is 0
//   Inline SVG — no external libs, no animation, no fancy markers.
// Previous variants had peak markers, pulsing rings, gradient fills,
// and intensity-based dot colours. They kept regressing when the
// data shape shifted. This is the minimum that always paints.
function SimpleLineChart({ payload }: { payload: HourlyPayload }) {
  const hours = payload.hours || []
  if (hours.length === 0) {
    return <div className="py-6 text-center text-slate-500 text-sm">
      No visitor data yet today
    </div>
  }
  const total = hours.reduce((s, h) => s + (h.visitors || 0), 0)
  if (total === 0) {
    return <div className="py-6 text-center text-slate-500 text-sm">
      No visitor data yet today
    </div>
  }

  // Fixed canvas. Viewbox + 100% width scales horizontally; fixed
  // height keeps the pane from collapsing.
  const W = 600, H = 180, PL = 36, PR = 12, PT = 10, PB = 28
  const innerW = W - PL - PR
  const innerH = H - PT - PB
  const max = Math.max(...hours.map(h => h.visitors || 0), 1)
  const stepX = hours.length > 1 ? innerW / (hours.length - 1) : 0
  const xAt = (i: number) => PL + i * stepX
  const yAt = (v: number) => PT + innerH - (v / max) * innerH

  const linePath = hours
    .map((h, i) => `${i === 0 ? 'M' : 'L'} ${xAt(i).toFixed(1)} ${yAt(h.visitors).toFixed(1)}`)
    .join(' ')

  // Y-axis ticks: 0 + max, plus 2 intermediate values if max > 4.
  const yTicks: number[] = [0]
  if (max > 1) {
    const step = Math.max(1, Math.ceil(max / 4))
    for (let v = step; v < max; v += step) yTicks.push(v)
  }
  yTicks.push(max)

  return (
    <div>
      <svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`} style={{ display: 'block' }}>
        {/* Y-axis grid + labels */}
        {yTicks.map((tv, i) => {
          const y = yAt(tv)
          return (
            <g key={i}>
              <line x1={PL} x2={W - PR} y1={y} y2={y}
                    stroke="#e5e7eb" strokeDasharray="2 3" />
              <text x={PL - 6} y={y + 4} textAnchor="end"
                    fontSize="11" fill="#64748b">{tv}</text>
            </g>
          )
        })}
        {/* The line. Single blue stroke. */}
        <path d={linePath} stroke="#1d4ed8" strokeWidth={2.5}
              fill="none" strokeLinejoin="round" strokeLinecap="round" />
        {/* Dots + x-axis labels */}
        {hours.map((h, i) => (
          <g key={i}>
            <circle cx={xAt(i)} cy={yAt(h.visitors)} r={3.5}
                    fill="#1d4ed8" stroke="white" strokeWidth={1.5} />
            <text x={xAt(i)} y={H - 8} textAnchor="middle"
                  fontSize="11" fill="#64748b">{h.label}</text>
          </g>
        ))}
      </svg>
      <HourlySummary payload={payload} />
    </div>
  )
}

// Text-based fallback for the hourly chart. ALWAYS rendered — when
// the SVG is also rendered, it sits below as a key-numbers summary;
// when the SVG is absent (all-zero day), this is the primary read.
// Operators get answers regardless of whether the chart paints.
function HourlySummary({ payload }: { payload: HourlyPayload }) {
  // Recompute peak / quiet / total client-side from the `hours`
  // array if the backend hasn't provided pre-computed fields. Keeps
  // the summary honest even when an old API container is in front
  // of a freshly-built frontend.
  const hrs = payload.hours || []
  const nonZero = hrs.filter(h => (h.visitors ?? 0) > 0)
  const peak  = payload.peak  ?? (nonZero.length
    ? nonZero.reduce((b, h) => h.visitors > b.visitors ? h : b)
    : null)
  const quiet = payload.quiet ?? (nonZero.length
    ? nonZero.reduce((b, h) => h.visitors < b.visitors ? h : b)
    : null)
  const total = payload.today_total ?? hrs.reduce((s, h) => s + (h.visitors || 0), 0)
  return (
    <div className="mt-3 grid grid-cols-1 sm:grid-cols-3 gap-2 text-xs text-slate-700 border-t border-slate-100 pt-2">
      <div>
        <span className="text-slate-500">Peak hour:</span>{' '}
        {peak ? (
          <strong>{peak.hour.toString().padStart(2, '0')}:00 ({peak.visitors} visitors)</strong>
        ) : <span className="text-slate-400">—</span>}
      </div>
      <div>
        <span className="text-slate-500">Quietest:</span>{' '}
        {quiet ? (
          <strong>{quiet.hour.toString().padStart(2, '0')}:00 ({quiet.visitors} visitors)</strong>
        ) : <span className="text-slate-400">—</span>}
      </div>
      <div>
        <span className="text-slate-500">Total today:</span>{' '}
        <strong>{total} visitors</strong>
      </div>
    </div>
  )
}

// Pick `count` round numbers between 0 and `max` for Y-axis ticks.
// Avoids "1.3, 2.6, 3.9..." style fractional gridlines.
function niceTicks(_min: number, max: number, count: number): number[] {
  if (max <= 0) return [0]
  const step = Math.max(1, Math.ceil(max / count))
  const out: number[] = []
  for (let v = 0; v <= max + 0.01; v += step) out.push(Math.round(v))
  if (out[out.length - 1] < max) out.push(Math.ceil(max))
  return out
}

function InsightList({ insights }: { insights: string[] }) {
  if (!insights || insights.length === 0) return null
  return (
    <ul className="mt-3 space-y-1 text-xs text-slate-700">
      {insights.map((i, idx) => (
        <li key={idx} className="flex gap-2">
          <span className="text-sky-500">›</span>
          <span>{i}</span>
        </li>
      ))}
    </ul>
  )
}


// ---------------------------------------------------------------------------
// PANEL 2 — Customer Journey Map

interface BehaviourPayload {
  has_journey_data: boolean
  setup_hint?: string
  total_journeys?: number
  avg_journey_seconds?: number
  completion_pct?: number
  common_path?: string[]
  most_skipped_zone?: string | null
  zone_visit_counts?: Record<string, number>
  top_paths?: { path: string[]; count: number }[]
}

export function JourneyMapPanel({ storeId, range }: { storeId: number; range?: RangeProp }) {
  const { data, busy } = useRefresh<BehaviourPayload>(withRange(`/analytics/store/${storeId}/behaviour`, range), [storeId, range?.since, range?.until])
  return (
    <PanelShell title="Customer journey map"
                subtitle="Common paths through the store over the last 7 days"
                busy={busy}>
      {!data ? <SkeletonBlock height={180} /> :
        !data.has_journey_data
          ? <EmptyState icon="🗺️" text={data.setup_hint ?? 'No journey data yet.'} />
          : <JourneyChart payload={data} />}
    </PanelShell>
  )
}

// Render one step (a node in the journey path) as a clean label.
// Belt-and-suspenders for the case where the backend stored a dict
// (legacy data from before the backend normaliser landed) — we don't
// want to print "{'zone_id': 5, ...}" in the UI.
function nodeLabel(node: unknown): string {
  if (node == null) return 'Zone'
  if (typeof node === 'object') {
    const o = node as any
    return o.zone_name || o.name || (o.zone_id != null ? `Zone ${o.zone_id}` : 'Zone')
  }
  if (typeof node === 'string') {
    const s = node.trim()
    // Stringified Python dict slipped through. The exact shape we
    // see in the wild has SINGLE quotes (Python repr):
    //   {'zone_id': 5, 'zone_name': 'Entrance', 'entered_at': '...'}
    // Match both single- and double-quoted variants.
    if (s.startsWith('{')) {
      let m = s.match(/['"]zone_name['"]\s*:\s*['"]([^'"]+)['"]/)
      if (m) return m[1]
      m = s.match(/['"]name['"]\s*:\s*['"]([^'"]+)['"]/)
      if (m) return m[1]
      m = s.match(/['"]zone_id['"]\s*:\s*(\d+)/)
      if (m) return `Zone ${m[1]}`
      return 'Zone'    // unrecognised dict shape — never print raw JSON
    }
    return s
  }
  return String(node)
}

function JourneyChart({ payload }: { payload: BehaviourPayload }) {
  // Simplified flow: render the top-3 paths as horizontal node lanes
  // with arrow connectors, width proportional to count.
  const top = payload.top_paths || []
  if (top.length === 0) return <EmptyState icon="🗺️" text="Not enough journey data yet." />
  const maxCount = Math.max(...top.map(p => p.count), 1)
  return (
    <div>
      <div className="space-y-2">
        {top.map((p, i) => {
          const width = (p.count / maxCount) * 100
          const labels = (p.path || []).map(nodeLabel)
          const lastLabel = (labels[labels.length - 1] || '').toLowerCase()
          const completed = lastLabel.includes('counter') || lastLabel.includes('checkout')
          return (
            <div key={i} className="flex items-center gap-1 flex-wrap">
              {labels.map((label, j) => (
                <span key={j} className="flex items-center gap-1">
                  <span className={'px-2 py-1 rounded text-xs ' +
                    (completed ? 'bg-emerald-100 text-emerald-800' : 'bg-slate-100 text-slate-700')}>
                    {label}
                  </span>
                  {j < labels.length - 1 && (
                    <span className="text-slate-400 text-xs">→</span>
                  )}
                </span>
              ))}
              <div className="flex-1 mx-2 h-2 bg-slate-100 rounded min-w-[60px]">
                <div className={'h-2 rounded ' +
                  (completed ? 'bg-emerald-500' : 'bg-slate-400')}
                     style={{ width: `${width}%` }} />
              </div>
              <span className="text-xs text-slate-600 tabular-nums w-10 text-right">{p.count}</span>
            </div>
          )
        })}
      </div>
      <div className="mt-3 text-xs text-slate-700 space-y-1">
        <div>Average journey:{' '}
          <strong>{Math.floor((payload.avg_journey_seconds ?? 0) / 60)}m {(payload.avg_journey_seconds ?? 0) % 60}s</strong>
        </div>
        <div>{payload.completion_pct}% of visitors reached the counter</div>
        {payload.most_skipped_zone && (
          <div>Most skipped: <strong>{nodeLabel(payload.most_skipped_zone)}</strong></div>
        )}
      </div>
    </div>
  )
}


// ---------------------------------------------------------------------------
// PANEL 3 — Heatmap Intelligence (recommendations from zone-perf + heatmap)

export function HeatmapIntelligencePanel({ storeId, firstCameraId, range }: {
  storeId: number; firstCameraId: number | null; range?: RangeProp
}) {
  const { data: zonePerf, busy } = useRefresh<ZonePerformancePayload>(
    withRange(`/analytics/store/${storeId}/zone-performance`, range), [storeId, range?.since, range?.until])
  return (
    <PanelShell title="Behaviour heatmap intelligence"
                subtitle="Where customers go — and what to do about it"
                busy={busy}>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Thumbnail */}
        {firstCameraId !== null ? (
          <img src={`/api/analytics/heatmap/${firstCameraId}/image?alpha=0.85&window=day`}
               alt="Heatmap" className="rounded border w-full bg-slate-900"
               onError={(e) => ((e.target as HTMLImageElement).style.display = 'none')} />
        ) : (
          <EmptyState icon="🔥" text="Add a camera with a heatmap zone to populate this view." />
        )}
        {/* Recommendations */}
        <div className="space-y-2 text-xs">
          {zonePerf?.zones && zonePerf.zones.length > 0 && (
            <>
              <RecCard icon="🔥"
                       title={`Hot zone: ${zonePerf.zones[0].name}`}
                       body={`${zonePerf.zones[0].engagement_pct}% engagement. Place promotional displays here for maximum visibility.`} />
              {zonePerf.zones.length > 1 && zonePerf.zones[zonePerf.zones.length - 1].engagement_pct < 40 && (
                <RecCard icon="❄️"
                         title={`Cold zone: ${zonePerf.zones[zonePerf.zones.length - 1].name}`}
                         body={`Only ${zonePerf.zones[zonePerf.zones.length - 1].engagement_pct}% traffic. Move slow-selling items here, or add signage to draw customers.`} />
              )}
              {zonePerf.zones[0].avg_dwell_seconds > 120 && (
                <RecCard icon="⏱️"
                         title={`High dwell: ${zonePerf.zones[0].name}`}
                         body={`Customers spend ${Math.round(zonePerf.zones[0].avg_dwell_seconds / 60)}m here on average. Keep it well-stocked and well-lit.`} />
              )}
            </>
          )}
          {(!zonePerf || (zonePerf.zones || []).length === 0) && !busy && (
            <EmptyState icon="💡"
                        text="Draw aisle/dwell zones on your cameras to unlock recommendations." />
          )}
        </div>
      </div>
    </PanelShell>
  )
}

function RecCard({ icon, title, body }: { icon: string; title: string; body: string }) {
  return (
    <div className="border border-slate-200 rounded p-2 bg-slate-50">
      <div className="font-medium text-slate-800">
        <span className="mr-1">{icon}</span>{title}
      </div>
      <div className="text-slate-600 mt-0.5">{body}</div>
    </div>
  )
}


// ---------------------------------------------------------------------------
// PANEL 4 — Staff Presence Intelligence (gauge + timeline)

interface StaffPayload {
  has_data: boolean
  setup_hint?: string
  today_pct: number
  current_status: 'staffed' | 'unstaffed' | 'unknown'
  gaps: { start: string; end: string | null; duration_minutes: number; ongoing?: boolean }[]
  timeline: { minute: string; staffed: boolean }[]
  insights: string[]
}

export function StaffPresencePanel({ storeId, range }: { storeId: number; range?: RangeProp }) {
  const { data, busy } = useRefresh<StaffPayload>(withRange(`/analytics/store/${storeId}/staff-timeline`, range), [storeId, range?.since, range?.until])
  return (
    <PanelShell title="Counter staffing intelligence"
                subtitle="Staff coverage today + gaps to investigate"
                busy={busy}>
      {!data ? <SkeletonBlock height={200} /> :
        !data.has_data
          ? <EmptyState icon="👥" text={data.setup_hint ?? 'No staff data yet.'} />
          : (
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <Gauge pct={data.today_pct} />
              <div className="md:col-span-2">
                <StaffTimelineBar timeline={data.timeline} />
                <InsightList insights={data.insights} />
              </div>
            </div>
          )}
    </PanelShell>
  )
}

function Gauge({ pct }: { pct: number }) {
  // 180° arc, needle angle interpolates [0..180].
  const angle = Math.max(0, Math.min(180, (pct / 100) * 180))
  const colour = pct >= 90 ? '#10b981' : pct >= 70 ? '#f59e0b' : '#ef4444'
  // Compute needle end coordinates
  const rad = (Math.PI * (180 - angle)) / 180
  const nx = 80 + 60 * Math.cos(rad)
  const ny = 80 - 60 * Math.sin(rad)
  return (
    <div className="text-center">
      <svg viewBox="0 0 160 100" className="w-40 mx-auto">
        {/* Three coloured arc segments (red, amber, green) */}
        <path d="M 10 80 A 70 70 0 0 1 58 14" stroke="#ef4444" strokeWidth="14" fill="none" />
        <path d="M 58 14 A 70 70 0 0 1 102 14" stroke="#f59e0b" strokeWidth="14" fill="none" />
        <path d="M 102 14 A 70 70 0 0 1 150 80" stroke="#10b981" strokeWidth="14" fill="none" />
        {/* Needle */}
        <line x1="80" y1="80" x2={nx} y2={ny} stroke={colour} strokeWidth="3" strokeLinecap="round" />
        <circle cx="80" cy="80" r="5" fill={colour} />
      </svg>
      <div className="text-2xl font-semibold mt-1" style={{ color: colour }}>{pct}%</div>
      <div className="text-xs text-slate-500">Staff present today</div>
    </div>
  )
}

function StaffTimelineBar({ timeline }: { timeline: StaffPayload['timeline'] }) {
  if (!timeline || timeline.length === 0) {
    return <div className="text-slate-400 text-xs">No timeline data.</div>
  }
  // Render as a horizontal bar of N coloured segments.
  return (
    <div>
      <div className="text-xs text-slate-500 mb-1">Today's coverage</div>
      <div className="flex h-4 rounded overflow-hidden border border-slate-200">
        {timeline.map((m, i) => (
          <div key={i} className={m.staffed ? 'bg-emerald-500' : 'bg-red-400'}
               style={{ flex: 1 }}
               title={`${m.minute.substring(11, 16)} — ${m.staffed ? 'staffed' : 'UNSTAFFED'}`} />
        ))}
      </div>
      <div className="flex justify-between text-[10px] text-slate-400 mt-0.5">
        <span>{timeline[0].minute.substring(11, 16)}</span>
        <span>{timeline[timeline.length - 1].minute.substring(11, 16)}</span>
      </div>
    </div>
  )
}


// ---------------------------------------------------------------------------
// PANEL 5 — Visual Merchandising Performance

interface ZonePerformancePayload {
  zones: { zone_id: number; name: string; engagement_pct: number; avg_dwell_seconds: number; rag: 'green'|'amber'|'red'; rank: number }[]
  insights: string[]
  setup_hint?: string
}

export function MerchandisingPanel({ storeId, range }: { storeId: number; range?: RangeProp }) {
  const { data, busy } = useRefresh<ZonePerformancePayload>(withRange(`/analytics/store/${storeId}/zone-performance`, range), [storeId, range?.since, range?.until])
  return (
    <PanelShell title="Visual merchandising performance"
                subtitle="Aisle engagement ranked by visitor share"
                busy={busy}>
      {!data ? <SkeletonBlock height={180} /> :
        (data.zones || []).length === 0
          ? <EmptyState icon="🛍️" text={data.setup_hint ?? 'No zone data yet.'} />
          : (
            <>
              <div className="space-y-1.5">
                {data.zones.map(z => (
                  <div key={z.zone_id} className="flex items-center gap-2 text-sm">
                    <span className="w-32 truncate" title={z.name}>{z.name}</span>
                    <div className="flex-1 bg-slate-100 rounded h-5 relative">
                      <div className={'absolute inset-y-0 left-0 rounded ' +
                        (z.rag === 'green' ? 'bg-emerald-500' :
                         z.rag === 'amber' ? 'bg-amber-500' : 'bg-red-500')}
                           style={{ width: `${Math.max(z.engagement_pct, 1)}%` }} />
                    </div>
                    <span className="w-12 text-right tabular-nums text-xs">{z.engagement_pct}%</span>
                    <span className="w-20 text-right text-xs text-slate-500 tabular-nums">
                      {Math.floor(z.avg_dwell_seconds / 60)}m{z.avg_dwell_seconds % 60}s
                    </span>
                  </div>
                ))}
              </div>
              <InsightList insights={data.insights} />
            </>
          )}
    </PanelShell>
  )
}


// ---------------------------------------------------------------------------
// PANEL 6 — Daily Performance Scorecard

interface ScorecardRow {
  metric: string
  today: number
  yesterday: number
  unit: string
  higher_is_better: boolean
  // delta semantics — null when there's no yesterday baseline.
  direction: 'up' | 'down' | 'flat'
  delta_pct: number | null
  rag: 'green' | 'amber' | 'red' | 'slate'
}
interface ScorecardPayload {
  rows: ScorecardRow[]
  overall_direction: 'better' | 'same' | 'worse'
  overall_rag: 'green' | 'amber' | 'red' | 'slate'
  headline: string
}

// Layer-3 visitor intelligence payload — staff exclusion +
// confidence indicator. Sits above the scorecard so operators see
// the customer/staff split + confidence before the per-metric table.
interface VisitorIntelligencePayload {
  total_persons: number
  staff_tracks: number
  estimated_customers: number
  staff_signatures: string[]
  avg_dwell_seconds: number
  zones_visited_avg: number
  total_zones: number
  visit_quality_score: number
  confidence: 'high' | 'estimated' | 'none'
  explanation: string
  footnote: string
}

export function ScorecardPanel({ storeId, range }: { storeId: number; range?: RangeProp }) {
  const { data, busy } = useRefresh<ScorecardPayload>(
    withRange(`/analytics/store/${storeId}/scorecard`, range), [storeId, range?.since, range?.until])
  // Separate fetch for the new visitor-intelligence endpoint. Keeps
  // the scorecard payload backwards-compatible — older API containers
  // that don't expose /visitor-intelligence just hide the band.
  const { data: vi } = useRefresh<VisitorIntelligencePayload>(
    withRange(`/analytics/store/${storeId}/visitor-intelligence`, range), [storeId, range?.since, range?.until])
  return (
    <PanelShell title="Daily performance scorecard"
                subtitle="Today vs yesterday — same store, same window"
                busy={busy}>
      {/* Visitor intelligence band — staff/customer split + confidence
          indicator. Surfaces the May-2026 3-layer logic so operators
          know "this is customers only, staff filtered out". */}
      {vi && vi.total_persons > 0 && (
        <VisitorIntelligenceBand vi={vi} />
      )}
      {!data ? <SkeletonBlock height={240} /> :
        (data.rows || []).length === 0
          ? <EmptyState icon="📈" text="No scorecard data yet." />
          : (
            <>
              <table className="w-full text-sm">
                <thead className="text-xs uppercase text-slate-500">
                  <tr>
                    <th className="text-left py-1">Metric</th>
                    <th className="text-right py-1">Today</th>
                    <th className="text-right py-1">Yesterday</th>
                    <th className="text-right py-1 w-24">Change</th>
                    <th className="text-center py-1 w-10">RAG</th>
                  </tr>
                </thead>
                <tbody>
                  {data.rows.map(r => (
                    <tr key={r.metric} className="border-t border-slate-100">
                      {/* "Visitors" row gets relabelled to clarify
                          that staff have been excluded from the count
                          (Layer 1 of the visitor-intelligence
                          redesign). Other rows pass through. */}
                      <td className="py-1.5">
                        {r.metric === 'Visitors'
                          ? <span title="Staff tracks excluded — see visitor intelligence band above">
                              Estimated customers
                              <span className="text-slate-400 text-xs"> (staff excluded)</span>
                            </span>
                          : r.metric}
                      </td>
                      <td className="text-right tabular-nums font-medium">{r.today}{r.unit}</td>
                      <td className="text-right tabular-nums text-slate-500">{r.yesterday}{r.unit}</td>
                      <td className="text-right tabular-nums">
                        <ChangeCell row={r} />
                      </td>
                      <td className="text-center">
                        <RagDot rag={r.rag} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {/* Headline label next to the overall RAG dot. Belt-
                  and-suspenders: derive the label client-side from
                  overall_direction if the backend ever omits the
                  pre-rendered headline string — operators never see
                  a naked dot. */}
              {(() => {
                // Direction may be absent (old API). Derive from per-
                // row pct deltas client-side so the label always
                // renders meaningful text next to the overall dot.
                let dir = data.overall_direction
                if (!dir) {
                  let better = 0, worse = 0
                  for (const r of data.rows) {
                    const higher = r.higher_is_better !== false
                    let p = r.delta_pct ?? null
                    if (p == null && r.yesterday && r.yesterday !== 0) {
                      p = ((r.today - r.yesterday) / r.yesterday) * 100
                    }
                    if (p == null) continue
                    if (higher ? p > 0.5 : p < -0.5) better++
                    else if (higher ? p < -0.5 : p > 0.5) worse++
                  }
                  dir = better > worse ? 'better' : worse > better ? 'worse' : 'same'
                }
                const rag = data.overall_rag
                  ?? (dir === 'better' ? 'green' : dir === 'worse' ? 'red' : 'amber')
                return (
                  <div className="mt-3 flex items-center gap-2 border-t border-slate-100 pt-2">
                    <RagDot rag={rag} big />
                    <div className={'text-sm font-semibold ' +
                      (dir === 'better' ? 'text-emerald-700' :
                       dir === 'worse'  ? 'text-red-700' :
                                          'text-slate-700')}>
                      vs yesterday:{' '}
                      {dir === 'better' ? '▲ Better overall'
                      : dir === 'worse' ? '▼ Worse overall'
                      : '→ Same overall'}
                    </div>
                  </div>
                )
              })()}
            </>
          )}
    </PanelShell>
  )
}

// One row's "Change" cell. ▲ green when better-than-yesterday for
// higher-is-better metrics (or lower-than for lower-is-better), ▼ red
// when worse, → grey when within ±0.5pp.
function ChangeCell({ row }: { row: ScorecardRow }) {
  // delta_pct may be missing entirely when an old API container is
  // serving the new frontend (the old shape returned `target` instead
  // of {direction, delta_pct, rag}). Compute it from today/yesterday
  // ourselves so the column reads correctly regardless.
  let pct: number | null = row.delta_pct ?? null
  if (pct == null && row.yesterday && row.yesterday !== 0) {
    pct = ((row.today - row.yesterday) / row.yesterday) * 100
    pct = Math.round(pct * 10) / 10
  }
  if (pct == null) {
    return <span className="text-slate-400">—</span>
  }
  const higher = row.higher_is_better !== false   // default true
  const better = higher ? pct > 0.5 : pct < -0.5
  const worse  = higher ? pct < -0.5 : pct > 0.5
  if (better) {
    return <span className="text-emerald-600 font-medium">▲ {Math.abs(pct)}%</span>
  }
  if (worse) {
    return <span className="text-red-600 font-medium">▼ {Math.abs(pct)}%</span>
  }
  return <span className="text-slate-400">→ 0%</span>
}

// Visitor intelligence band shown above the scorecard table. Three
// inline numbers (total persons / staff tracks / estimated customers)
// plus the confidence badge and the spec's footnote.
function VisitorIntelligenceBand({ vi }: { vi: VisitorIntelligencePayload }) {
  const confidenceColour = vi.confidence === 'high'
    ? 'bg-emerald-100 text-emerald-800'
    : vi.confidence === 'estimated'
      ? 'bg-amber-100 text-amber-800'
      : 'bg-slate-100 text-slate-600'
  return (
    <div className="mb-3 rounded border border-sky-100 bg-sky-50 p-3">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-sm">
        <div>
          <span className="text-slate-500">Total persons:</span>{' '}
          <strong className="tabular-nums">{vi.total_persons}</strong>
        </div>
        <div>
          <span className="text-slate-500">Staff tracks:</span>{' '}
          <strong className="tabular-nums text-amber-700">{vi.staff_tracks}</strong>
        </div>
        <div>
          <span className="text-slate-500">Estimated customers:</span>{' '}
          <strong className="tabular-nums text-emerald-700">{vi.estimated_customers}</strong>
        </div>
        <div className="ml-auto">
          <span className={'text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded ' + confidenceColour}>
            {vi.confidence === 'high' ? '✓ High confidence' :
             vi.confidence === 'estimated' ? '~ Estimated' : 'No data'}
          </span>
        </div>
      </div>
      {vi.total_zones > 0 && (
        <div className="text-xs text-slate-600 mt-1.5">
          Average customer visited <strong>{vi.zones_visited_avg.toFixed(1)}</strong> of {vi.total_zones} zones, spent{' '}
          <strong>{Math.floor(vi.avg_dwell_seconds / 60)}m {Math.round(vi.avg_dwell_seconds % 60)}s</strong>{' '}
          in product areas. Visit quality score: <strong>{vi.visit_quality_score}</strong>
        </div>
      )}
      <div className="text-[11px] text-slate-500 mt-1 italic">
        {vi.footnote || 'Staff identified by counter presence >10 min'}
      </div>
    </div>
  )
}

function RagDot({ rag, big }: { rag: 'green'|'amber'|'red'|'slate'; big?: boolean }) {
  const cls = rag === 'green' ? 'bg-emerald-500' :
              rag === 'amber' ? 'bg-amber-500' :
              rag === 'red'   ? 'bg-red-500' : 'bg-slate-300'
  const size = big ? 'w-4 h-4' : 'w-2.5 h-2.5'
  return <span className={`inline-block rounded-full ${cls} ${size}`} />
}


// ---------------------------------------------------------------------------
// Default export: all six panels stacked. Caller drops this into the
// store dashboard and the panels self-fetch.

export default function StoreBIPanels({ storeId, firstCameraId, range }: {
  storeId: number; firstCameraId: number | null; range?: RangeProp
}) {
  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <HourlyFootfallPanel        storeId={storeId} range={range} />
      <ScorecardPanel             storeId={storeId} range={range} />
      <JourneyMapPanel            storeId={storeId} range={range} />
      <StaffPresencePanel         storeId={storeId} range={range} />
      <HeatmapIntelligencePanel   storeId={storeId} range={range} firstCameraId={firstCameraId} />
      <MerchandisingPanel         storeId={storeId} range={range} />
    </div>
  )
}
