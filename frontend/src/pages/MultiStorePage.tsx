// Chain dashboard — executive view for Vivo head office.
//
// Layout:
//   1. Hero strip: total chain visitors today, best store, alerts.
//   2. NEEDS ATTENTION: top section listing red/amber stores with
//      one-line reasons. Hidden when everyone is green.
//   3. Cross-store comparison bar chart (visitors today, each store).
//   4. Full sortable RAG table.
//
// Auto-refreshes every 60s (chain data is less time-sensitive than
// the per-store dashboard).

import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Card, PageHeader, Select } from '@/components/ui/Primitives'
import { analytics } from '@/api/stores'
import { labelForDetector } from '@/lib/detectorLabels'

const WINDOWS = [1, 7, 30]
type RAG = 'red' | 'amber' | 'green'

export default function MultiStorePage() {
  const [days, setDays] = useState(7)
  const [data, setData] = useState<Awaited<ReturnType<typeof analytics.multiDashboard>> | null>(null)
  const [sortBy, setSortBy] = useState<string>('rag_status')

  // Incremental load: paint the store list (cheap) from /api/stores
  // FIRST so the page is interactive in <200ms even when the heavy
  // chain aggregation endpoint takes a second or two. The full
  // multiDashboard payload then layers the KPIs on top.
  const [bootstrapStores, setBootstrapStores] = useState<{ id: number; name: string; country: string }[]>([])
  useEffect(() => {
    fetch('/api/stores', {
      headers: { Authorization: `Bearer ${localStorage.getItem('vg_access_token') ?? ''}` },
    }).then(r => r.ok ? r.json() : [])
      .then(rows => setBootstrapStores(rows || []))
      .catch(() => {})
  }, [])

  useEffect(() => {
    // In-flight dedup — if a previous request is still pending when
    // the 60s tick fires, skip the new one.
    let inFlight = false
    const fetchOnce = () => {
      if (inFlight) return
      inFlight = true
      analytics.multiDashboard(days)
        .then(setData).catch(console.error)
        .finally(() => { inFlight = false })
    }
    fetchOnce()
    const t = setInterval(fetchOnce, 60_000)
    return () => clearInterval(t)
  }, [days])

  // Sort: by RAG status (red→amber→green), then by the chosen column.
  // Operators want troubled stores at the top by default.
  const rows = useMemo(() => {
    if (!data) return []
    const ragWeight: Record<RAG, number> = { red: 0, amber: 1, green: 2 }
    const arr = [...data.stores]
    arr.sort((a, b) => {
      // Primary: chosen column.
      if (sortBy !== 'rag_status') {
        const av = pluck(a, sortBy), bv = pluck(b, sortBy)
        if (typeof av === 'number' && typeof bv === 'number') return bv - av
        const cmp = String(av ?? '').localeCompare(String(bv ?? ''))
        if (cmp !== 0) return cmp
      }
      // Secondary (or primary when sortBy is 'rag_status'): RAG bucket.
      return ragWeight[a.rag_status] - ragWeight[b.rag_status]
    })
    return arr
  }, [data, sortBy])

  // While the heavy aggregation is loading, show the store list with
  // placeholder rows so the page doesn't read "Loading…" — operators
  // see "yes, the system is up, here are your stores" immediately.
  if (!data) {
    return (
      <div className="p-6">
        <PageHeader title="Chain dashboard"
                    actions={<span className="text-xs text-slate-500">loading metrics…</span>} />
        <Card className="overflow-auto">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-slate-600">
              <tr>
                <th className="text-left p-3">Store</th>
                <th className="text-left p-3">Country</th>
                <th className="text-left p-3">Status</th>
              </tr>
            </thead>
            <tbody>
              {bootstrapStores.map(s => (
                <tr key={s.id} className="border-t">
                  <td className="p-3 font-medium">{s.name}</td>
                  <td className="p-3">{s.country}</td>
                  <td className="p-3 text-slate-400">loading…</td>
                </tr>
              ))}
              {bootstrapStores.length === 0 && (
                <tr><td colSpan={3} className="p-8 text-center text-slate-400">Loading…</td></tr>
              )}
            </tbody>
          </table>
        </Card>
      </div>
    )
  }

  const maxVisitors = Math.max(
    ...data.stores.map(s => s.kpis.unique_visitors_today || 0), 1,
  )

  return (
    <div className="p-6 space-y-4">
      <PageHeader
        title="Chain dashboard"
        actions={
          <div className="flex items-center gap-3">
            <Select value={days} onChange={e => setDays(Number(e.target.value))}>
              {WINDOWS.map(d => <option key={d} value={d}>last {d}d</option>)}
            </Select>
            <span className="text-xs text-slate-500">
              {data.totals.stores_open} of {data.totals.stores} stores open · updated {new Date(data.as_of).toLocaleTimeString()}
            </span>
          </div>
        }
      />

      {/* Hero strip */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Tile label="Chain visitors today" value={String(data.totals.unique_visitors_today)} big />
        <Tile label="Stores needing attention" value={String(data.totals.stores_attention)}
              tone={data.totals.stores_attention > 0 ? 'amber' : 'green'} />
        <Tile label="Critical alerts (1h)" value={String(data.totals.alerts_critical)}
              tone={data.totals.alerts_critical > 0 ? 'red' : 'green'} />
        <Tile label="Best store today"
              value={data.best_store_today ? data.best_store_today.store_name : '—'}
              sub={data.best_store_today
                ? `${data.best_store_today.visitors} visitors`
                : 'no visitor data yet'} />
      </div>

      {/* Needs attention */}
      {data.needs_attention.length > 0 && (
        <Card className="p-4 border-amber-200 bg-amber-50">
          <div className="text-sm font-semibold text-amber-800 mb-2 flex items-center gap-2">
            🚨 Needs attention ({data.needs_attention.length})
          </div>
          <div className="space-y-1.5">
            {data.needs_attention.map(s => (
              <Link key={s.store_id} to={`/stores/${s.store_id}`}
                    className="flex flex-wrap items-center gap-2 text-sm hover:bg-amber-100 px-2 py-1 rounded">
                <RagDot status={s.rag_status} />
                <span className="font-medium w-44 truncate">{s.store_name}</span>
                <span className="text-xs text-slate-500 w-20">{s.country}</span>
                <span className="text-xs text-slate-500">
                  {s.cameras_online}/{s.cameras_total} cams live
                </span>
                <span className="text-xs text-slate-700">— {s.rag_reasons.join('; ')}</span>
              </Link>
            ))}
          </div>
        </Card>
      )}

      {/* Cross-store comparison bar chart */}
      <Card className="p-4">
        <div className="text-sm font-medium mb-3">Visitors today by store</div>
        <div className="space-y-1">
          {rows.map(r => {
            const v = r.kpis.unique_visitors_today || 0
            const width = (v / maxVisitors) * 100
            return (
              <div key={r.store_id} className="flex items-center gap-2 text-sm">
                <RagDot status={r.rag_status} />
                <Link to={`/stores/${r.store_id}`}
                      className="w-40 truncate text-sky-700 hover:underline">
                  {r.store_name}
                </Link>
                <div className="flex-1 bg-slate-100 rounded h-5 relative">
                  <div className={'absolute inset-y-0 left-0 rounded ' +
                                  (r.rag_status === 'red' ? 'bg-red-500' :
                                   r.rag_status === 'amber' ? 'bg-amber-500' :
                                   'bg-emerald-500')}
                       style={{ width: `${Math.max(width, 1)}%` }} />
                </div>
                <div className="w-12 text-right tabular-nums">{v}</div>
              </div>
            )
          })}
        </div>
      </Card>

      {/* Sortable table */}
      <Card className="overflow-auto">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600">
            <tr>
              <Th id="rag_status"           label="Status"           cur={sortBy} set={setSortBy} />
              <Th id="store_name"           label="Store"            cur={sortBy} set={setSortBy} />
              <Th id="country"              label="Country"          cur={sortBy} set={setSortBy} />
              <Th id="cameras_online"       label="Cameras live"     cur={sortBy} set={setSortBy} />
              <Th id="kpis.unique_visitors_today" label="Visitors today" cur={sortBy} set={setSortBy} />
              <Th id="kpis.occupancy_avg"   label="Occupancy avg"    cur={sortBy} set={setSortBy} />
              <Th id="kpis.staff_present_avg" label="Staff %"        cur={sortBy} set={setSortBy} />
              <Th id="recent_critical_alerts" label="Critical (1h)"  cur={sortBy} set={setSortBy} />
              <th className="text-left p-3">Top alerts</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.store_id} className="border-t hover:bg-slate-50">
                <td className="p-3"><RagDot status={r.rag_status} /></td>
                <td className="p-3 font-medium">
                  <Link to={`/stores/${r.store_id}`} className="text-sky-600 hover:underline">{r.store_name}</Link>
                  {!r.is_open && <span className="ml-2 text-[10px] text-slate-400">closed</span>}
                </td>
                <td className="p-3">{r.country}</td>
                <td className={'p-3 tabular-nums ' +
                  (r.cameras_online === 0 && r.cameras_total > 0 ? 'text-red-600 font-semibold' : '')}>
                  {r.cameras_online}/{r.cameras_total}
                </td>
                <td className="p-3 tabular-nums">{r.kpis.unique_visitors_today}</td>
                <td className="p-3 tabular-nums">{fmt(r.kpis.occupancy_avg, 0)}</td>
                <td className="p-3 tabular-nums">{fmt(r.kpis.staff_present_avg && r.kpis.staff_present_avg * 100, 0, '%')}</td>
                <td className={'p-3 tabular-nums ' +
                  (r.recent_critical_alerts > 0 ? 'text-red-600 font-semibold' : '')}>
                  {r.recent_critical_alerts}
                </td>
                <td className="p-3 space-x-1">
                  {Object.entries(r.alerts_breakdown).slice(0, 3).map(([t, n]) => (
                    <span key={t} className="inline-block text-[11px] bg-slate-100 text-slate-700 rounded px-1.5 py-0.5">
                      {labelForDetector(t)}: {n}
                    </span>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  )
}

function Tile({ label, value, sub, big, tone }: {
  label: string; value: string; sub?: string; big?: boolean
  tone?: 'red' | 'amber' | 'green'
}) {
  const toneCls = tone === 'red'   ? 'text-red-600' :
                  tone === 'amber' ? 'text-amber-600' :
                  tone === 'green' ? 'text-emerald-600' : ''
  return (
    <Card className="p-4">
      <div className="text-xs text-slate-500">{label}</div>
      <div className={(big ? 'text-3xl' : 'text-2xl') + ' font-semibold mt-1 ' + toneCls}>
        {value}
      </div>
      {sub && <div className="text-xs text-slate-500 mt-1">{sub}</div>}
    </Card>
  )
}

function RagDot({ status }: { status: RAG }) {
  const cls = status === 'red'   ? 'bg-red-500' :
              status === 'amber' ? 'bg-amber-500' :
                                   'bg-emerald-500'
  return <span className={'inline-block w-3 h-3 rounded-full ' + cls}
               title={status} />
}

function Th({ id, label, cur, set }: { id: string; label: string; cur: string; set: (s: string) => void }) {
  return (
    <th className={'text-left p-3 cursor-pointer ' + (cur === id ? 'text-sky-700' : '')}
        onClick={() => set(id)}>{label}</th>
  )
}
function pluck(obj: any, path: string): any {
  return path.split('.').reduce((o, k) => (o ?? {})[k], obj)
}
function fmt(v: number | null | undefined, digits = 1, suffix = ''): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return v.toFixed(digits) + suffix
}
