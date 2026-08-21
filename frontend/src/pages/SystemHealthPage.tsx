// System health — disk, GPUs, per-camera FPS, today's alert count.

import { useEffect, useState } from 'react'
import { api } from '@/api/client'
import { Badge, Card, PageHeader } from '@/components/ui/Primitives'

interface CamHealth {
  camera_id: number; name: string; status: string
  configured_status: string
  fps: number | null; last_frame_at: number | null
  seconds_since_last_frame: number | null
  error: string | null; network_type: string
}
interface SystemHealth {
  now: string
  cameras: CamHealth[]
  disk_total_gb: number; disk_used_gb: number; disk_free_gb: number
  gpus: { index: number; name: string; total_mb: number; free_mb: number }[]
  alerts_today: number
}

// Sprint 1.2 inference-latency telemetry (GET /analytics/perf).
interface PerfCamera {
  camera_id: number; camera_name: string | null; store_name: string | null
  p50_ms: number; p95_ms: number; p99_ms: number; avg_ms: number
  frame_count: number; backend: string | null; format: string | null
  last_seen: string | null
}
interface PerfReport {
  window_hours: number
  chain: {
    avg_ms: number | null; p95_ms_worst_cam: number | null
    p99_ms_worst_cam: number | null; cameras_reporting: number; frames: number
  }
  per_camera: PerfCamera[]
  slowest: PerfCamera[]
}

