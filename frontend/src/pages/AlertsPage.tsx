// Alerts page — paginated list with filters, real-time prepend via the
// WebSocket alerts feed, confirm/dismiss buttons.

import { useEffect, useState } from 'react'
import { Badge, Button, Card, Input, PageHeader, Select } from '@/components/ui/Primitives'
import { alerts as alertsApi, type Alert } from '@/api/alerts'

export default function AlertsPage() {
  const [items, setItems] = useState<Alert[]>([])
  const [filters, setFilters] = useState({
    status: '', detection_type: '', camera_id: '',
    since: '', until: '',
  })

  const reload = () => alertsApi.list({
    status: filters.status || undefined,
    detection_type: filters.detection_type || undefined,
    camera_id: filters.camera_id || undefined,
    since: filters.since || undefined,
    until: filters.until || undefined,
  }).then(setItems)
  useEffect(() => { reload() }, [filters.status, filters.detection_type, filters.camera_id])

  // Real-time prepend: when /ws/alerts pushes a new event, refetch.
  useEffect(() => alertsApi.subscribe(() => reload()), [])

  async function confirm(id: number) { await alertsApi.confirm(id); reload() }
  async function dismiss(id: number) { await alertsApi.dismiss(id); reload() }

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
      <PageHeader title="Alerts" actions={<Button variant="ghost" onClick={exportCSV}>Export CSV</Button>} />

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
      </Card>

      <Card>
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600">
            <tr>
              <th className="text-left p-3">When</th>
              <th className="text-left p-3">Camera</th>
              <th className="text-left p-3">Type</th>
              <th className="text-left p-3">Confidence</th>
              <th className="text-left p-3">Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.map(a => (
              <tr key={a.id} className="border-t hover:bg-slate-50">
                <td className="p-3 whitespace-nowrap">{new Date(a.created_at).toLocaleString()}</td>
                <td className="p-3">{a.camera_name ?? `#${a.camera_id}`}</td>
                <td className="p-3"><Badge color="sky">{a.detection_type}</Badge></td>
                <td className="p-3">{a.confidence?.toFixed(2) ?? '—'}</td>
                <td className="p-3">
                  {a.status === 'new'       && <Badge color="amber">new</Badge>}
                  {a.status === 'confirmed' && <Badge color="green">confirmed</Badge>}
                  {a.status === 'dismissed' && <Badge color="slate">dismissed</Badge>}
                </td>
                <td className="p-3 text-right whitespace-nowrap">
                  {a.status === 'new' && <>
                    <Button onClick={() => confirm(a.id)} className="mr-2">Confirm</Button>
                    <Button variant="ghost" onClick={() => dismiss(a.id)}>Dismiss</Button>
                  </>}
                </td>
              </tr>
            ))}
            {!items.length && (
              <tr><td colSpan={6} className="p-8 text-center text-slate-500">No alerts.</td></tr>
            )}
          </tbody>
        </table>
      </Card>
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
