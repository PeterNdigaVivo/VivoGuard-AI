// Store Health Score + plain-English summary + AI narrative panels
// (Commit 2 of the dashboard revamp).
//
// All three derive 100% from the LiveResponse.tiles payload the store
// dashboard already loads — no new API surface — so the components
// can drop in above the existing "Right now" / "Today so far" grids
// without any backend work.

import type { ReactNode } from 'react'

// Loose shape of the live-dashboard tiles we read from. Kept here so
// the panels are self-contained; the real type lives in
// StoreDashboardPage.tsx but isn't exported.
type Tile = {
  value: any
  visible: boolean
  trend?: { direction: 'up' | 'down' | 'flat'; delta_pct: number | null }
}
type Tiles = Record<string, Tile | undefined>


// ---------------------------------------------------------------
// Score calculator
// ---------------------------------------------------------------
// 0–100 store health, derived from the existing tile payload:
//   25  staff presence at counter         (staff_present_pct_today)
//   20  queue wait normal                 (queue_wait_avg_today_sec)
//   20  uniform compliance                (uniform_compliance_pct_today)
//   20  low incident rate                 (status_light fallback)
//   15  opened on time                    (shop_opened_today)
// When a signal isn't available we give full credit rather than punish
// stores for not having that camera type configured.

interface ScoreFactor {
  key: string
  label: string
  emoji: string
  points: number
  maxPoints: number
  ok: 'good' | 'warn' | 'bad'
  detail: string         // plain-English explanation under the bar
}

export interface StoreHealthScore {
  total: number               // 0–100
  band: 'great' | 'good' | 'needs_work' | 'poor'
  bandEmoji: string
  bandLabel: string
  bandCopy: string
  factors: ScoreFactor[]
  // Optional vs-yesterday delta. Computed only when at least one
  // factor has a trend.delta_pct on its source tile.
  vsYesterdayPts: number | null
}

function _band(total: number): Pick<StoreHealthScore,
  'band' | 'bandEmoji' | 'bandLabel' | 'bandCopy'> {
  if (total >= 80) return { band: 'great',     bandEmoji: '🟢',
                            bandLabel: 'Great',
                            bandCopy: 'Store is running well today' }
  if (total >= 60) return { band: 'good',      bandEmoji: '🟡',
                            bandLabel: 'Good',
                            bandCopy: 'A few things need attention' }
  if (total >= 40) return { band: 'needs_work',bandEmoji: '🟠',
                            bandLabel: 'Needs Work',
                            bandCopy: 'Several issues need fixing' }
  return                  { band: 'poor',      bandEmoji: '🔴',
                            bandLabel: 'Poor',
                            bandCopy: 'Urgent attention required' }
}

