// Chain-wide head-office page: side-by-side KPIs for every store, plus
// totals. Sortable by clicking any column header.

import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge, Card, PageHeader, Select } from '@/components/ui/Primitives'
import { analytics } from '@/api/stores'

const WINDOWS = [1, 7, 30]

export default function MultiStorePage() {
  const [days, setDays] = useState(7)
  const [data, setData] = useState<Awaited<ReturnType<typeof analytics.multiDashboard>> | null>(null)
  const [sortBy, setSortBy] = useState<string>('store_name')

  useEffect(() => {
    analytics.multiDashboard(days).then(setData).catch(console.error)
    const t = setInterval(() => analytics.multiDashboard(days).then(setData), 60_000)
    return () => clearInterval(t)
  }, [days])

  const rows = useMemo(() => {
    if (!data) return []
    const arr = [...data.stores]
    arr.sort((a, b) => {
      const av = pluck(a, sortBy), bv = pluck(b, sortBy)
      if (typeof av === 'number' && typeof bv === 'number') return bv - av
      return String(av ?? '').localeCompare(String(bv ?? ''))
    })
    return arr
  }, [data, sortBy])

  if (!data) return <div className="p-6 text-slate-500">Loading…</div>

  return (
    <div className="p-6">
      <PageHeader
        title="Chain dashboard"
        actions={
          <Select value={days} onChange={e => setDays(Number(e.target.value))}>
            {WINDOWS.map(d => <option key={d} value={d}>last {d}d</option>)}
          </Select>
        }
      />

      <div className="grid grid-cols-3 gap-3 mb-4">
        <Tile label="Stores"             value={String(data.totals.stores)} />
        <Tile label="Unique visitors today" value={String(data.totals.unique_visitors_today)} />
        <Tile label="Alerts (window)"    value={String(data.totals.alerts_total)} />
      </div>

      <Card className="overflow-auto">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600">
            <tr>
              <Th id="store_name"          label="Store"            cur={sortBy} set={setSortBy} />
              <Th id="country"             label="Country"          cur={sortBy} set={setSortBy} />
              <Th id="kpis.unique_visitors_today" label="Visitors today" cur={sortBy} set={setSortBy} />
              <Th id="kpis.occupancy_avg"  label="Occupancy avg"    cur={sortBy} set={setSortBy} />
              <Th id="kpis.queue_length_avg" label="Queue avg"      cur={sortBy} set={setSortBy} />
              <Th id="kpis.staff_present_avg" label="Staff %"       cur={sortBy} set={setSortBy} />
              <Th id="kpis.stop_rate_avg"  label="Stop rate"        cur={sortBy} set={setSortBy} />
              <th className="text-left p-3">Alerts</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.store_id} className="border-t hover:bg-slate-50">
                <td className="p-3 font-medium">
                  <Link to={`/stores/${r.store_id}`} className="text-sky-600 hover:underline">{r.store_name}</Link>
                </td>
                <td className="p-3">{r.country}</td>
                <td className="p-3">{r.kpis.unique_visitors_today}</td>
                <td className="p-3">{fmt(r.kpis.occupancy_avg, 0)}</td>
                <td className="p-3">{fmt(r.kpis.queue_length_avg, 1)}</td>
                <td className="p-3">{fmt(r.kpis.staff_present_avg && r.kpis.staff_present_avg * 100, 0, '%')}</td>
                <td className="p-3">{fmt(r.kpis.stop_rate_avg && r.kpis.stop_rate_avg * 100, 0, '%')}</td>
                <td className="p-3">
                  {Object.entries(r.alerts_breakdown).slice(0, 3).map(([t, n]) =>
                    <Badge key={t} color="sky">{t}:{n}</Badge>)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  )
}

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <Card className="p-4">
      <div className="text-xs text-slate-500">{label}</div>
      <div className="text-3xl font-semibold mt-1">{value}</div>
    </Card>
  )
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
