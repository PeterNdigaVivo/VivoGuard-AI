// Alerts page — chain-wide feed using the shared AlertCard.
// May-2026 redesign: same card component as the per-store dashboard
// feed so titles, severity colours, and action buttons match exactly.

import { useEffect, useState } from 'react'
import { Button, Card, Input, PageHeader, Select } from '@/components/ui/Primitives'
import DateRangePicker, { rangeFor, type DateRange } from '@/components/DateRangePicker'
import { alerts as alertsApi, type Alert } from '@/api/alerts'
import { AlertCard, groupAlerts } from '@/components/AlertCard'

export default function AlertsPage() {
  const [items, setItems] = useState<Alert[]>([])
  const [range, setRange] = useState<DateRange>(() => rangeFor('today'))
  const [filters, setFilters] = useState({
    status: '', detection_type: '', camera_id: '',
  })

  const reload = () => alertsApi.list({
    status: filters.status || undefined,
    detection_type: filters.detection_type || undefined,
    camera_id: filters.camera_id || undefined,
    since: range.since,
    until: range.until,
  }).then(setItems)
  useEffect(() => { reload() },
    [filters.status, filters.detection_type, filters.camera_id,
     range.since, range.until])

  // Real-time prepend: when /ws/alerts pushes a new event, refetch.
  useEffect(() => alertsApi.subscribe(() => reload()), [])

  // `groups` collapses repeat-of-same-thing alerts into one row each
  // (e.g. "Counter Unstaffed ×7 today") to keep the feed scannable.
  const groups = groupAlerts(items)

  function exportCSV() {
    const rows = [
      ['id', 'camera', 'type', 'confidence', 'status', 'created_at'].join(','),
      ...items.map(a => [a.id, a.camera_name, a.detection_type, a.confidence, a.status, a.created_at]
        .map(v => JSON.stringify(v ?? '')).join(',')),
    ]
    const blob = new Blob([rows.join('\n')], { type: 'text/csv' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob); a.download = 'alerts.csv'; a.click()
  }

  return (
    <div className="p-6">
      <PageHeader title="Alerts" actions={
        <>
          <DateRangePicker value={range} onChange={setRange} />
          <Button variant="ghost" onClick={exportCSV}>Export CSV</Button>
        </>
      } />

      <Card className="p-3 mb-4 flex flex-wrap gap-3 items-end">
        <Filter label="Status">
          <Select value={filters.status} onChange={e => setFilters({ ...filters, status: e.target.value })}>
            <option value="">all</option>
            <option value="new">new</option>
            <option value="confirmed">confirmed</option>
            <option value="dismissed">dismissed</option>
          </Select>
        </Filter>
        <Filter label="Detection type">
          <Input value={filters.detection_type}
                 onChange={e => setFilters({ ...filters, detection_type: e.target.value })}
                 placeholder="person, fire, …" />
        </Filter>
        <Filter label="Camera ID">
          <Input value={filters.camera_id}
                 onChange={e => setFilters({ ...filters, camera_id: e.target.value })} />
        </Filter>
        <div className="text-xs text-slate-500 ml-auto">
          Showing <strong>{range.label}</strong> · {items.length} result{items.length === 1 ? '' : 's'}
        </div>
      </Card>

      <div className="space-y-2">
        {groups.length === 0 ? (
          <Card className="p-8 text-center text-slate-500">
            No alerts in this window.
          </Card>
        ) : (
          groups.map(g => (
            <AlertCard key={g.head.id}
                       alert={g.head}
                       groupCount={g.count}
                       groupLast={g.last}
                       groupSiblings={g.siblings}
                       onChanged={reload} />
          ))
        )}
      </div>
    </div>
  )
}

function Filter({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="text-sm">
      <div className="text-xs text-slate-500 mb-1">{label}</div>
      {children}
    </label>
  )
}
