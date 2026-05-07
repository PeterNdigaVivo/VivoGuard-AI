// System health — disk, GPUs, per-camera FPS, today's alert count.

import { useEffect, useState } from 'react'
import { api } from '@/api/client'
import { Badge, Card, PageHeader } from '@/components/ui/Primitives'

interface CamHealth {
  camera_id: number; name: string; status: string
  fps: number | null; last_frame_at: number | null
  error: string | null; network_type: string
}
interface SystemHealth {
  now: string
  cameras: CamHealth[]
  disk_total_gb: number; disk_used_gb: number; disk_free_gb: number
  gpus: { index: number; name: string; total_mb: number; free_mb: number }[]
  alerts_today: number
}

export default function SystemHealthPage() {
  const [data, setData] = useState<SystemHealth | null>(null)
  useEffect(() => {
    const fetch = () => api<SystemHealth>('/system/health').then(setData).catch(console.error)
    fetch()
    const t = setInterval(fetch, 5000)
    return () => clearInterval(t)
  }, [])

  if (!data) return <div className="p-6 text-slate-500">Loading…</div>

  const usedPct = data.disk_total_gb ? Math.round(data.disk_used_gb / data.disk_total_gb * 100) : 0

  return (
    <div className="p-6">
      <PageHeader title="System Health" />

      <div className="grid grid-cols-3 gap-4 mb-4">
        <Card className="p-4">
          <div className="text-sm text-slate-500">Storage</div>
          <div className="text-2xl font-semibold">{data.disk_used_gb} / {data.disk_total_gb} GB</div>
          <div className="h-2 mt-2 bg-slate-200 rounded">
            <div className="h-2 rounded bg-sky-600" style={{ width: `${usedPct}%` }} />
          </div>
          <div className="text-xs text-slate-500 mt-1">{data.disk_free_gb} GB free</div>
        </Card>

        <Card className="p-4">
          <div className="text-sm text-slate-500">GPUs</div>
          {data.gpus.length === 0 && <div className="text-slate-400">CPU-only mode</div>}
          {data.gpus.map(g => (
            <div key={g.index} className="text-sm">
              <div className="font-medium">{g.name}</div>
              <div className="text-slate-500 text-xs">
                {Math.round((g.total_mb - g.free_mb) / 1024 * 10) / 10} / {Math.round(g.total_mb / 1024 * 10) / 10} GB used
              </div>
            </div>
          ))}
        </Card>

        <Card className="p-4">
          <div className="text-sm text-slate-500">Alerts today</div>
          <div className="text-3xl font-semibold">{data.alerts_today}</div>
        </Card>
      </div>

      <Card>
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600">
            <tr>
              <th className="text-left p-3">Camera</th>
              <th className="text-left p-3">Status</th>
              <th className="text-left p-3">FPS</th>
              <th className="text-left p-3">Network</th>
              <th className="text-left p-3">Last frame</th>
              <th className="text-left p-3">Error</th>
            </tr>
          </thead>
          <tbody>
            {data.cameras.map(c => (
              <tr key={c.camera_id} className="border-t">
                <td className="p-3">{c.name}</td>
                <td className="p-3">
                  <Badge color={c.status === 'online' ? 'green' : c.status === 'offline' ? 'red' : 'amber'}>
                    {c.status}
                  </Badge>
                </td>
                <td className="p-3">{c.fps ? c.fps.toFixed(1) : '—'}</td>
                <td className="p-3 uppercase text-xs">{c.network_type}</td>
                <td className="p-3 text-xs text-slate-500">
                  {c.last_frame_at ? new Date(c.last_frame_at * 1000).toLocaleTimeString() : '—'}
                </td>
                <td className="p-3 text-red-600 text-xs">{c.error}</td>
              </tr>
            ))}
            {!data.cameras.length && (
              <tr><td colSpan={6} className="p-6 text-center text-slate-500">No cameras configured.</td></tr>
            )}
          </tbody>
        </table>
      </Card>
    </div>
  )
}
