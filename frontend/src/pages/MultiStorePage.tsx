// Chain dashboard — executive "Vibrant Bento" view for Vivo head office.
//
// Same route (/chain), same nav label, same data sources as before — this
// is a visual/layout revamp only. All figures are live:
//   analytics.multiDashboard({since,until})        — KPIs, per-store rows
//   /analytics/chain/top-issues                    — ranked issues
//   /analytics/chain/health-leaderboard            — store health scores
//   /analytics/chain/opening-status                — today's opening state
//   /analytics/chain/hourly-comparison             — hourly footfall
//   /api/stores                                    — cheap bootstrap list
//
// Auto-refreshes every 60s. No new endpoints, no changed data shapes.

import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import DateRangePicker, { rangeFor, type DateRange } from '@/components/DateRangePicker'
import { analytics } from '@/api/stores'
import { labelForDetector } from '@/lib/detectorLabels'
import { api } from '@/api/client'

type RAG = 'red' | 'amber' | 'green'

// ── Vibrant Bento tokens ──────────────────────────────────────────────
const GRAD_PRIMARY = 'linear-gradient(135deg,#7c3aed 0%,#ec4899 100%)'
const GRAD_BLUE    = 'linear-gradient(135deg,#3b82f6 0%,#14b8a6 100%)'
const C = {
  purple: '#7c3aed', pink: '#ec4899', orange: '#f97316',
  red: '#ef4444', teal: '#14b8a6', blue: '#3b82f6', slate: '#94a3b8',
}
// Health bands → colours (teal/blue positive, orange/red warning).
const BAND = {
  great: { label: 'Great (80–100)', color: C.teal },
  good:  { label: 'Good (60–79)',   color: C.blue },
  needs_work: { label: 'Needs Work (40–59)', color: C.orange },
  poor:  { label: 'Poor (0–39)',    color: C.red },
} as const
type Band = keyof typeof BAND

