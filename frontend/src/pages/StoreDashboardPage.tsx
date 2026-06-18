// Per-store dashboard — Rule 3/4 redesign.
//
//   Top bar:    name, country, status-light pill, last update timestamp
//   Right now:  occupancy now, queue now, status
//   Today:      unique visitors (w/ trend), peak occupancy, staff %,
//               in/out/net visitors (entry_exit only),
//               top 3 aisles by dwell
//   Charts:     hourly footfall sparkline, alerts-by-type bar chart
//   Heatmap:    composite thumbnail; clicks through to the heatmap page

import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  Card, PageHeader, Skeleton, StatusLight, Trend,
} from '@/components/ui/Primitives'
import DateRangePicker, { rangeFor, type DateRange } from '@/components/DateRangePicker'
import {
  computeStoreHealth, StoreHealthCard,
  TodayPlainEnglishPanel, AiNarrative, StaffPerformancePanel,
} from '@/components/StoreHealthPanels'
import { api } from '@/api/client'
import { stores as storesApi, type Store } from '@/api/stores'
import StoreForm from '@/components/StoreForm'
import { labelForDetector } from '@/lib/detectorLabels'
// Heavy chart bundles — lazy-loaded so the dashboard's critical
// "Right Now" tiles paint before the chart code is even fetched.
const StoreBIPanels        = lazy(() => import('@/components/StoreBIPanels'))
const StoreAIIntelligence  = lazy(() => import('@/components/StoreAIIntelligence'))
import { AlertCard, groupAlerts } from '@/components/AlertCard'
import type { Alert as AlertRowFull } from '@/api/alerts'

