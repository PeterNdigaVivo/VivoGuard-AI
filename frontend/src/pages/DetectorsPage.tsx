// Detector catalog — at-a-glance view of what works out of the box
// versus what needs custom training. Rule 6.

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge, Card, Input, PageHeader, Skeleton } from '@/components/ui/Primitives'
import { api } from '@/api/client'

interface Entry {
  status: 'active' | 'training_required'
  needs_zone: boolean
  purpose: string | null
  purpose_label: string | null
  training_hint?: string
}

export default function DetectorsPage() {
  const [data, setData] = useState<Record<string, Entry> | null>(null)
  const [filter, setFilter] = useState('')

  useEffect(() => {
    api<Record<string, Entry>>('/detectors/catalog').then(setData).catch(console.error)
  }, [])

  if (!data) return (
    <div className="p-6">
      <PageHeader title="Detection capabilities" />
      <Skeleton className="h-96" />
    </div>
  )

  const entries = Object.entries(data)
    .filter(([k]) => !filter || k.includes(filter.toLowerCase()))
    .sort((a, b) =>
      a[1].status === b[1].status ? a[0].localeCompare(b[0])
      : (a[1].status === 'active' ? -1 : 1))

  return (
    <div className="p-6">
      <PageHeader title="Detection capabilities" />
      <Card className="p-3 mb-4">
        <Input className="w-full" placeholder="Filter (e.g. queue, weapon, dwell)"
               value={filter} onChange={e => setFilter(e.target.value)} />
      </Card>

      <Card>
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600">
            <tr>
              <th className="text-left p-3">Detector</th>
              <th className="text-left p-3">Status</th>
              <th className="text-left p-3">Operator action</th>
              <th className="text-left p-3">Notes</th>
            </tr>
          </thead>
          <tbody>
            {entries.map(([k, v]) => (
              <tr key={k} className="border-t">
                <td className="p-3 font-mono">{k}</td>
                <td className="p-3">
                  {v.status === 'active'
                    ? <Badge color="green">Active</Badge>
                    : <Badge color="amber">Training required</Badge>}
                </td>
                <td className="p-3">
                  {v.needs_zone && v.purpose_label
                    ? <>Draw a zone — <Link to="/cameras" className="text-sky-600 hover:underline">pick a camera</Link>,
                       choose <em>"{v.purpose_label}"</em></>
                    : v.status === 'active'
                      ? 'None — runs automatically'
                      : 'Train a custom model in AI Training'}
                </td>
                <td className="p-3 text-slate-600 text-xs max-w-md">
                  {v.training_hint ?? '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  )
}
