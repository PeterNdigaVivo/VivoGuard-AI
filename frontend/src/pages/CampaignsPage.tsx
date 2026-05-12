// Campaigns: create date-window markers, view before/during/after lift
// for any metric_type, and download chain-wide reports as PDF or CSV.

import { useEffect, useState } from 'react'
import { Badge, Button, Card, Input, PageHeader, Select } from '@/components/ui/Primitives'
import { api } from '@/api/client'
import { stores as storesApi, type Store } from '@/api/stores'

interface Campaign {
  id: number; name: string; store_id: number | null
  start_date: string; end_date: string; description: string | null
}
interface LiftReport {
  campaign_id: number; name: string; store_id: number | null
  metric_type: string; span_days: number
  before: number | null; during: number | null; after: number | null
  lift_during_vs_before_pct: number | null
  lift_after_vs_before_pct: number | null
}

const METRICS = [
  'passersby', 'stop_rate', 'occupancy', 'queue_length',
  'unique_visitors', 'dwell_seconds',
]

export default function CampaignsPage() {
  const [list, setList] = useState<Campaign[]>([])
  const [stores, setStores] = useState<Store[]>([])
  const [form, setForm] = useState({ name: '', store_id: '', start_date: '', end_date: '', description: '' })
  const [lift, setLift] = useState<LiftReport | null>(null)
  const [metric, setMetric] = useState('passersby')
  const [since, setSince] = useState('')
  const [until, setUntil] = useState('')

  const reload = () => api<Campaign[]>('/stores/campaigns/all').then(setList).catch(console.error)
  useEffect(() => { reload(); storesApi.list().then(setStores) }, [])

  async function create() {
    if (!form.name || !form.start_date || !form.end_date) return
    await api('/stores/campaigns', {
      method: 'POST',
      body: {
        name: form.name,
        store_id: form.store_id ? Number(form.store_id) : null,
        start_date: form.start_date, end_date: form.end_date,
        description: form.description || null,
      },
    })
    setForm({ name: '', store_id: '', start_date: '', end_date: '', description: '' })
    reload()
  }

  async function viewLift(c: Campaign) {
    const r = await api<LiftReport>(`/analytics/campaigns/${c.id}/lift?metric_type=${metric}`)
    setLift(r)
  }

  function reportUrl(kind: 'pdf' | 'csv', storeId?: number): string {
    const params = new URLSearchParams({ since, until })
    if (storeId) params.set('store_id', String(storeId))
    return `/api/analytics/report.${kind}?${params}`
  }

  return (
    <div className="p-6">
      <PageHeader title="Campaigns & Reports" />

      {/* --- New campaign --- */}
      <Card className="p-4 mb-6">
        <div className="font-medium mb-3">New campaign window</div>
        <div className="grid grid-cols-1 md:grid-cols-6 gap-2">
          <Input placeholder="Name" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} />
          <Select value={form.store_id} onChange={e => setForm({ ...form, store_id: e.target.value })}>
            <option value="">all stores</option>
            {stores.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
          </Select>
          <Input type="date" value={form.start_date} onChange={e => setForm({ ...form, start_date: e.target.value })} />
          <Input type="date" value={form.end_date}   onChange={e => setForm({ ...form, end_date: e.target.value })} />
          <Input placeholder="Description" value={form.description}
                 onChange={e => setForm({ ...form, description: e.target.value })} />
          <Button onClick={create}>Add</Button>
        </div>
      </Card>

      {/* --- Lift viewer --- */}
      <Card className="p-4 mb-6">
        <div className="font-medium mb-3">Campaign lift</div>
        <div className="flex items-center gap-2 mb-3">
          <span className="text-sm text-slate-600">Metric:</span>
          <Select value={metric} onChange={e => setMetric(e.target.value)}>
            {METRICS.map(m => <option key={m} value={m}>{m}</option>)}
          </Select>
        </div>
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600">
            <tr>
              <th className="text-left p-2">Campaign</th>
              <th className="text-left p-2">Store</th>
              <th className="text-left p-2">Window</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {list.map(c => (
              <tr key={c.id} className="border-t hover:bg-slate-50">
                <td className="p-2 font-medium">{c.name}</td>
                <td className="p-2">{c.store_id ?? 'all stores'}</td>
                <td className="p-2 text-xs">{c.start_date} → {c.end_date}</td>
                <td className="p-2 text-right">
                  <Button onClick={() => viewLift(c)}>Compute lift</Button>
                </td>
              </tr>
            ))}
            {!list.length && (
              <tr><td colSpan={4} className="p-6 text-center text-slate-500">No campaigns yet.</td></tr>
            )}
          </tbody>
        </table>

        {lift && (
          <div className="mt-4 grid grid-cols-2 md:grid-cols-5 gap-3">
            <Tile label="Metric" value={lift.metric_type} />
            <Tile label="Before (avg)" value={fmt(lift.before)} />
            <Tile label="During (avg)" value={fmt(lift.during)} />
            <Tile label="After (avg)"  value={fmt(lift.after)} />
            <Tile label="Lift during vs before"
                  value={lift.lift_during_vs_before_pct === null ? '—' : `${lift.lift_during_vs_before_pct.toFixed(1)}%`}
                  emphasis={(lift.lift_during_vs_before_pct ?? 0) >= 0 ? 'green' : 'red'} />
          </div>
        )}
      </Card>

      {/* --- Reports --- */}
      <Card className="p-4">
        <div className="font-medium mb-3">Chain report — PDF / CSV</div>
        <div className="flex flex-wrap gap-3 items-end">
          <label>
            <div className="text-xs text-slate-500 mb-1">Since</div>
            <Input type="datetime-local" value={since} onChange={e => setSince(e.target.value)} />
          </label>
          <label>
            <div className="text-xs text-slate-500 mb-1">Until</div>
            <Input type="datetime-local" value={until} onChange={e => setUntil(e.target.value)} />
          </label>
          <a href={reportUrl('pdf')} target="_blank" rel="noreferrer">
            <Button disabled={!since || !until}>Download PDF</Button>
          </a>
          <a href={reportUrl('csv')} target="_blank" rel="noreferrer">
            <Button variant="ghost" disabled={!since || !until}>Download CSV</Button>
          </a>
        </div>
        <div className="text-xs text-slate-500 mt-2">
          PDF includes a chain summary table plus one detail page per store.
          CSV has one row per store with all KPIs.
        </div>
      </Card>
    </div>
  )
}

function Tile({ label, value, emphasis }: { label: string; value: string; emphasis?: 'green' | 'red' }) {
  return (
    <Card className="p-3">
      <div className="text-xs text-slate-500">{label}</div>
      <div className={'text-xl font-semibold mt-1 ' + (emphasis === 'green' ? 'text-emerald-600' : emphasis === 'red' ? 'text-red-600' : '')}>
        {value}
      </div>
    </Card>
  )
}

function fmt(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return v.toFixed(2)
}
