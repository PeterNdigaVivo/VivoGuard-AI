// Alerts page — chain-wide feed using the shared AlertCard.
// May-2026 redesign: same card component as the per-store dashboard
// feed so titles, severity colours, and action buttons match exactly.

import { useEffect, useMemo, useState } from 'react'
import { Card, PageHeader } from '@/components/ui/Primitives'
import DateRangePicker, { rangeFor, type DateRange } from '@/components/DateRangePicker'
import { alerts as alertsApi, type Alert } from '@/api/alerts'
import { AlertCard, groupAlerts } from '@/components/AlertCard'
import { stores as storesApi, type Store } from '@/api/stores'

// Simple quick-filter buttons non-technical staff understand.
type Quick = 'urgent' | 'attention' | 'resolved' | 'all'

export default function AlertsPage() {
  const [items, setItems] = useState<Alert[]>([])
  const [range, setRange] = useState<DateRange>(() => rangeFor('today'))
  const [quick, setQuick] = useState<Quick>('all')
  const [storeId, setStoreId] = useState<string>('')
  const [search, setSearch] = useState('')
  const [stores, setStores] = useState<Store[]>([])
  const [summary, setSummary] = useState({ urgent: 0, attention: 0, resolved_today: 0, unread_urgent: 0 })

  useEffect(() => { storesApi.list().then(setStores).catch(() => {}) }, [])

  const reload = () => {
    alertsApi.list({
      store_id: storeId || undefined,
      since: range.since,
      until: range.until,
      limit: 500,
    }).then(setItems)
    alertsApi.summary(storeId ? Number(storeId) : undefined).then(setSummary).catch(() => {})
  }
  useEffect(() => { reload() }, [storeId, range.since, range.until])  // eslint-disable-line react-hooks/exhaustive-deps

  // Real-time: when /ws/alerts pushes a new event, refetch.
  useEffect(() => alertsApi.subscribe(() => reload()), [])  // eslint-disable-line react-hooks/exhaustive-deps

  // Client-side quick-filter + search over the loaded window.
  const filtered = useMemo(() => {
    let rows = items
    if (quick === 'urgent')     rows = rows.filter(a => a.severity_label === 'URGENT' && a.status === 'new')
    else if (quick === 'attention') rows = rows.filter(a => a.severity_label === 'ATTENTION' && a.status === 'new')
    else if (quick === 'resolved')  rows = rows.filter(a => a.status === 'resolved' || a.status === 'dismissed')
    if (search.trim()) {
      const q = search.toLowerCase()
      rows = rows.filter(a =>
        (a.plain_title ?? a.title ?? '').toLowerCase().includes(q) ||
        (a.camera_name ?? '').toLowerCase().includes(q) ||
        (a.detection_type ?? '').toLowerCase().includes(q))
    }
    return rows
  }, [items, quick, search])

  const groups = groupAlerts(filtered)

  // Excel export — fetch with the bearer header (an <a href> can't
  // carry it) and trigger a download of the returned .xlsx blob.
  async function resolveAll() {
    // Use whatever filter the user can see — store + date window —
    // so the bulk action mirrors the visible list, not the whole DB.
    const newCount = items.filter(a => a.status === 'new').length
    if (newCount === 0) { alert('There are no unresolved alerts to clear.'); return }
    const label = storeId
      ? stores.find(s => String(s.id) === storeId)?.name ?? 'this store'
      : 'all stores'
    if (!confirm(`Mark ${newCount} unresolved alert${newCount === 1 ? '' : 's'} `
                 + `for ${label} (${range.label.toLowerCase()}) as resolved?`)) return
    try {
      const { resolved } = await alertsApi.resolveAll({
        store_id: storeId || undefined,
        since: range.since, until: range.until,
      })
      alert(`Resolved ${resolved} alert${resolved === 1 ? '' : 's'}.`)
      reload()
    } catch (e) {
      alert(`Could not resolve: ${e}`)
    }
  }

  async function exportExcel() {
    const q = new URLSearchParams()
    if (storeId) q.set('store_id', storeId)
    q.set('since', range.since); q.set('until', range.until)
    const tok = localStorage.getItem('vg_access_token') ?? ''
    const res = await fetch(`/api/alerts/export.xlsx?${q}`, {
      headers: { Authorization: `Bearer ${tok}` },
    })
    if (!res.ok) { alert('Export failed — rebuild the api container if this persists.'); return }
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `vivoguard_alerts_${range.key}.xlsx`
    document.body.appendChild(a); a.click(); document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  return (
    <div className="p-6">
      <PageHeader title="Alerts" actions={
        <div className="flex items-center gap-2">
          <DateRangePicker value={range} onChange={setRange} />
          <button onClick={resolveAll}
                  className="px-3 py-1.5 rounded bg-slate-700 text-white text-sm hover:bg-slate-600">
            ✅ Resolve all
          </button>
          <button onClick={exportExcel}
                  className="px-3 py-1.5 rounded bg-emerald-600 text-white text-sm hover:bg-emerald-500">
            📊 Export to Excel
          </button>
        </div>
      } />

      {/* Quick stats */}
      <div className="text-sm text-slate-600 mb-3">
        Today: <strong className="text-red-600">{summary.urgent} urgent</strong>
        {' · '}<strong className="text-amber-600">{summary.attention} need attention</strong>
        {' · '}<strong className="text-emerald-600">{summary.resolved_today} resolved</strong>
      </div>

      {/* Simple filter bar */}
      <Card className="p-3 mb-4 flex flex-wrap gap-2 items-center">
        <QuickBtn active={quick === 'urgent'}    onClick={() => setQuick('urgent')}>🔴 Urgent</QuickBtn>
        <QuickBtn active={quick === 'attention'} onClick={() => setQuick('attention')}>🟡 Needs Attention</QuickBtn>
        <QuickBtn active={quick === 'resolved'}  onClick={() => setQuick('resolved')}>✅ Resolved</QuickBtn>
        <QuickBtn active={quick === 'all'}       onClick={() => setQuick('all')}>📋 All</QuickBtn>

        <select className="border rounded px-2 py-1 text-sm ml-2"
                value={storeId} onChange={e => setStoreId(e.target.value)}>
          <option value="">All Stores</option>
          {stores.map(s => <option key={s.id} value={String(s.id)}>{s.name}</option>)}
        </select>

        <input value={search} onChange={e => setSearch(e.target.value)}
               placeholder="Search alerts…"
               className="border rounded px-2 py-1 text-sm flex-1 min-w-[160px]" />

        <span className="text-xs text-slate-500">
          {range.label} · {filtered.length} shown
        </span>
      </Card>

      <div className="space-y-2">
        {groups.length === 0 ? (
          <Card className="p-8 text-center text-slate-500">No alerts to show.</Card>
        ) : (
          groups.map(g => (
            <AlertCard key={g.head.id} alert={g.head}
                       groupCount={g.count} groupLast={g.last}
                       groupSiblings={g.siblings} onChanged={reload} />
          ))
        )}
      </div>
    </div>
  )
}

function QuickBtn({ active, onClick, children }: {
  active: boolean; onClick: () => void; children: React.ReactNode
}) {
  return (
    <button onClick={onClick}
            className={'px-3 py-1.5 rounded text-sm font-medium ' +
              (active ? 'bg-slate-800 text-white' : 'bg-slate-100 text-slate-700 hover:bg-slate-200')}>
      {children}
    </button>
  )
}