function _numTile(t: Tile | undefined): number | null {
  if (!t || !t.visible) return null
  const v = t.value
  if (v == null) return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

export function computeStoreHealth(
  tiles: Tiles,
  statusLight: 'green' | 'amber' | 'red' | undefined,
): StoreHealthScore {
  // Factor 1 — staff coverage at counter.
  const staffPct = _numTile(tiles.staff_present_pct_today)
  const f1Pts = staffPct == null ? 25
              : Math.round((Math.min(100, Math.max(0, staffPct)) / 100) * 25)
  const f1 = staffPct == null
    ? { ok: 'good' as const, detail: 'Counter coverage not measured on this store yet.' }
    : staffPct >= 80 ? { ok: 'good'  as const, detail: `Staff at counter ${Math.round(staffPct)}% of opening hours` }
    : staffPct >= 60 ? { ok: 'warn'  as const, detail: `Counter coverage ${Math.round(staffPct)}% — a few gaps detected` }
    : { ok: 'bad' as const, detail: `Counter coverage only ${Math.round(staffPct)}% — long gaps detected` }

  // Factor 2 — queue wait normal.
  const waitSec = _numTile(tiles.queue_wait_avg_today_sec)
  const f2Pts = waitSec == null
    ? 20
    : waitSec <= 120 ? 20      // ≤ 2 min: full marks
    : waitSec <= 240 ? 15
    : waitSec <= 360 ? 10
    : waitSec <= 480 ?  5
    : 0
  const fmtWait = waitSec == null ? '—'
                  : waitSec < 60 ? `${Math.round(waitSec)} sec`
                  : `${(waitSec / 60).toFixed(1)} min`
  const f2 = waitSec == null
    ? { ok: 'good' as const, detail: 'No queue camera configured — queue times not tracked.' }
    : waitSec <= 180 ? { ok: 'good' as const, detail: `Queue times normal (${fmtWait} average)` }
    : waitSec <= 300 ? { ok: 'warn' as const, detail: `Queue times slightly elevated (${fmtWait} average)` }
    : { ok: 'bad' as const, detail: `Queue times high (${fmtWait} average) — open another till` }

  // Factor 3 — uniform compliance.
  const uniformPct = _numTile(tiles.uniform_compliance_pct_today)
  const f3Pts = uniformPct == null ? 20
              : Math.round((Math.min(100, Math.max(0, uniformPct)) / 100) * 20)
  const f3 = uniformPct == null
    ? { ok: 'good' as const, detail: 'Uniform compliance not yet measured.' }
    : uniformPct >= 90 ? { ok: 'good' as const, detail: `Uniform compliance ${Math.round(uniformPct)}% — excellent` }
    : uniformPct >= 75 ? { ok: 'warn' as const, detail: `Uniform compliance ${Math.round(uniformPct)}% — a few exceptions` }
    : { ok: 'bad' as const, detail: `Uniform compliance only ${Math.round(uniformPct)}% — needs follow-up` }

  // Factor 4 — incidents (status_light proxy).
  //   green → 20  amber → 12  red → 4
  const f4Pts = statusLight === 'red' ? 4
              : statusLight === 'amber' ? 12
              : 20
  const f4 = statusLight === 'red'
    ? { ok: 'bad' as const, detail: 'High-priority incidents detected today' }
    : statusLight === 'amber'
    ? { ok: 'warn' as const, detail: 'A few operational issues today' }
    : { ok: 'good' as const, detail: 'Incidents: low — no critical alerts today' }

  // Factor 5 — opened on time.
  //   shop_opened_today tile: { value: { eat: "08:58", on_time: true } | null }
  //   If the tile is missing we treat as "—" and award the points (don't
  //   punish stores that haven't wired the entry/exit line yet).
  const openTile = tiles.shop_opened_today
  const openVal: any = openTile?.visible ? openTile.value : null
  let f5Pts = 15
  let f5: { ok: 'good' | 'warn' | 'bad'; detail: string }
  if (openVal && typeof openVal === 'object') {
    const eat: string = openVal.eat ?? openVal.eat_time ?? '?'
    if (openVal.on_time === false) {
      f5Pts = 5
      f5 = { ok: 'warn', detail: `Opened late at ${eat}` }
    } else if (openVal.on_time === true) {
      f5Pts = 15
      f5 = { ok: 'good', detail: `Opened on time at ${eat}` }
    } else {
      f5 = { ok: 'good', detail: `Opening time: ${eat}` }
    }
  } else {
    f5 = { ok: 'good', detail: 'Opening time not measured on this store yet' }
  }

  const factors: ScoreFactor[] = [
    { key: 'staff',     label: 'Staff at counter',        emoji: '👔', points: f1Pts, maxPoints: 25, ...f1 },
    { key: 'queue',     label: 'Queue times',             emoji: '⏱️', points: f2Pts, maxPoints: 20, ...f2 },
    { key: 'uniform',   label: 'Uniform compliance',      emoji: '🎽', points: f3Pts, maxPoints: 20, ...f3 },
    { key: 'incidents', label: 'Incident rate',           emoji: '🛡️', points: f4Pts, maxPoints: 20, ...f4 },
    { key: 'opened',    label: 'Opened on time',          emoji: '🏬', points: f5Pts, maxPoints: 15, ...f5 },
  ]
  const total = factors.reduce((s, f) => s + f.points, 0)
  // vs-yesterday — rough proxy from the staff_present trend (the
  // dominant input). If absent we report null.
  const staffTrend = tiles.staff_present_pct_today?.trend
  const vsYesterdayPts = staffTrend && staffTrend.delta_pct != null
    ? Math.round(staffTrend.delta_pct * 0.5)   // ±50% trend → ±25 pts shift, halve to be conservative
    : null

  return { total, ...(_band(total)), factors, vsYesterdayPts }
}


// ---------------------------------------------------------------
// Score card UI
// ---------------------------------------------------------------

function _factorEmoji(ok: 'good' | 'warn' | 'bad'): string {
  return ok === 'good' ? '✅' : ok === 'warn' ? '⚠️' : '🔴'
}
function _bandColor(band: StoreHealthScore['band']): string {
  return band === 'great' ? 'text-emerald-600'
       : band === 'good'  ? 'text-yellow-600'
       : band === 'needs_work' ? 'text-orange-600'
       : 'text-red-600'
}

export function StoreHealthCard({ health }: { health: StoreHealthScore }) {
  const trend = health.vsYesterdayPts
  return (
    <section className="rounded-lg border border-slate-200 bg-white shadow-sm p-4">
      <div className="text-xs uppercase tracking-wide text-slate-500 mb-1">
        Store Health Score
      </div>
      <div className="flex items-baseline gap-3 mb-1">
        <div className={`text-4xl font-semibold tabular-nums ${_bandColor(health.band)}`}>
          {health.total}<span className="text-lg text-slate-400">/100</span>
        </div>
        <div className={`text-sm font-medium ${_bandColor(health.band)}`}>
          {health.bandEmoji} {health.bandLabel}
        </div>
      </div>
      <div className="text-xs text-slate-500 mb-3">{health.bandCopy}</div>
      <ul className="text-sm space-y-1">
        {health.factors.map(f => (
          <li key={f.key} className="flex items-baseline gap-2">
            <span>{_factorEmoji(f.ok)}</span>
            <span className="text-slate-700">{f.detail}</span>
            <span className="ml-auto text-xs text-slate-400 tabular-nums">
              {f.points}/{f.maxPoints}
            </span>
          </li>
        ))}
      </ul>
      {trend != null && (
        <div className="mt-3 text-xs">
          vs Yesterday:{' '}
          <span className={trend > 0 ? 'text-emerald-600'
                          : trend < 0 ? 'text-red-600' : 'text-slate-500'}>
            {trend > 0 ? '↑' : trend < 0 ? '↓' : '→'} {trend > 0 ? '+' : ''}{trend} points
            {trend > 0 ? ' improvement' : trend < 0 ? ' decline' : ''}
          </span>
        </div>
      )}
    </section>
  )
}


// ---------------------------------------------------------------
// Plain-English "What's happening today" panel
// ---------------------------------------------------------------

interface SummaryItem { emoji: string; text: ReactNode }

function _peakHourLabel(tiles: Tiles): string | null {
  const t = tiles.peak_hour_today
  if (!t || !t.visible || t.value == null) return null
  const h = Number(t.value)
  if (!Number.isFinite(h)) return null
  return `${String(h).padStart(2, '0')}:00 - ${String(h + 1).padStart(2, '0')}:00`
}

export function TodayPlainEnglishPanel({ tiles }: { tiles: Tiles }) {
  const items: SummaryItem[] = []
  const visitors = _numTile(tiles.unique_visitors_today)
  if (visitors != null) {
    items.push({ emoji: '👥', text: <><strong>{visitors}</strong> customers visited today</> })
  }
  const browse = _numTile(tiles.browse_time_seconds_today)
                 ?? _numTile(tiles.dwell_seconds_today)
  if (browse != null && browse > 0) {
    const m = Math.floor(browse / 60), s = Math.round(browse % 60)
    const txt = m && s ? `${m} minutes ${s} seconds`
              : m      ? `${m} minutes`
              :          `${s} seconds`
    items.push({ emoji: '⏱️', text: <>They spent an average of <strong>{txt}</strong> browsing</> })
  }
  const peak = _peakHourLabel(tiles)
  const peakCount = _numTile(tiles.peak_hour_count_today)
  if (peak) {
    items.push({
      emoji: '🏆',
      text: peakCount != null
        ? <>Busiest time: <strong>{peak}</strong> ({peakCount} customers)</>
        : <>Busiest time: <strong>{peak}</strong></>,
    })
  }
  const topZone = tiles.top_browse_zone_today?.value
  if (topZone && typeof topZone === 'object') {
    const name = topZone.name ?? topZone.zone_name
    const avg = Number(topZone.avg_seconds ?? topZone.avg ?? 0)
    if (name) {
      const m = Math.floor(avg / 60), s = Math.round(avg % 60)
      items.push({
        emoji: '🛍️',
        text: <>Most browsed area: <strong>{name}</strong>{
          avg > 0 ? ` (${m}:${String(s).padStart(2, '0')} avg)` : ''}</>,
      })
    }
  }
  const quiet = tiles.quiet_hour_today?.value
  if (quiet != null) {
    const h = Number(quiet)
    if (Number.isFinite(h)) {
      items.push({
        emoji: '😴',
        text: <>Quietest time: <strong>
          {String(h).padStart(2, '0')}:00 - {String(h + 1).padStart(2, '0')}:00
        </strong></>,
      })
    }
  }
  const longestQ = _numTile(tiles.queue_peak_today)
  if (longestQ != null && longestQ > 0) {
    items.push({ emoji: '🔢', text: <>Longest queue today: <strong>{longestQ}</strong> people</> })
  }
  const staff = _numTile(tiles.staff_present_pct_today)
  if (staff != null) {
    items.push({
      emoji: '✅',
      text: <>Counter covered: <strong>{Math.round(staff)}%</strong> of the day</>,
    })
  }

  return (
    <section className="rounded-lg border border-slate-200 bg-white shadow-sm p-4">
      <div className="text-xs uppercase tracking-wide text-slate-500 mb-2">
        What's Happening in Your Store Today
      </div>
      {items.length === 0 ? (
        <div className="text-sm text-slate-500">
          Store data is still being collected.
          Check back after 9:00 AM.
        </div>
      ) : (
        <ul className="text-sm space-y-1.5">
          {items.map((it, i) => (
            <li key={i} className="flex items-baseline gap-2">
              <span>{it.emoji}</span>
              <span className="text-slate-700">{it.text}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}


// ---------------------------------------------------------------
// AI narrative — auto-composed paragraph
// ---------------------------------------------------------------

export function AiNarrative({ storeName, tiles }: {
  storeName: string
  tiles: Tiles
}) {
  const visitors = _numTile(tiles.unique_visitors_today)
  const trend = tiles.unique_visitors_today?.trend
  const peak = _peakHourLabel(tiles)
  const peakCount = _numTile(tiles.peak_hour_count_today)
  const browseSec = _numTile(tiles.browse_time_seconds_today)
                    ?? _numTile(tiles.dwell_seconds_today)
  const topZone = tiles.top_browse_zone_today?.value
  const topZoneName = topZone && typeof topZone === 'object'
    ? (topZone.name ?? topZone.zone_name ?? null) : null
  const staff = _numTile(tiles.staff_present_pct_today)
  const queueWait = _numTile(tiles.queue_wait_avg_today_sec)
  const longestQ = _numTile(tiles.queue_peak_today)

  if (visitors == null && peak == null && staff == null && browseSec == null) {
    return (
      <section className="rounded-lg border border-slate-200 bg-slate-50 p-4">
        <div className="text-xs uppercase tracking-wide text-slate-500 mb-1">
          Today at {storeName}
        </div>
        <p className="text-sm text-slate-600">
          Store data is still being collected.
          Check back after 9:00 AM.
        </p>
      </section>
    )
  }

  const sentences: string[] = []

  // Opener — visitors + trend.
  if (visitors != null) {
    let compare = ''
    if (trend && trend.delta_pct != null) {
      const p = trend.delta_pct
      if (Math.abs(p) < 5)       compare = ', about the same as yesterday'
      else if (p > 0)            compare = `, ${Math.round(p)}% more than yesterday`
      else                       compare = `, ${Math.abs(Math.round(p))}% fewer than yesterday`
    }
    sentences.push(`${storeName} had ${visitors} customer${visitors === 1 ? '' : 's'} today${compare}.`)
  }

  // Best-performing area.
  if (topZoneName && browseSec != null && browseSec > 0) {
    const m = Math.floor(browseSec / 60), s = Math.round(browseSec % 60)
    const dur = m ? `${m} minute${m === 1 ? '' : 's'} ${s} seconds` : `${s} seconds`
    sentences.push(`The ${topZoneName} section got the most attention — customers spent an average of ${dur} there.`)
  } else if (peak && peakCount != null) {
    sentences.push(`Between ${peak} the store was at its busiest with ${peakCount} customer${peakCount === 1 ? '' : 's'} browsing.`)
  }

  // Staff coverage.
  if (staff != null) {
    if (staff >= 85)      sentences.push(`Your staff covered the counter ${Math.round(staff)}% of the day — excellent coverage.`)
    else if (staff >= 70) sentences.push(`Your staff covered the counter ${Math.round(staff)}% of the day.`)
    else                  sentences.push(`Counter coverage was only ${Math.round(staff)}% today — several gaps detected.`)
  }

  // Queue situation.
  if (queueWait != null) {
    if (queueWait <= 120) {
      sentences.push(`Queue times were normal throughout the day, averaging ${(queueWait / 60).toFixed(1)} minutes. No long queues were detected.`)
    } else if (queueWait <= 300) {
      sentences.push(`Queue times averaged ${(queueWait / 60).toFixed(1)} minutes today — slightly elevated.`)
    } else {
      const txt = longestQ != null ? ` Longest queue today: ${longestQ} people.` : ''
      sentences.push(`Queue times averaged ${(queueWait / 60).toFixed(1)} minutes today — high.${txt}`)
    }
  }

  return (
    <section className="rounded-lg border border-slate-200 bg-slate-50 p-4">
      <div className="text-xs uppercase tracking-wide text-slate-500 mb-1">
        Today at {storeName}
      </div>
      <p className="text-sm text-slate-700 leading-relaxed">
        {sentences.join(' ')}
      </p>
    </section>
  )
}


// ---------------------------------------------------------------
// Staff Performance section
// ---------------------------------------------------------------

interface StaffBar { label: string; pct: number; trend: number | null; copy: string }

export function StaffPerformancePanel({ tiles, todayAlertHandledPct }: {
  tiles: Tiles
  todayAlertHandledPct?: number | null
}) {
  const coverage = _numTile(tiles.staff_present_pct_today)
  const coverageTrend = tiles.staff_present_pct_today?.trend?.delta_pct ?? null
  const uniform  = _numTile(tiles.uniform_compliance_pct_today)
  const uniformTrend = tiles.uniform_compliance_pct_today?.trend?.delta_pct ?? null
  const bars: StaffBar[] = []
  if (coverage != null) {
    bars.push({
      label: 'Counter Coverage',
      pct: Math.max(0, Math.min(100, coverage)),
      trend: coverageTrend,
      copy: `Staff covered the counter for ${Math.round(coverage)}% of opening hours.`,
    })
  }
  if (uniform != null) {
    bars.push({
      label: 'Uniform Compliance',
      pct: Math.max(0, Math.min(100, uniform)),
      trend: uniformTrend,
      copy: uniform >= 90 ? 'All staff in full uniform today — well done.'
          : `${Math.max(1, Math.round((100 - uniform) / 10))} staff seen without full uniform.`,
    })
  }
  if (todayAlertHandledPct != null) {
    bars.push({
      label: 'Alerts Responded',
      pct: Math.max(0, Math.min(100, todayAlertHandledPct)),
      trend: null,
      copy: todayAlertHandledPct >= 90
        ? 'Almost all alerts were handled quickly — good job.'
        : `${Math.round(todayAlertHandledPct)}% of alerts have been handled today.`,
    })
  }
  if (!bars.length) return null

  return (
    <section className="rounded-lg border border-slate-200 bg-white shadow-sm p-4">
      <div className="text-xs uppercase tracking-wide text-slate-500 mb-2">
        Staff Performance Today
      </div>
      <ul className="space-y-3">
        {bars.map(b => {
          const tone = b.pct >= 80 ? 'bg-emerald-500'
                     : b.pct >= 60 ? 'bg-yellow-500'
                     : 'bg-red-500'
          return (
            <li key={b.label}>
              <div className="flex items-baseline justify-between text-sm">
                <span className="text-slate-700">{b.label}</span>
                <span className="tabular-nums text-slate-600">
                  {Math.round(b.pct)}%
                  {b.trend != null && Math.abs(b.trend) >= 1 && (
                    <span className={'ml-2 text-xs ' + (b.trend > 0 ? 'text-emerald-600' : 'text-red-600')}>
                      {b.trend > 0 ? '↑' : '↓'} {Math.abs(Math.round(b.trend))}% vs yesterday
                    </span>
                  )}
                </span>
              </div>
              <div className="mt-1 h-2 rounded bg-slate-100 overflow-hidden">
                <div className={'h-full ' + tone} style={{ width: `${b.pct}%` }} />
              </div>
              <div className="mt-1 text-xs text-slate-500">{b.copy}</div>
            </li>
          )
        })}
      </ul>
    </section>
  )
}
