// Side-by-side store comparison — bar chart for any metric across all
// stores. SVG bars, no external chart lib.

import { useEffect, useMemo, useState } from 'react'
import { Badge, Card, PageHeader, Select } from '@/components/ui/Primitives'
import { api } from '@/api/client'

const METRICS = [
  'occupancy', 'queue_length', 'queue_wait_seconds', 'staff_present_pct',
  'unique_visitors', 'passersby', 'stop_rate', 'dwell_seconds',
  'visitor_count_in', 'visitor_count_out', 'uniform_compliance_pct',
]
const WINDOWS = [1, 7, 30]

interface CompareRow {
  store_id: number; store_name: string; country: string
  avg: number; peak: number; total: number; samples: number
}

export default function ComparePage() {
  const [metric, setMetric] = useState('occupancy')
  const [days, setDays] = useState(7)
  const [data, setData] = useState<{ stores: CompareRow[] } | null>(null)

  useEffect(() => {
    api<{ stores: CompareRow[] }>(`/analytics/compare?metric_type=${metric}&days=${days}`)
      .then(setData)
      .catch(console.error)
  }, [metric, days])

  const max = useMemo(
    () => Math.max(1, ...(data?.stores ?? []).map(s => s.avg)),
    [data],
  )

  return (
    <div className="p-6">
      <PageHeader
        title="Compare stores"
        actions={<>
          <Select value={metric} onChange={e => setMetric(e.target.value)}>
            {METRICS.map(m => <option key={m} value={m}>{m}</option>)}
          </Select>
          <Select value={days} onChange={e => setDays(Number(e.target.value))}>
            {WINDOWS.map(w => <option key={w} value={w}>last {w}d</option>)}
          </Select>
        </>}
      />

      <Card className="p-4">
        {!data && <div className="text-slate-500">Loading…</div>}
        {data && data.stores.length === 0 && (
          <div className="text-slate-500">No data for this metric in the selected window.</div>
        )}
        <div className="space-y-2">
          {data?.stores.map(s => (
            <div key={s.store_id} className="flex items-center gap-3">
              <div className="w-40 truncate font-medium" title={s.store_name}>{s.store_name}</div>
              <Badge color="slate">{s.country}</Badge>
              <div className="flex-1 bg-slate-100 rounded h-6 relative overflow-hidden">
                <div className="absolute inset-y-0 left-0 bg-sky-500"
                     style={{ width: `${(s.avg / max) * 100}%` }} />
                <div className="absolute inset-y-0 left-0 right-0 px-2 flex items-center text-xs text-slate-700">
                  avg {s.avg.toFixed(2)} · peak {s.peak.toFixed(2)} · {s.samples} samples
                </div>
              </div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  )
}