// Parse a camera_id out of the live tile's heatmap thumb URL.
// Format: /api/analytics/heatmap/{cam}/image?...
// Used by the heatmap-intelligence panel so it knows which camera's
// PNG to render. Returns null if no thumb URL was emitted.
function extractFirstCameraId(url: string | null): number | null {
  if (!url) return null
  const m = url.match(/\/heatmap\/(\d+)\//)
  return m ? Number(m[1]) : null
}

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
  // `data_source` / `data_source_label` are populated by the backend
  // on the visitor-count tile so the UI can render the glass-door
  // "High accuracy count" badge — optional because most tiles don't
  // carry provenance metadata.
  tiles: Record<string, {
    value: any
    visible: boolean
    trend?: { direction: 'up'|'down'|'flat'; delta_pct: number | null }
    data_source?: string
    data_source_label?: string
  }>
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
  // Store details for the edit modal — opening hours, manager phone,
  // queue SLA, etc. Loaded lazily when the pen icon is clicked so we
  // don't pay the round-trip on every dashboard render.
  const [editing, setEditing] = useState(false)
  const [storeForEdit, setStoreForEdit] = useState<Store | null>(null)
  const [editBusy, setEditBusy] = useState(false)
  async function openEdit() {
    setEditing(true)
    if (!storeForEdit) {
      try { setStoreForEdit(await storesApi.get(storeId)) }
      catch { /* surfaced in the modal */ }
    }
  }
  async function saveEdit(patch: Partial<Store>) {
    setEditBusy(true)
    try {
      const updated = await storesApi.update(storeId, patch)
      setStoreForEdit(updated)
      setEditing(false)
    } finally { setEditBusy(false) }
  }
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
            <button onClick={openEdit}
                    title="Edit store details — opening hours, manager phone, queue targets"
                    aria-label="Edit store details"
                    className="text-slate-500 hover:text-slate-900 px-2 py-1 rounded
                               hover:bg-slate-100 text-base">
              ✏️
            </button>
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

      {/* STORE HEALTH SCORE + Plain-English summary + AI narrative —
          Commit 2 of the dashboard revamp. All three derive from the
          tiles payload, no new API surface. */}
      {(() => {
        const health = computeStoreHealth(t as any, data.status_light)
        return (
          <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <StoreHealthCard health={health} />
            <TodayPlainEnglishPanel tiles={t as any} />
          </section>
        )
      })()}
      <AiNarrative storeName={data.store_name} tiles={t as any} />
      <StaffPerformancePanel tiles={t as any} />

      {/* RIGHT NOW — values are last-known when the store is closed.
          The dimmed prop greys the tile and prints a small "Store
          closed" pill so operators know this isn't a live reading. */}
      <section>
        <SectionTitle>What's happening right now</SectionTitle>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Kpi label="People in store" big dimmed={closed} value={fmtInt(t.occupancy_now?.value)} />
          {t.queue_length_now?.visible && (
            <Kpi label="People in queue" big dimmed={closed} value={fmtInt(t.queue_length_now.value)} />
          )}
          {t.staff_present_pct_today?.visible && (
            <Kpi label="Time staff were at counter" big dimmed={closed}
                 value={`${fmtInt(t.staff_present_pct_today.value)}%`} />
          )}
          <Kpi label="Cameras live" value={`${data.cameras_online ?? 0}/${data.cameras_total ?? data.camera_count ?? 0}`} />
        </div>
      </section>

      {/* PEAK PREDICTION — 7-day historical average. */}
      <PeakPredictionBanner storeId={storeId} />

      {/* TODAY SO FAR */}
      <section>
        <SectionTitle>How today compares to yesterday</SectionTitle>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {/* Every tile in this section now carries trend vs the prior
              same-length window via t.<key>.trend. */}
          <Kpi label="Customers today"
               value={fmtInt(t.unique_visitors_today?.value)}
               trendDir={t.unique_visitors_today?.trend?.direction}
               trendPct={t.unique_visitors_today?.trend?.delta_pct ?? null}
               badge={visitorBadge(t.unique_visitors_today?.data_source)} />
          <Kpi label="Most people at once"
               value={fmtInt(t.occupancy_peak_today?.value)}
               trendDir={t.occupancy_peak_today?.trend?.direction}
               trendPct={t.occupancy_peak_today?.trend?.delta_pct ?? null} />
          {t.checkout_avg_seconds_today?.visible && (
            <Kpi label="Avg checkout time"
                 value={`${fmtInt(t.checkout_avg_seconds_today.value)} sec`}
                 trendDir={t.checkout_avg_seconds_today?.trend?.direction}
                 trendPct={t.checkout_avg_seconds_today?.trend?.delta_pct ?? null} />
          )}
          {t.checkout_max_seconds_today?.visible && (
            <Kpi label="Slowest checkout"
                 value={`${fmtInt(t.checkout_max_seconds_today.value)} sec`}
                 trendDir={t.checkout_max_seconds_today?.trend?.direction}
                 trendPct={t.checkout_max_seconds_today?.trend?.delta_pct ?? null} />
          )}
          {t.checkout_count_today?.visible && (
            <Kpi label="Checkouts today"
                 value={fmtInt(t.checkout_count_today.value)}
                 trendDir={t.checkout_count_today?.trend?.direction}
                 trendPct={t.checkout_count_today?.trend?.delta_pct ?? null} />
          )}
          {t.checkout_busiest_hour_today?.visible
              && t.checkout_busiest_hour_today.value != null && (
            <Kpi label="Busiest checkout hour"
                 value={`${String(t.checkout_busiest_hour_today.value).padStart(2,'0')}:00`} />
          )}
          {t.queue_wait_avg_today_sec?.visible && (
            <Kpi label="Average wait in queue"
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

      {/* BI panels — six interactive insight cards replacing the
          old "aisles + alerts + footfall" three-column row and the
          standalone heatmap thumbnail. Each panel self-fetches,
          self-refreshes every 30s, and is independently collapsible.
          firstCameraId is parsed from the live tile's heatmap URL
          so the heatmap-intelligence panel can render the thumb. */}
      {/* Progressive loading. Right Now + Today So Far above render
          instantly. The heavier sections below appear in order so
          the page is interactive within a few hundred ms even at
          117-camera scale, instead of waiting for every endpoint. */}
      <StaggeredSection delayMs={500}>
        <WeekSection storeId={storeId} />
      </StaggeredSection>

      <StaggeredSection delayMs={800}>
        <AlertsFeedSection storeId={storeId} />
      </StaggeredSection>

      {/* AI intelligence + BI panels are HEAVY — only fetch their
          code chunks when the operator scrolls them into view. */}
      <ScrollMounted>
        <section>
          <SectionTitle>AI intelligence</SectionTitle>
          <Suspense fallback={<div className="text-slate-400 text-sm p-4">Loading insights…</div>}>
            <StoreAIIntelligence storeId={storeId} range={range} />
          </Suspense>
        </section>

        <section className="mt-6">
          <SectionTitle>Store intelligence</SectionTitle>
          <Suspense fallback={<div className="text-slate-400 text-sm p-4">Loading charts…</div>}>
            <StoreBIPanels
              storeId={storeId}
              range={range}
              firstCameraId={extractFirstCameraId(t.heatmap_thumb_url?.value as string | null)} />
          </Suspense>
        </section>
      </ScrollMounted>

      {/* Edit-store modal — opens from the pen icon next to the title.
          Reuses the same StoreForm as /stores, so opening hours, manager
          phone, queue SLA, default RTSP port etc. are all editable in
          one place. */}
      {editing && (
        <div className="fixed inset-0 bg-black/50 flex items-start justify-center z-50 p-6 overflow-auto"
             onClick={() => !editBusy && setEditing(false)}>
          <div onClick={e => e.stopPropagation()} className="w-full max-w-3xl">
            {storeForEdit ? (
              <StoreForm initial={storeForEdit}
                         submitLabel={editBusy ? 'Saving…' : 'Save store'}
                         onSubmit={saveEdit}
                         onCancel={() => setEditing(false)} />
            ) : (
              <Card className="p-6 text-sm text-slate-600">Loading store details…</Card>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

// Compact banner showing the predicted busy window for today, derived
// from the last 7 days of visitor traffic. Hidden when the API has
// no prediction yet (cold start).
function PeakPredictionBanner({ storeId }: { storeId: number }) {
  const [pred, setPred] = useState<{ headline: string; label: string | null } | null>(null)
  useEffect(() => {
    fetch(`/api/analytics/store/${storeId}/peak-prediction`, {
      headers: { Authorization: `Bearer ${localStorage.getItem('vg_access_token') ?? ''}` },
    }).then(r => r.ok ? r.json() : null).then(d => { if (d) setPred(d) }).catch(() => {})
  }, [storeId])
  if (!pred || !pred.label) return null
  return (
    <div className="rounded-md border border-sky-200 bg-sky-50 text-sky-800 px-3 py-2 text-sm">
      🔮 {pred.headline}
    </div>
  )
}


// Progressive-loading helpers
// ----------------------------------------------------------------
// Stagger: render children after a short delay so the main thread
// gets to paint critical content first. Used for the moderate-cost
// sections (week summary, alerts feed).
function StaggeredSection({ delayMs, children }: { delayMs: number; children: React.ReactNode }) {
  const [show, setShow] = useState(false)
  useEffect(() => {
    const t = setTimeout(() => setShow(true), delayMs)
    return () => clearTimeout(t)
  }, [delayMs])
  return show ? <>{children}</> : null
}

// Scroll-mounted: render children only when a sentinel above is in
// the viewport. For the heavy AI / BI panel bundles — the chart code
// isn't even downloaded until the operator scrolls toward it.
function ScrollMounted({ children }: { children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement | null>(null)
  const [mounted, setMounted] = useState(false)
  useEffect(() => {
    if (mounted) return
    const el = ref.current
    if (!el) return
    const io = new IntersectionObserver(entries => {
      for (const e of entries) if (e.isIntersecting) setMounted(true)
    }, { rootMargin: '300px 0px' })
    io.observe(el)
    return () => io.disconnect()
  }, [mounted])
  return (
    <>
      <div ref={ref} />
      {mounted && children}
    </>
  )
}

// ----- small bits -----

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h2 className="text-sm font-semibold text-slate-500 uppercase tracking-wide mb-2">{children}</h2>
}

function Kpi({ label, value, big, sub, trendDir, trendPct, dimmed, badge }: {
  label: string; value: string; big?: boolean; sub?: string
  trendDir?: 'up' | 'down' | 'flat'; trendPct?: number | null
  // When true, greys the tile and shows a small "Store closed" pill
  // — used on the Right Now row so operators see last-known values
  // while still understanding the store isn't currently trading.
  dimmed?: boolean
  // Optional provenance pill rendered under the trend (e.g. for the
  // visitor count: 🚪 High accuracy / ~estimated). Kept generic so
  // other tiles can reuse it.
  badge?: { text: string; tone: 'emerald' | 'slate' | 'amber' } | null
}) {
  const badgeCls =
    badge?.tone === 'emerald' ? 'bg-emerald-50 text-emerald-700 border-emerald-200' :
    badge?.tone === 'amber'   ? 'bg-amber-50  text-amber-700  border-amber-200' :
                                 'bg-slate-50  text-slate-600  border-slate-200'
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
      {badge && (
        <div className={'mt-1 inline-block text-[10px] px-1.5 py-0.5 rounded '
                         + 'border ' + badgeCls}>
          {badge.text}
        </div>
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


// Map the /store/{id}/live tile's `data_source` to a small provenance
// pill rendered next to the visitor count. Glass-door sourced counts
// are flagged as high-accuracy; occupancy-fallback is flagged as a
// rough estimate; plain entry-exit + unique-visitors render no badge
// (the default count is trustworthy enough that the chrome would
// just add noise).
function visitorBadge(source: string | null | undefined):
    { text: string; tone: 'emerald' | 'slate' | 'amber' } | null {
  switch (source) {
    case 'visitor_count_in_glass_door':
      return { text: '🚪 High accuracy count', tone: 'emerald' }
    case 'occupancy':
      return { text: '~estimated', tone: 'amber' }
    default:
      return null
  }
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

// Per-store alerts feed — uses the shared AlertCard component so the
// presentation matches the chain /alerts page exactly. Server hands
// us pre-rendered title/body/severity per row.
function AlertsFeedSection({ storeId }: { storeId: number }) {
  const [rows, setRows] = useState<AlertRowFull[] | null>(null)
  const load = () => api<AlertRowFull[]>(`/alerts?store_id=${storeId}&limit=50`)
    .then(setRows).catch(() => setRows([]))
  useEffect(() => {
    load()
    const t = setInterval(load, 30_000)
    return () => clearInterval(t)
  }, [storeId])

  if (rows === null) return null
  const groups = groupAlerts(rows)

  return (
    <section>
      <SectionTitle>Alerts &amp; incidents</SectionTitle>
      <Card className="p-3 space-y-2">
        {groups.length === 0 ? (
          <div className="p-3 text-sm">
            <div className="text-slate-500">No alerts in the last window.</div>
            <div className="text-xs text-slate-400 mt-1">
              If you expect alerts but see none, check that detectors are
              enabled on each camera under{' '}
              <Link to="/detectors" className="text-sky-600 hover:underline">Detectors</Link>{' '}
              and that detection_events are being written. From the host:
              <pre className="bg-slate-50 rounded p-2 mt-1 text-[11px] font-mono">{`docker compose exec postgres psql -U $POSTGRES_USER $POSTGRES_DB \\
  -c "SELECT COUNT(*) FROM detection_events
      WHERE timestamp > now() - interval '1 hour';"`}</pre>
            </div>
          </div>
        ) : (
          groups.map(g => (
            <AlertCard key={g.head.id}
                       alert={g.head}
                       groupCount={g.count}
                       groupLast={g.last}
                       groupSiblings={g.siblings}
                       onChanged={load} />
          ))
        )}
      </Card>
    </section>
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