export default function SystemHealthPage() {
  const [data, setData] = useState<SystemHealth | null>(null)
  const [perf, setPerf] = useState<PerfReport | null>(null)
  useEffect(() => {
    const fetch = () => api<SystemHealth>('/system/health').then(setData).catch(console.error)
    fetch()
    const t = setInterval(fetch, 5000)
    return () => clearInterval(t)
  }, [])
  useEffect(() => {
    const f = () => api<PerfReport>('/analytics/perf?hours=24').then(setPerf).catch(() => {})
    f()
    const t = setInterval(f, 60_000)
    return () => clearInterval(t)
  }, [])

  if (!data) return <div className="p-6 text-slate-500 dark:text-slate-300">Loading…</div>

  const usedPct = data.disk_total_gb ? Math.round(data.disk_used_gb / data.disk_total_gb * 100) : 0

  return (
    <div className="p-6">
      <PageHeader title="System Health" />

      <div className="grid grid-cols-3 gap-4 mb-4">
        <Card className="p-4 dark:bg-slate-900 dark:border-slate-800">
          <div className="text-sm text-slate-500 dark:text-slate-300">Storage</div>
          <div className="text-2xl font-semibold">{data.disk_used_gb} / {data.disk_total_gb} GB</div>
          <div className="h-2 mt-2 bg-slate-200 dark:bg-slate-700 rounded">
            <div className="h-2 rounded bg-sky-600" style={{ width: `${usedPct}%` }} />
          </div>
          <div className="text-xs text-slate-500 dark:text-slate-400 mt-1">{data.disk_free_gb} GB free</div>
        </Card>

        <Card className="p-4 dark:bg-slate-900 dark:border-slate-800">
          <div className="text-sm text-slate-500 dark:text-slate-300">GPUs</div>
          {data.gpus.length === 0 && <div className="text-slate-400 dark:text-slate-400">CPU-only mode</div>}
          {data.gpus.map(g => (
            <div key={g.index} className="text-sm">
              <div className="font-medium">{g.name}</div>
              <div className="text-slate-500 dark:text-slate-400 text-xs">
                {Math.round((g.total_mb - g.free_mb) / 1024 * 10) / 10} / {Math.round(g.total_mb / 1024 * 10) / 10} GB used
              </div>
            </div>
          ))}
        </Card>

        <Card className="p-4 dark:bg-slate-900 dark:border-slate-800">
          <div className="text-sm text-slate-500 dark:text-slate-300">Alerts today</div>
          <div className="text-3xl font-semibold">{data.alerts_today}</div>
        </Card>
      </div>

      {/* Inference performance (Sprint 1.2 PerfTracker) */}
      <Card className="p-4 mb-4 dark:bg-slate-900 dark:border-slate-800">
        <div className="flex items-center justify-between mb-3">
          <div className="text-sm font-semibold text-slate-700 dark:text-slate-200">
            Inference Performance
            <span className="text-slate-400 dark:text-slate-400 font-normal"> · last 24h</span>
          </div>
          {perf && perf.per_camera[0]?.backend && (
            <Badge color="slate">
              {perf.per_camera[0].backend}/{perf.per_camera[0].format ?? '—'}
            </Badge>
          )}
        </div>
        {!perf || perf.chain.cameras_reporting === 0 ? (
          <div className="text-sm text-slate-400 dark:text-slate-400">
            No telemetry yet — populates after cameras process ~100 frames.
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div>
              <div className="text-xs text-slate-500 dark:text-slate-300">Chain-wide avg inference</div>
              <div className="text-2xl font-semibold">
                {perf.chain.avg_ms != null ? `${perf.chain.avg_ms} ms` : '—'}
              </div>
              <div className="text-xs text-slate-400 dark:text-slate-400 mt-0.5">
                {perf.chain.cameras_reporting} cameras · {perf.chain.frames.toLocaleString()} frames
              </div>
            </div>
            <div className="md:col-span-2">
              <div className="text-xs text-slate-500 dark:text-slate-300 mb-1">Slowest cameras (p99)</div>
              <table className="w-full text-xs">
                <tbody>
                  {perf.slowest.map(c => (
                    <tr key={c.camera_id} className="border-t border-slate-100 dark:border-slate-800">
                      <td className="py-1">{c.camera_name ?? `Cam ${c.camera_id}`}</td>
                      <td className="py-1 text-slate-500 dark:text-slate-400">{c.store_name ?? ''}</td>
                      <td className="py-1 text-right tabular-nums">p99 {c.p99_ms} ms</td>
                      <td className="py-1 text-right tabular-nums text-slate-500 dark:text-slate-400">
                        p50 {c.p50_ms} ms
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </Card>

      <Card className="dark:bg-slate-900 dark:border-slate-800">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600 dark:bg-slate-800 dark:text-slate-200">
            <tr>
              <th className="text-left p-3">Camera</th>
              <th className="text-left p-3">Status</th>
              <th className="text-left p-3">Configured</th>
              <th className="text-left p-3">FPS</th>
              <th className="text-left p-3">Network</th>
              <th className="text-left p-3">Last frame</th>
              <th className="text-left p-3">Error</th>
            </tr>
          </thead>
          <tbody>
            {data.cameras.map(c => (
              <tr key={c.camera_id} className="border-t dark:border-slate-800">
                <td className="p-3">{c.name}</td>
                <td className="p-3">
                  <Badge color={c.status === 'online' ? 'green' : c.status === 'offline' ? 'red' : 'amber'}>
                    {c.status}
                  </Badge>
                </td>
                <td className="p-3 text-xs text-slate-500 dark:text-slate-400">{c.configured_status}</td>
                <td className="p-3">{c.fps ? c.fps.toFixed(1) : '—'}</td>
                <td className="p-3 uppercase text-xs">{c.network_type}</td>
                <td className="p-3 text-xs text-slate-500 dark:text-slate-400">
                  {c.last_frame_at ? new Date(c.last_frame_at * 1000).toLocaleTimeString() : '—'}
                </td>
                <td className="p-3 text-red-600 dark:text-red-400 text-xs">{c.error}</td>
              </tr>
            ))}
            {!data.cameras.length && (
              <tr><td colSpan={7} className="p-6 text-center text-slate-500 dark:text-slate-300">No cameras configured.</td></tr>
            )}
          </tbody>
        </table>
      </Card>
    </div>
  )
}
