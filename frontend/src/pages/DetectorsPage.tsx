// Detector catalogue — what each detector is really doing across the fleet.
//
// The state is computed on the server from live configuration and review
// evidence (app/services/detector_status.py). "Active" is gone on purpose:
// being able to run is not proof that anything is running, zoned,
// scheduled or accurate.

import { useEffect, useState } from 'react'
import { Badge, Card, Input, PageHeader, Skeleton } from '@/components/ui/Primitives'
import { api } from '@/api/client'
import { labelForDetector } from '@/lib/detectorLabels'

type DetectorState =
  | 'available' | 'configured' | 'validated'
  | 'review_only' | 'training_required' | 'disabled'

interface Entry {
  label: string | null
  description: string | null
  purpose_label: string | null
  state: DetectorState
  configured_cameras: number
  configured_anywhere: boolean
  needs_zone: boolean
  cameras_missing_zone: number
  needs_schedule: boolean
  stores_missing_hours: number
  requirements_met: boolean
  reviewed_30d: number
  precision_30d: number | null
  next_action: string
  admin_note?: string
}

export const STATE_LABEL: Record<DetectorState, string> = {
  available: 'Available',
  configured: 'Configured',
  validated: 'Validated',
  review_only: 'Review-only',
  training_required: 'Training required',
  disabled: 'Disabled',
}
const STATE_COLOUR: Record<DetectorState, 'green' | 'red' | 'amber' | 'slate' | 'sky'> = {
  validated: 'green',
  configured: 'sky',
  review_only: 'amber',
  available: 'slate',
  disabled: 'slate',
  training_required: 'red',
}
// Things needing a decision first, then working detectors, then the rest.
const STATE_ORDER: DetectorState[] = [
  'review_only', 'configured', 'validated', 'available', 'training_required', 'disabled',
]

export default function DetectorsPage() {
  const [data, setData] = useState<Record<string, Entry> | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState('')

  useEffect(() => {
    api<Record<string, Entry>>('/detectors/catalog').then(setData)
      .catch(e => setError(String(e)))
  }, [])

  if (error) return (
    <div className="p-6">
      <PageHeader title="Detectors" />
      <Card className="p-4 text-red-700 dark:text-red-300">Could not load detectors: {error}</Card>
    </div>
  )
  if (!data) return (
    <div className="p-6">
      <PageHeader title="Detectors" />
      <Skeleton className="h-96" />
    </div>
  )

  const f = filter.trim().toLowerCase()
  const entries = Object.entries(data)
    .filter(([k, v]) => !f || k.includes(f)
      || labelForDetector(k).toLowerCase().includes(f)
      || STATE_LABEL[v.state].toLowerCase().includes(f))
    .sort((a, b) => STATE_ORDER.indexOf(a[1].state) - STATE_ORDER.indexOf(b[1].state)
      || b[1].configured_cameras - a[1].configured_cameras
      || a[0].localeCompare(b[0]))

  const counts = STATE_ORDER.map(s => [s, Object.values(data).filter(v => v.state === s).length] as const)

  return (
    <div className="p-6">
      <PageHeader title="Detectors" />
      <Card className="p-3 mb-4 flex flex-wrap items-center gap-3">
        <Input className="flex-1 min-w-[16rem]" placeholder="Filter by name or state"
               value={filter} onChange={e => setFilter(e.target.value)} />
        <div className="flex flex-wrap gap-2 text-xs">
          {counts.map(([s, n]) => (
            <Badge key={s} color={STATE_COLOUR[s]}>{STATE_LABEL[s]} {n}</Badge>
          ))}
        </div>
      </Card>

      <Card>
        <table className="w-full text-sm">
          <thead className="bg-slate-50 dark:bg-slate-800 text-slate-600 dark:text-slate-200">
            <tr>
              <th className="text-left p-3">Detector</th>
              <th className="text-left p-3">State</th>
              <th className="text-right p-3">Cameras</th>
              <th className="text-left p-3">Zones &amp; hours</th>
              <th className="text-left p-3">Accuracy (30 days)</th>
              <th className="text-left p-3">Next action</th>
            </tr>
          </thead>
          <tbody>
            {entries.map(([k, v]) => (
              <tr key={k} className="border-t dark:border-slate-800 align-top">
                <td className="p-3">
                  <div className="font-medium text-slate-800 dark:text-slate-100">
                    {v.label ?? labelForDetector(k)}
                  </div>
                  <div className="text-xs text-slate-500 dark:text-slate-300 font-mono">{k}</div>
                  {v.admin_note && (
                    <div className="text-xs text-slate-500 dark:text-slate-300 mt-1 max-w-xs">
                      Admin: {v.admin_note}
                    </div>
                  )}
                </td>
                <td className="p-3"><Badge color={STATE_COLOUR[v.state]}>{STATE_LABEL[v.state]}</Badge></td>
                <td className="p-3 text-right tabular-nums">{v.configured_cameras}</td>
                <td className="p-3 text-xs text-slate-600 dark:text-slate-300">
                  {!v.needs_zone && !v.needs_schedule ? 'None needed'
                    : !v.configured_anywhere ? (v.needs_zone ? `Needs a zone${v.purpose_label ? ` (“${v.purpose_label}”)` : ''}` : 'Needs opening hours')
                    : v.requirements_met ? '✓ In place'
                    : [v.cameras_missing_zone ? `${v.cameras_missing_zone} camera(s) missing a zone` : null,
                       v.stores_missing_hours ? `${v.stores_missing_hours} store(s) missing hours` : null]
                        .filter(Boolean).join(' · ')}
                </td>
                <td className="p-3 text-xs text-slate-600 dark:text-slate-300">
                  {v.reviewed_30d === 0 ? 'Not reviewed yet'
                    : `${Math.round((v.precision_30d ?? 0) * 100)}% correct of ${v.reviewed_30d} reviewed`}
                </td>
                <td className="p-3 text-xs text-slate-700 dark:text-slate-200 max-w-xs">{v.next_action}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  )
}
