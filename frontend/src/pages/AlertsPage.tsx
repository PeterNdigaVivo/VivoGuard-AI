// Alerts page — chain-wide feed using the shared AlertCard.
// May-2026 redesign: same card component as the per-store dashboard
// feed so titles, severity colours, and action buttons match exactly.

import { useEffect, useMemo, useState } from 'react'
import { Card, PageHeader } from '@/components/ui/Primitives'
import DateRangePicker, { rangeFor, type DateRange } from '@/components/DateRangePicker'
import { alerts as alertsApi, type Alert } from '@/api/alerts'
import { AlertCard, groupAlerts } from '@/components/AlertCard'
import { stores as storesApi, type Store } from '@/api/stores'

// Simple quick-filter buttons non-technical staff understand. The
// 'status' bucket isolates the recurring sales_floor_insight cards
// ("Status Update — <store>") so they don't pollute the urgent /
// attention / resolved feeds.
type Quick = 'urgent' | 'attention' | 'resolved' | 'all' | 'status'

const SFI_TYPE = 'sales_floor_insight'

export default function AlertsPage() {
  const [items, setItems] = useState<Alert[]>([])
  const [range, setRange] = useState<DateRange>(() => rangeFor('today'))
  const [quick, setQuick] = useState<Quick>('all')
  const [storeId, setStoreId] = useState<string>('')
  const [search, setSearch] = useState('')
  const [stores, setStores] = useState<Store[]>([])
  const [summary, setSummary] = useState({
    urgent: 0, attention: 0,
    resolved_today: 0, dismissed_today: 0,
    unread_urgent: 0,
    critical_today: 0, high_today: 0, medium_today: 0, low_today: 0,
    avg_response_seconds: null as number | null,
    today_count: 0, yesterday_count: 0,
    trend_vs_yesterday_pct: null as number | null,
    date_label: null as string | null,
  })
  // Bottom-right toast for the "resolved" confirmation. Auto-clears
  // after 4s. Use this for any user-facing success/error message
  // instead of window.alert() so the operator's flow isn't blocked.
  const [toast, setToast] = useState<string | null>(null)
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 4000)
    return () => clearTimeout(t)
  }, [toast])

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

  // Instant-feedback: any time a card fires the resolve/dismiss
  // event, locally decrement the urgent/attention count so the header
  // tally + sidebar badge update without waiting for the next poll.
  // The /summary fetch fired by reload() then reconciles the truth.
  useEffect(() => {
    function onResolved(e: Event) {
      const detail = (e as CustomEvent).detail || {}
      const id = detail.id
      const action: 'resolve' | 'dismiss' = detail.action === 'dismiss' ? 'dismiss' : 'resolve'
      const closed = items.find(a => a.id === id)
      if (!closed) return
      setSummary(s => ({
        ...s,
        urgent:    closed.severity_label === 'URGENT'    ? Math.max(0, s.urgent - 1)    : s.urgent,
        attention: closed.severity_label === 'ATTENTION' ? Math.max(0, s.attention - 1) : s.attention,
        unread_urgent: closed.severity_label === 'URGENT' && closed.status === 'new'
          ? Math.max(0, s.unread_urgent - 1) : s.unread_urgent,
        // Split resolved vs dismissed so the header reflects what the
        // operator actually clicked.
        resolved_today:  action === 'resolve' ? s.resolved_today + 1  : s.resolved_today,
        dismissed_today: action === 'dismiss' ? s.dismissed_today + 1 : s.dismissed_today,
      }))
    }
    window.addEventListener('vg:alert-resolved', onResolved)
    return () => window.removeEventListener('vg:alert-resolved', onResolved)
  }, [items])

  // Client-side quick-filter + search over the loaded window. Every
  // non-status filter EXCLUDES sales_floor_insight so the routine
  // 15-min "Status Update" alerts only show up under their own tab.
  const filtered = useMemo(() => {
    let rows = items
    if (quick === 'status') {
      rows = rows.filter(a => a.detection_type === SFI_TYPE)
    } else {
      rows = rows.filter(a => a.detection_type !== SFI_TYPE)
      if (quick === 'urgent')         rows = rows.filter(a => a.severity_label === 'URGENT' && a.status === 'new')
      else if (quick === 'attention') rows = rows.filter(a => a.severity_label === 'ATTENTION' && a.status === 'new')
      // 'confirmed' covers alerts the /resolve endpoint flipped (it
      // re-uses that bucket as a "handled" state). 'dismissed' covers
      // "Not a problem".
      else if (quick === 'resolved')  rows = rows.filter(a => ['resolved', 'confirmed', 'dismissed'].includes(a.status))
    }
    if (search.trim()) {
      const q = search.toLowerCase()
      rows = rows.filter(a =>
        (a.plain_title ?? a.title ?? '').toLowerCase().includes(q) ||
        (a.camera_name ?? '').toLowerCase().includes(q) ||
        (a.detection_type ?? '').toLowerCase().includes(q))
    }
    return rows
  }, [items, quick, search])

  // Per-bucket counts derived from the loaded items, so each filter
  // button's badge equals what the user will actually see when they
  // click it. Status-update alerts are excluded from every other
  // bucket; the Status Update tab is the only one that shows them.
  const counts = useMemo(() => {
    const nonSfi = items.filter(a => a.detection_type !== SFI_TYPE)
    return {
      urgent:    nonSfi.filter(a => a.severity_label === 'URGENT' && a.status === 'new').length,
      attention: nonSfi.filter(a => a.severity_label === 'ATTENTION' && a.status === 'new').length,
      resolved:  nonSfi.filter(a => ['resolved', 'confirmed', 'dismissed'].includes(a.status)).length,
      all:       nonSfi.length,
      status:    items.filter(a => a.detection_type === SFI_TYPE).length,
    }
  }, [items])

  const groups = groupAlerts(filtered)

  // Excel export — fetch with the bearer header (an <a href> can't
  // carry it) and trigger a download of the returned .xlsx blob.
  async function resolveAll() {
    // Use whatever filter the user can see — store + date window —
    // so the bulk action mirrors the visible list, not the whole DB.
    const newCount = items.filter(a => a.status === 'new').length
    if (newCount === 0) { setToast('There are no unresolved alerts to clear.'); return }
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
      // Toast with the count from the server — never lies about what
      // actually flipped.
      setToast(`✅ ${resolved} alert${resolved === 1 ? '' : 's'} resolved successfully`)
      // Decrement summary immediately so the page header reflects it
      // before the network reload returns.
      setSummary(s => ({
        ...s,
        urgent: 0, attention: 0, unread_urgent: 0,
        resolved_today: s.resolved_today + resolved,
      }))
      window.dispatchEvent(new CustomEvent('vg:alert-resolved', { detail: { bulk: resolved } }))
      reload()
    } catch (e) {
      setToast(`Could not resolve: ${e}`)
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

      {/* Executive summary bar — spec Part 1 §3. Four-tier severity
          counts + resolved tally + avg-time-to-resolve + vs-yesterday
          trend in one glanceable strip. */}
      <ExecutiveSummaryBar summary={summary} />

      {/* Legacy compact tally — kept beneath the bar for the operators
          who still scan for it. Drops once everyone's adopted the new
          card-style bar above. */}
      <div className="text-xs text-slate-500 mb-3">
        Today: <strong className="text-red-600">{summary.urgent} urgent</strong>
        {' · '}<strong className="text-amber-600">{summary.attention} need attention</strong>
        {' · '}<strong className="text-emerald-600">{summary.resolved_today} resolved</strong>
        {' · '}<strong className="text-slate-600">{summary.dismissed_today} dismissed</strong>
      </div>

      {/* Simple filter bar. Each button shows a live count so the
          operator sees at a glance how much is in each bucket. */}
      <Card className="p-3 mb-4 flex flex-wrap gap-2 items-center">
        <QuickBtn active={quick === 'urgent'}    onClick={() => setQuick('urgent')}>
          🔴 Urgent ({counts.urgent})
        </QuickBtn>
        <QuickBtn active={quick === 'attention'} onClick={() => setQuick('attention')}>
          🟡 Needs Attention ({counts.attention})
        </QuickBtn>
        <QuickBtn active={quick === 'resolved'}  onClick={() => setQuick('resolved')}>
          ✅ Resolved ({counts.resolved})
        </QuickBtn>
        <QuickBtn active={quick === 'status'}    onClick={() => setQuick('status')}>
          📊 Status Update ({counts.status})
        </QuickBtn>
        <QuickBtn active={quick === 'all'}       onClick={() => setQuick('all')}>
          📋 All ({counts.all})
        </QuickBtn>

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

      {/* Toast — bottom-right, auto-dismiss after 4s. */}
      {toast && (
        <div className="fixed bottom-6 right-6 bg-slate-800 text-white text-sm
                        rounded shadow-lg px-4 py-2 z-50">
          {toast}
        </div>
      )}
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


// Executive summary bar — spec Part 1 §3. One card at the top of the
// page with the day label, four severity counts, resolved tally, the
// average time-to-resolve, and the vs-yesterday trend.
function ExecutiveSummaryBar({ summary }: {
  summary: {
    critical_today: number; high_today: number
    medium_today:   number; low_today:  number
    resolved_today: number
    avg_response_seconds: number | null
    today_count: number; yesterday_count: number
    trend_vs_yesterday_pct: number | null
    date_label: string | null
  }
}) {
  const today = summary.date_label || new Date().toLocaleDateString(
    'en-GB', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })
  const avg = summary.avg_response_seconds
  const avgText = avg == null
    ? '—'
    : avg < 60
      ? `${Math.round(avg)}s`
      : `${Math.floor(avg / 60)}m ${Math.round(avg % 60)}s`
  const trend = summary.trend_vs_yesterday_pct
  const trendText = trend == null
    ? null
    : trend > 0 ? `📈 Alerts up ${trend}% vs yesterday`
      : trend < 0 ? `📉 Alerts down ${Math.abs(trend)}% vs yesterday`
      : '➡️ Same volume as yesterday'
  const trendColor = trend == null ? 'text-slate-500'
    : trend > 0 ? 'text-red-600'
    : trend < 0 ? 'text-emerald-600'
    : 'text-slate-500'

  return (
    <div className="rounded-lg border border-slate-200 bg-white shadow-sm
                    mb-3 px-4 py-3">
      <div className="text-xs uppercase tracking-wide text-slate-500 mb-1">
        Today's Store Health — {today}
      </div>
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-sm">
        <SevPill label="Critical" emoji="🔴" count={summary.critical_today}
                 tone="text-red-700 bg-red-50 border-red-200" />
        <SevPill label="High"     emoji="🟠" count={summary.high_today}
                 tone="text-orange-700 bg-orange-50 border-orange-200" />
        <SevPill label="Medium"   emoji="🟡" count={summary.medium_today}
                 tone="text-yellow-700 bg-yellow-50 border-yellow-200" />
        <SevPill label="Low"      emoji="🔵" count={summary.low_today}
                 tone="text-blue-700 bg-blue-50 border-blue-200" />
        <span className="text-slate-300">|</span>
        <span className="text-emerald-700">
          ✅ <strong className="tabular-nums">{summary.resolved_today}</strong> Resolved
        </span>
        <span className="text-slate-600">
          ⏱ Avg Response: <strong className="tabular-nums">{avgText}</strong>
        </span>
        {trendText && (
          <span className={'ml-auto ' + trendColor}>{trendText}</span>
        )}
      </div>
    </div>
  )
}

function SevPill({ label, emoji, count, tone }: {
  label: string; emoji: string; count: number; tone: string
}) {
  return (
    <span className={'inline-flex items-center gap-1.5 px-2 py-0.5 rounded ' +
                     'border text-xs font-medium ' + tone}>
      <span>{emoji}</span>
      <span className="tabular-nums">{count}</span>
      <span className="opacity-80">{label}</span>
    </span>
  )
}