export default function MultiStorePage() {
  const [range, setRange] = useState<DateRange>(() => rangeFor('last_7_days'))
  const [data, setData] = useState<Awaited<ReturnType<typeof analytics.multiDashboard>> | null>(null)
  const [sortBy, setSortBy] = useState<string>('rag_status')
  // "Visitors by Store" — Top 10 by default, expandable to all stores.
  const [showAllVisitors, setShowAllVisitors] = useState(false)

  const [bootstrapStores, setBootstrapStores] = useState<{ id: number; name: string; country: string }[]>([])
  useEffect(() => {
    fetch('/api/stores', {
      headers: { Authorization: `Bearer ${localStorage.getItem('vg_access_token') ?? ''}` },
    }).then(r => r.ok ? r.json() : [])
      .then(rows => setBootstrapStores(rows || []))
      .catch(() => {})
  }, [])

  useEffect(() => {
    let inFlight = false
    const fetchOnce = () => {
      if (inFlight) return
      inFlight = true
      analytics.multiDashboard({ since: range.since, until: range.until })
        .then(setData).catch(console.error)
        .finally(() => { inFlight = false })
    }
    fetchOnce()
    const t = setInterval(fetchOnce, 60_000)
    return () => clearInterval(t)
  }, [range.since, range.until])

  const rows = useMemo(() => {
    if (!data) return []
    const ragWeight: Record<RAG, number> = { red: 0, amber: 1, green: 2 }
    const arr = [...data.stores]
    arr.sort((a, b) => {
      if (sortBy !== 'rag_status') {
        const av = pluck(a, sortBy), bv = pluck(b, sortBy)
        if (typeof av === 'number' && typeof bv === 'number') return bv - av
        const cmp = String(av ?? '').localeCompare(String(bv ?? ''))
        if (cmp !== 0) return cmp
      }
      return ragWeight[a.rag_status] - ragWeight[b.rag_status]
    })
    return arr
  }, [data, sortBy])

  // ── Loading skeleton (keeps the bento look while metrics load) ──────
  if (!data) {
    return (
      <div className="min-h-full p-6 bg-[#f6f5fb] dark:bg-slate-900">
        <ExecHeader subtitle="Loading network metrics…" />
        <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-4 mt-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <Bento key={i}><div className="h-16 animate-pulse rounded-lg bg-purple-50" /></Bento>
          ))}
        </div>
        <Bento className="mt-4">
          <SectionTitle icon="🏬" title="Stores" />
          <ul className="mt-2 divide-y divide-slate-100 dark:divide-slate-700 text-sm">
            {bootstrapStores.slice(0, 12).map(s => (
              <li key={s.id} className="flex justify-between py-2">
                <span className="font-medium text-slate-700 dark:text-slate-200">{s.name}</span>
                <span className="text-slate-400">{s.country} · loading…</span>
              </li>
            ))}
            {bootstrapStores.length === 0 && <li className="py-8 text-center text-slate-400">Loading…</li>}
          </ul>
        </Bento>
      </div>
    )
  }

  // ── Derived figures (all live) ──────────────────────────────────────
  const isHistorical = range.key !== 'today'
  const storeVisitors = (s: { kpis: { unique_visitors_today: number; unique_visitors_in_window?: number } }) =>
    (isHistorical ? s.kpis.unique_visitors_in_window : s.kpis.unique_visitors_today) ?? 0
  const totalVisitors = (isHistorical
    ? (data.totals.unique_visitors_in_window ?? data.totals.unique_visitors_today)
    : data.totals.unique_visitors_today) || 0

  const camsOnline = data.stores.reduce((a, s) => a + (s.cameras_online || 0), 0)
  const camsTotal  = data.stores.reduce((a, s) => a + (s.cameras_total || 0), 0)
  const uptimePct  = camsTotal > 0 ? Math.round((camsOnline / camsTotal) * 100) : null

  const staffVals  = data.stores.map(s => s.kpis.staff_present_avg).filter((x): x is number => x != null)
  const avgStaffPct = staffVals.length
    ? Math.round((staffVals.reduce((a, b) => a + b, 0) / staffVals.length) * 100) : null
  const STAFF_TARGET = 80

  const attentionPct = data.totals.stores > 0
    ? Math.round((data.totals.stores_attention / data.totals.stores) * 100) : 0

  const byCountry = (() => {
    const m = new Map<string, { country: string; visitors: number; stores: number; camsOnline: number; camsTotal: number }>()
    for (const s of data.stores) {
      const k = s.country || '—'
      const e = m.get(k) ?? { country: k, visitors: 0, stores: 0, camsOnline: 0, camsTotal: 0 }
      e.visitors += storeVisitors(s); e.stores += 1
      e.camsOnline += s.cameras_online || 0; e.camsTotal += s.cameras_total || 0
      m.set(k, e)
    }
    return [...m.values()].sort((a, b) => b.visitors - a.visitors)
  })()
  const maxCountryV = Math.max(1, ...byCountry.map(c => c.visitors))

  const coverageRisk = data.stores.filter(s => s.cameras_total > 0 && s.cameras_online === 0)
  const sortedVisitors = [...data.stores].sort((a, b) => storeVisitors(b) - storeVisitors(a))
  const topVisitors = sortedVisitors.slice(0, 10)      // always-shown Top 10
  const extraVisitors = sortedVisitors.slice(10)       // revealed on "View more"
  const maxStoreV = Math.max(1, ...sortedVisitors.map(storeVisitors))
  const attentionRows = rows.filter(r => r.rag_status !== 'green' || (r.cameras_total > 0 && r.cameras_online === 0))

  return (
    <div className="min-h-full p-6 space-y-4 bg-[#f6f5fb] dark:bg-slate-900">
      <ExecHeader
        subtitle={`Vivo Fashion Group · ${data.totals.stores} stores · Kenya · Uganda · Rwanda`}
        right={
          <div className="flex flex-col items-end gap-1">
            <DateRangePicker value={range} onChange={setRange} />
            <span className="text-[11px] text-slate-500 dark:text-slate-300">
              {data.totals.stores_open}/{data.totals.stores} open · updated {new Date(data.as_of).toLocaleTimeString()}
            </span>
          </div>
        }
      />

      {/* ── KPI bento row (6) ── */}
      <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-4">
        <Kpi label="Chain Visitors" value={fmtInt(totalVisitors)} gradient={GRAD_PRIMARY}
             sub={range.label} />
        <Kpi label="Stores Needing Attention"
             value={`${data.totals.stores_attention}/${data.totals.stores}`}
             accent={data.totals.stores_attention > 0 ? C.orange : C.teal}
             sub={`${attentionPct}% of network`} />
        <Kpi label="Critical Alerts (1h)" value={fmtInt(data.totals.alerts_critical)}
             accent={data.totals.alerts_critical > 0 ? C.red : C.teal}
             sub={data.totals.alerts_critical > 0 ? 'needs review' : 'all clear'} />
        <Kpi label="Best Store by Traffic"
             value={data.best_store_today?.store_name ?? '—'}
             small accent={C.purple}
             sub={data.best_store_today ? `${fmtInt(data.best_store_today.visitors)} visitors` : 'no data yet'} />
        <Kpi label="Camera Network Uptime"
             value={uptimePct == null ? '—' : `${uptimePct}%`}
             accent={uptimePct == null ? C.slate : uptimePct >= 90 ? C.teal : uptimePct >= 70 ? C.orange : C.red}
             sub={`${camsOnline}/${camsTotal} live`} />
        <Kpi label="Avg Staff Presence"
             value={avgStaffPct == null ? '—' : `${avgStaffPct}%`}
             accent={avgStaffPct == null ? C.slate : avgStaffPct >= STAFF_TARGET ? C.teal : avgStaffPct >= 50 ? C.orange : C.red}
             sub={`target ${STAFF_TARGET}%`} />
      </div>

      {/* ── Top issues + health donut ── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <TopIssuesBento />
        <HealthDonutBento />
      </div>

      {/* ── Visitors by store + country footprint ── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Bento>
          <SectionTitle icon="🏆"
                        title={showAllVisitors ? 'Visitors by Store — All' : 'Visitors by Store — Top 10'}
                        note={range.label.toLowerCase()} />
          {topVisitors.length === 0 ? (
            <Empty text="No visitor data in this range." />
          ) : (
            <>
              <div className="mt-3 space-y-2">
                {topVisitors.map((s, i) => (
                  <GradientBarRow key={s.store_id} rank={i + 1}
                    label={s.store_name} to={`/stores/${s.store_id}`}
                    value={fmtInt(storeVisitors(s))}
                    pct={(storeVisitors(s) / maxStoreV) * 100} gradient={GRAD_PRIMARY} />
                ))}
              </div>

              {/* Extra stores (rank 11+) — the grid-rows 0fr→1fr trick
                  animates the auto height smoothly both ways. */}
              {extraVisitors.length > 0 && (
                <div className={'grid transition-[grid-template-rows] duration-300 ease-out '
                                + (showAllVisitors ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]')}>
                  <div className="overflow-hidden">
                    <div className="space-y-2 mt-2">
                      {extraVisitors.map((s, j) => (
                        <GradientBarRow key={s.store_id} rank={j + 11}
                          label={s.store_name} to={`/stores/${s.store_id}`}
                          value={fmtInt(storeVisitors(s))}
                          pct={(storeVisitors(s) / maxStoreV) * 100} gradient={GRAD_PRIMARY} />
                      ))}
                    </div>
                  </div>
                </div>
              )}

              {extraVisitors.length > 0 && (
                <button type="button"
                        onClick={() => setShowAllVisitors(v => !v)}
                        className="mt-3 w-full rounded-xl py-2 text-sm font-semibold
                                   text-purple-700 hover:bg-purple-50 transition-colors">
                  {showAllVisitors
                    ? 'View less ▲'
                    : `View more (${extraVisitors.length} more) ▼`}
                </button>
              )}
            </>
          )}
        </Bento>

        <Bento>
          <SectionTitle icon="🌍" title="Country Footprint" />
          <div className="mt-3 space-y-2">
            {byCountry.map(c => (
              <GradientBarRow key={c.country} label={c.country}
                value={fmtInt(c.visitors)}
                sub={`${c.stores} stores · ${c.camsOnline}/${c.camsTotal} cams`}
                pct={(c.visitors / maxCountryV) * 100} gradient={GRAD_BLUE} />
            ))}
          </div>
          <div className={'mt-4 rounded-xl px-3 py-2.5 text-sm ' +
            (coverageRisk.length ? 'bg-red-50 text-red-700' : 'bg-teal-50 text-teal-700')}>
            {coverageRisk.length ? (
              <>🚨 <strong>Coverage risk:</strong> {coverageRisk.length} store{coverageRisk.length > 1 ? 's' : ''} with
                {' '}installed cameras but <strong>0 live</strong> — {coverageRisk.map(s => s.store_name).slice(0, 4).join(', ')}
                {coverageRisk.length > 4 ? ` +${coverageRisk.length - 4} more` : ''}.</>
            ) : (<>✅ Every store with installed cameras has at least one live feed.</>)}
          </div>
        </Bento>
      </div>

      {/* ── Stores needing attention (worst-first) ── */}
      <Bento>
        <SectionTitle icon="🚨" title="Stores Needing Attention"
                      note={`${attentionRows.length} of ${data.totals.stores}`} />
        {attentionRows.length === 0 ? (
          <Empty text="🎉 Every store is healthy — nothing needs attention right now." />
        ) : (
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-sm min-w-[640px]">
              <thead>
                <tr className="text-left text-[11px] uppercase tracking-wide text-slate-400">
                  <th className="py-2 pr-3">Store</th>
                  <th className="py-2 pr-3">Country</th>
                  <th className="py-2 pr-3">Cameras</th>
                  <th className="py-2 pr-3">Visitors</th>
                  <th className="py-2 pr-3">Staff %</th>
                  <th className="py-2 pr-3">Status</th>
                </tr>
              </thead>
              <tbody>
                {attentionRows.map(r => (
                  <tr key={r.store_id} className="border-t border-slate-100 dark:border-slate-700">
                    <td className="py-2.5 pr-3 font-medium">
                      <Link to={`/stores/${r.store_id}`} className="text-purple-700 hover:underline">{r.store_name}</Link>
                    </td>
                    <td className="py-2.5 pr-3 text-slate-500 dark:text-slate-300">{r.country}</td>
                    <td className={'py-2.5 pr-3 tabular-nums ' +
                      (r.cameras_online === 0 && r.cameras_total > 0 ? 'text-red-600 font-semibold' : 'text-slate-600 dark:text-slate-300')}>
                      {r.cameras_online}/{r.cameras_total}
                    </td>
                    <td className="py-2.5 pr-3 tabular-nums text-slate-600 dark:text-slate-300">{fmtInt(storeVisitors(r))}</td>
                    <td className="py-2.5 pr-3 tabular-nums text-slate-600 dark:text-slate-300">
                      {fmt(r.kpis.staff_present_avg != null ? r.kpis.staff_present_avg * 100 : null, 0, '%')}
                    </td>
                    <td className="py-2.5 pr-3">{badgesFor(r).map((b, i) =>
                      <StatusBadge key={i} {...b} />)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Bento>

      {/* ── Preserved panels: opening status + hourly footfall ── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <OpeningStatusBento />
        <HourlyComparisonBento />
      </div>

      {/* ── Full store performance table (restyled, sortable) ── */}
      <Bento>
        <SectionTitle icon="📊" title="Store Performance" note="click a column to sort" />
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-sm min-w-[820px]">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wide text-slate-400">
                <Th id="rag_status" label="●" cur={sortBy} set={setSortBy} />
                <Th id="store_name" label="Store" cur={sortBy} set={setSortBy} />
                <Th id="country" label="Country" cur={sortBy} set={setSortBy} />
                <Th id="cameras_online" label="Cameras" cur={sortBy} set={setSortBy} />
                <Th id={isHistorical ? 'kpis.unique_visitors_in_window' : 'kpis.unique_visitors_today'}
                    label="Visitors" cur={sortBy} set={setSortBy} />
                <Th id="kpis.occupancy_avg" label="Occupancy" cur={sortBy} set={setSortBy} />
                <Th id="kpis.staff_present_avg" label="Staff %" cur={sortBy} set={setSortBy} />
                <Th id="recent_critical_alerts" label="Critical" cur={sortBy} set={setSortBy} />
                <th className="py-2 pr-3">Top alerts</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.store_id} className="border-t border-slate-100 dark:border-slate-700 hover:bg-purple-50/40">
                  <td className="py-2.5 pr-3"><RagDot status={r.rag_status} /></td>
                  <td className="py-2.5 pr-3 font-medium">
                    <Link to={`/stores/${r.store_id}`} className="text-purple-700 hover:underline">{r.store_name}</Link>
                    {!r.is_open && <span className="ml-2 text-[10px] text-slate-400">closed</span>}
                  </td>
                  <td className="py-2.5 pr-3 text-slate-500 dark:text-slate-300">{r.country}</td>
                  <td className={'py-2.5 pr-3 tabular-nums ' +
                    (r.cameras_online === 0 && r.cameras_total > 0 ? 'text-red-600 font-semibold' : 'text-slate-600 dark:text-slate-300')}>
                    {r.cameras_online}/{r.cameras_total}
                  </td>
                  <td className="py-2.5 pr-3 tabular-nums text-slate-600 dark:text-slate-300">{fmtInt(storeVisitors(r))}</td>
                  <td className="py-2.5 pr-3 tabular-nums text-slate-600 dark:text-slate-300">{fmt(r.kpis.occupancy_avg, 0)}</td>
                  <td className="py-2.5 pr-3 tabular-nums text-slate-600 dark:text-slate-300">{fmt(r.kpis.staff_present_avg != null ? r.kpis.staff_present_avg * 100 : null, 0, '%')}</td>
                  <td className={'py-2.5 pr-3 tabular-nums ' + (r.recent_critical_alerts > 0 ? 'text-red-600 font-semibold' : 'text-slate-600 dark:text-slate-300')}>
                    {r.recent_critical_alerts}
                  </td>
                  <td className="py-2.5 pr-3 space-x-1 space-y-1">
                    {Object.entries(r.alerts_breakdown).slice(0, 3).map(([t, n]) => (
                      <span key={t} className="inline-block text-[11px] bg-purple-50 text-purple-700 rounded-full px-2 py-0.5">
                        {labelForDetector(t)}: {n}
                      </span>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Bento>
    </div>
  )
}

// ══ Layout primitives (page-local) ═══════════════════════════════════════
function ExecHeader({ subtitle, right }: { subtitle: string; right?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 className="text-2xl font-bold tracking-tight"
            style={{ background: GRAD_PRIMARY, WebkitBackgroundClip: 'text', backgroundClip: 'text', color: 'transparent' }}>
          Executive Dashboard
        </h1>
        <p className="text-sm text-slate-500 dark:text-slate-300 mt-0.5">{subtitle}</p>
      </div>
      {right}
    </div>
  )
}

function Bento({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <section className={'rounded-2xl bg-white border border-purple-100/60 p-4 ' +
      'dark:bg-slate-800 dark:border-slate-700 ' +
      'shadow-[0_4px_24px_rgba(124,58,237,0.07)] ' + className}>
      {children}
    </section>
  )
}

function SectionTitle({ icon, title, note }: { icon: string; title: string; note?: string }) {
  return (
    <div className="flex items-baseline justify-between">
      <div className="text-sm font-semibold text-slate-800 dark:text-slate-100">{icon} {title}</div>
      {note && <div className="text-[11px] uppercase tracking-wide text-slate-400">{note}</div>}
    </div>
  )
}

function Empty({ text }: { text: string }) {
  return <div className="mt-4 text-sm text-slate-400 text-center py-6">{text}</div>
}

function Kpi({ label, value, sub, accent, gradient, small }: {
  label: string; value: string; sub?: string; accent?: string; gradient?: string; small?: boolean
}) {
  return (
    <Bento className="flex flex-col justify-between">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">{label}</div>
      <div className={(small ? 'text-lg leading-tight' : 'text-[26px]') + ' font-bold mt-1 truncate '
             + (!gradient && !accent ? 'text-slate-800 dark:text-slate-100' : '')}
           style={gradient
             ? { background: gradient, WebkitBackgroundClip: 'text', backgroundClip: 'text', color: 'transparent' }
             : accent ? { color: accent } : undefined}
           title={value}>
        {value}
      </div>
      {sub && <div className="text-xs mt-1 font-medium" style={{ color: accent ?? C.purple }}>{sub}</div>}
    </Bento>
  )
}

function GradientBarRow({ rank, label, value, sub, pct, gradient, to }: {
  rank?: number; label: string; value: string; sub?: string; pct: number; gradient: string; to?: string
}) {
  const name = to
    ? <Link to={to} className="hover:underline">{label}</Link>
    : <span>{label}</span>
  return (
    <div className="text-sm">
      <div className="flex items-baseline gap-2">
        {rank != null && <span className="text-slate-300 tabular-nums w-4 text-right">{rank}</span>}
        <span className="flex-1 truncate text-slate-700 dark:text-slate-200 font-medium">{name}</span>
        <span className="tabular-nums text-slate-600 dark:text-slate-300">{value}</span>
      </div>
      <div className="mt-1 h-2.5 rounded-full bg-slate-100 dark:bg-slate-700 overflow-hidden">
        <div className="h-full rounded-full" style={{ width: `${Math.max(pct, 2)}%`, background: gradient }} />
      </div>
      {sub && <div className="text-[11px] text-slate-400 mt-0.5">{sub}</div>}
    </div>
  )
}

function StatusBadge({ text, color }: { text: string; color: string }) {
  return (
    <span className="inline-block mr-1 mb-1 text-[11px] font-semibold rounded-full px-2 py-0.5"
          style={{ color, background: color + '1a' }}>
      {text}
    </span>
  )
}

// Worst-first status badges from live signals (cameras, staff, RAG).
function badgesFor(s: {
  cameras_online: number; cameras_total: number
  kpis: { staff_present_avg: number | null; unique_visitors_today: number }
  rag_status: RAG
}): { text: string; color: string }[] {
  const out: { text: string; color: string }[] = []
  if (s.cameras_total > 0 && s.cameras_online === 0) out.push({ text: 'Cams down', color: C.red })
  const staff = s.kpis.staff_present_avg
  if (staff != null) {
    if (staff < 0.3) out.push({ text: 'Critical staffing', color: C.red })
    else if (staff < 0.6) out.push({ text: 'Understaffed', color: C.orange })
  }
  if (s.rag_status === 'red' && !out.length) out.push({ text: 'Poor health', color: C.red })
  if (s.cameras_total === 0 && !s.kpis.unique_visitors_today) out.push({ text: 'No data', color: C.slate })
  if (!out.length) out.push({ text: 'Needs attention', color: C.orange })
  return out
}

function RagDot({ status }: { status: RAG }) {
  const color = status === 'red' ? C.red : status === 'amber' ? C.orange : C.teal
  return <span className="inline-block w-2.5 h-2.5 rounded-full" style={{ background: color }} title={status} />
}

function Th({ id, label, cur, set }: { id: string; label: string; cur: string; set: (s: string) => void }) {
  return (
    <th className={'py-2 pr-3 cursor-pointer select-none ' + (cur === id ? 'text-purple-600' : 'hover:text-slate-600')}
        onClick={() => set(id)}>{label}</th>
  )
}
function pluck(obj: any, path: string): any { return path.split('.').reduce((o, k) => (o ?? {})[k], obj) }
function fmt(v: number | null | undefined, digits = 1, suffix = ''): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return v.toFixed(digits) + suffix
}
function fmtInt(v: number): string { return (v ?? 0).toLocaleString() }

// ══ Top Issues (gradient bars) ═══════════════════════════════════════════
interface ChainIssue {
  detection_type: string; label: string; count: number
  severity: 'critical' | 'high' | 'medium'; affected_text: string
  late_stores?: { store_id: number; store_name: string | null; minutes_late: number }[]
}
const SEV_GRAD: Record<ChainIssue['severity'], string> = {
  critical: 'linear-gradient(135deg,#ef4444,#f97316)',
  high:     'linear-gradient(135deg,#f97316,#f59e0b)',
  medium:   'linear-gradient(135deg,#14b8a6,#3b82f6)',
}

function TopIssuesBento() {
  const [data, setData] = useState<ChainIssue[] | null>(null)
  useEffect(() => {
    let alive = true
    const tick = () => api<{ top_issues: ChainIssue[] }>('/analytics/chain/top-issues')
      .then(d => { if (alive) setData(d.top_issues ?? []) }).catch(() => { if (alive) setData([]) })
    tick(); const t = setInterval(tick, 60_000)
    return () => { alive = false; clearInterval(t) }
  }, [])
  const max = Math.max(1, ...(data ?? []).map(i => i.count))
  return (
    <Bento>
      <SectionTitle icon="🔥" title="Top Issues Across Network" />
      {data == null ? <Empty text="Loading issues…" />
        : data.length === 0 ? <Empty text="✅ No actionable issues across the chain today." />
        : (
          <div className="mt-3 space-y-2.5">
            {data.map((it, i) => (
              <div key={it.detection_type}>
                <GradientBarRow rank={i + 1} label={it.label}
                  value={`${it.count}`} pct={(it.count / max) * 100} gradient={SEV_GRAD[it.severity]}
                  sub={it.detection_type === 'shop_open_close' && it.late_stores?.length
                    ? it.late_stores.map(s => `${s.store_name ?? `Store ${s.store_id}`} (${s.minutes_late}m)`).join(', ')
                    : `Most affected: ${it.affected_text}`} />
              </div>
            ))}
          </div>
        )}
    </Bento>
  )
}

// ══ Store Health donut ═══════════════════════════════════════════════════
interface LeaderboardEntry {
  store_id: number; store_name: string; country: string; score: number; delta: number | null
}
function bandOf(score: number): Band {
  if (score >= 80) return 'great'; if (score >= 60) return 'good'
  if (score >= 40) return 'needs_work'; return 'poor'
}

function HealthDonutBento() {
  const [data, setData] = useState<LeaderboardEntry[] | null>(null)
  useEffect(() => {
    api<{ leaderboard: LeaderboardEntry[] }>('/analytics/chain/health-leaderboard')
      .then(d => setData(d.leaderboard ?? [])).catch(() => setData([]))
  }, [])
  const counts = useMemo(() => {
    const c: Record<Band, number> = { great: 0, good: 0, needs_work: 0, poor: 0 }
    for (const r of (data ?? [])) c[bandOf(Math.round(r.score))]++
    return c
  }, [data])
  const total = (data ?? []).length
  const best = (data ?? [])[0] || null
  const worst = (data ?? [])[(data ?? []).length - 1] || null

  // Donut geometry.
  const R = 54, SW = 18, C0 = 2 * Math.PI * R
  let offset = 0
  const segs = (Object.keys(BAND) as Band[]).flatMap(b => {
    const n = counts[b]; if (!n || !total) return []
    const frac = n / total; const len = frac * C0
    const seg = { b, dash: `${len} ${C0 - len}`, off: -offset }
    offset += len; return [seg]
  })

  return (
    <Bento>
      <SectionTitle icon="💜" title="Store Health Distribution" note={total ? `${total} stores` : ''} />
      {data == null ? <Empty text="Loading scores…" />
        : total === 0 ? <Empty text="No store health scores available yet." />
        : (
          <div className="mt-3 flex items-center gap-5">
            <svg width="150" height="150" viewBox="0 0 150 150" className="shrink-0">
              <circle cx="75" cy="75" r={R} fill="none" stroke="#eef0f4" strokeWidth={SW} />
              {segs.map(s => (
                <circle key={s.b} cx="75" cy="75" r={R} fill="none"
                  stroke={BAND[s.b].color} strokeWidth={SW}
                  strokeDasharray={s.dash} strokeDashoffset={s.off}
                  transform="rotate(-90 75 75)" strokeLinecap="butt" />
              ))}
              <text x="75" y="70" textAnchor="middle" fontSize="26" fontWeight="700" fill="#1e293b">{total}</text>
              <text x="75" y="90" textAnchor="middle" fontSize="11" fill="#94a3b8">stores</text>
            </svg>
            <div className="flex-1 space-y-1.5">
              {(Object.keys(BAND) as Band[]).map(b => (
                <div key={b} className="flex items-center gap-2 text-sm">
                  <span className="inline-block w-3 h-3 rounded" style={{ background: BAND[b].color }} />
                  <span className="flex-1 text-slate-600 dark:text-slate-300">{BAND[b].label}</span>
                  <span className="tabular-nums font-semibold text-slate-700 dark:text-slate-200">{counts[b]}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      {total > 0 && (
        <div className="mt-4 flex flex-wrap gap-2 text-sm">
          {best && (
            <span className="rounded-xl px-3 py-1.5" style={{ background: C.teal + '1a', color: '#0f766e' }}>
              🏆 Best: <strong>{best.store_name}</strong> ({Math.round(best.score)})
            </span>
          )}
          {worst && worst.store_id !== best?.store_id && (
            <span className="rounded-xl px-3 py-1.5" style={{ background: C.red + '1a', color: '#b91c1c' }}>
              ⚠️ Needs help: <strong>{worst.store_name}</strong> ({Math.round(worst.score)})
            </span>
          )}
        </div>
      )}
    </Bento>
  )
}

// ══ Opening status (preserved) ═══════════════════════════════════════════
interface OpeningRow {
  store_id: number; store_name: string
  status: 'on_time' | 'late' | 'not_opened' | 'pending' | 'no_data'
  status_label: string; opened_at_eat: string | null; minutes_late: number | null
}
const OPEN_EMOJI: Record<OpeningRow['status'], string> = {
  on_time: '✅', late: '⚠️', not_opened: '🔴', pending: '🕐', no_data: '⚫',
}
const OPEN_TONE: Record<OpeningRow['status'], string> = {
  on_time: 'text-teal-700', late: 'text-orange-700',
  not_opened: 'text-red-700', pending: 'text-slate-500 dark:text-slate-300', no_data: 'text-slate-400',
}
function OpeningStatusBento() {
  const [data, setData] = useState<OpeningRow[] | null>(null)
  useEffect(() => {
    let alive = true
    const tick = () => api<{ stores: OpeningRow[] }>('/analytics/chain/opening-status')
      .then(d => { if (alive) setData(d.stores ?? []) }).catch(() => { if (alive) setData([]) })
    tick(); const t = setInterval(tick, 60_000)
    return () => { alive = false; clearInterval(t) }
  }, [])
  return (
    <Bento>
      <SectionTitle icon="🕐" title="Store Opening Status — Today" />
      {data == null ? <Empty text="Loading…" />
        : data.length === 0 ? <Empty text="No active stores configured." />
        : (
          <ul className="mt-3 text-sm divide-y divide-slate-100 dark:divide-slate-700">
            {data.map(r => (
              <li key={r.store_id} className="flex items-baseline gap-3 py-1.5">
                <span>{OPEN_EMOJI[r.status]}</span>
                <span className="flex-1 text-slate-700 dark:text-slate-200">{r.store_name}</span>
                <span className={'text-xs ' + OPEN_TONE[r.status]}>{r.status_label}</span>
              </li>
            ))}
          </ul>
        )}
    </Bento>
  )
}

// ══ Hourly footfall (preserved, restyled palette) ════════════════════════
interface HourlyComparisonPayload {
  hours: string[]
  stores: { store_id: number; store_name: string; series: number[]; today_total: number }[]
}
function HourlyComparisonBento() {
  const [data, setData] = useState<HourlyComparisonPayload | null>(null)
  useEffect(() => {
    const load = () => fetch('/api/analytics/chain/hourly-comparison', {
      headers: { Authorization: `Bearer ${localStorage.getItem('vg_access_token') ?? ''}` },
    }).then(r => r.ok ? r.json() : null).then(d => { if (d) setData(d) }).catch(() => {})
    load(); const t = setInterval(load, 60_000)
    return () => clearInterval(t)
  }, [])
  if (!data) return <Bento><SectionTitle icon="📈" title="Hourly Footfall — Today" /><Empty text="Loading…" /></Bento>
  if (!data.stores.length) return <Bento><SectionTitle icon="📈" title="Hourly Footfall — Today" /><Empty text="No footfall recorded today yet." /></Bento>

  const topStores = data.stores.slice(0, 8)
  const W = 800, H = 220, PL = 36, PR = 12, PT = 10, PB = 28
  const innerW = W - PL - PR, innerH = H - PT - PB
  const maxV = Math.max(10, ...topStores.flatMap(s => s.series))
  const stepX = innerW / (data.hours.length - 1)
  const xAt = (i: number) => PL + i * stepX
  const yAt = (v: number) => PT + innerH - (v / maxV) * innerH
  const palette = ['#7c3aed', '#ec4899', '#3b82f6', '#14b8a6', '#f97316', '#ef4444', '#8b5cf6', '#0891b2']
  const yTicks = [0, Math.ceil(maxV / 2), maxV]
  return (
    <Bento>
      <SectionTitle icon="📈" title="Hourly Footfall — Today" />
      <svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`} style={{ display: 'block' }} className="mt-2">
        {yTicks.map((tv, i) => (
          <g key={i}>
            <line x1={PL} x2={W - PR} y1={yAt(tv)} y2={yAt(tv)} stroke="#eef0f4" strokeDasharray="2 3" />
            <text x={PL - 6} y={yAt(tv) + 4} textAnchor="end" fontSize="11" fill="#94a3b8">{tv}</text>
          </g>
        ))}
        {topStores.map((s, idx) => (
          <polyline key={s.store_id}
            points={s.series.map((v, i) => `${xAt(i).toFixed(1)},${yAt(v).toFixed(1)}`).join(' ')}
            fill="none" stroke={palette[idx % palette.length]} strokeWidth={2.5}
            strokeLinejoin="round" strokeLinecap="round" />
        ))}
        {data.hours.map((label, i) => (
          <text key={i} x={xAt(i)} y={H - 8} textAnchor="middle" fontSize="11" fill="#94a3b8">{label}</text>
        ))}
      </svg>
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs">
        {topStores.map((s, idx) => (
          <span key={s.store_id} className="inline-flex items-center gap-1.5">
            <span className="inline-block w-3 h-0.5" style={{ background: palette[idx % palette.length] }} />
            <span className="text-slate-600 dark:text-slate-300">{s.store_name}</span>
            <span className="text-slate-400">({s.today_total})</span>
          </span>
        ))}
      </div>
    </Bento>
  )
}
