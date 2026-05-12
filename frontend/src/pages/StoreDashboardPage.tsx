// Per-store dashboard: KPIs at a glance + alert-type breakdown.

import { useEffect, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { Badge, Card, PageHeader, Select } from '@/components/ui/Primitives'
import { analytics, type StoreDashboard } from '@/api/stores'

const WINDOWS = [1, 7, 30]

export default function StoreDashboardPage() {
  const { id } = useParams()
  const storeId = Number(id)
  const [days, setDays] = useState(7)
  const [data, setData] = useState<StoreDashboard | null>(null)

  useEffect(() => {
    if (!storeId) return
    analytics.storeDashboard(storeId, days).then(setData).catch(console.error)
    const t = setInterval(() => analytics.storeDashboard(storeId, days).then(setData), 30000)
    return () => clearInterval(t)
  }, [storeId, days])

  if (!data) return <div className="p-6 text-slate-500">Loading…</div>

  const k = data.kpis
  return (
    <div className="p-6">
      <PageHeader
        title={`${data.store_name}`}
        actions={
          <Select value={days} onChange={e => setDays(Number(e.target.value))}>
            {WINDOWS.map(d => <option key={d} value={d}>last {d}d</option>)}
          </Select>
        }
      />
      <div className="text-slate-500 mb-4">
        <Link to="/stores" className="text-sky-600 hover:underline">← All stores</Link>
        <span className="ml-3">{data.country} · updated {new Date(data.as_of).toLocaleTimeString()}</span>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        <Kpi label="Occupancy now"      value={fmt(k.occupancy_now, 0)} />
        <Kpi label="Queue length (avg)" value={fmt(k.queue_length_avg, 1)} />
        <Kpi label="Avg wait (sec)"     value={fmt(k.queue_wait_avg_sec, 0)} />
        <Kpi label="Staff present %"    value={fmt(k.staff_present_avg && k.staff_present_avg * 100, 0, '%')} />
        <Kpi label="Unique visitors today" value={String(k.unique_visitors_today)} />
        <Kpi label="Passersby (avg)"    value={fmt(k.passersby_avg, 0)} />
        <Kpi label="Stop rate"          value={fmt(k.stop_rate_avg && k.stop_rate_avg * 100, 0, '%')} />
        <Kpi label="Avg dwell (sec)"    value={fmt(k.dwell_seconds_avg, 0)} />
      </div>

      <Card className="p-4">
        <div className="text-sm font-medium mb-3">Alerts in window — by type</div>
        {Object.keys(data.alerts_breakdown).length === 0 && (
          <div className="text-slate-400 text-sm">No alerts in this window.</div>
        )}
        <div className="flex flex-wrap gap-2">
          {Object.entries(data.alerts_breakdown).map(([t, n]) => (
            <Badge key={t} color="sky">{t}: {n}</Badge>
          ))}
        </div>
      </Card>
    </div>
  )
}

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <Card className="p-4">
      <div className="text-xs text-slate-500">{label}</div>
      <div className="text-2xl font-semibold mt-1">{value}</div>
    </Card>
  )
}

function fmt(v: number | null | undefined, digits = 1, suffix = ''): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return v.toFixed(digits) + suffix
}
