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
  inference_pipeline: {
    cameras_total: number; cameras_fresh: number
    cameras_actively_inferencing: number | null
    cameras_waiting_for_worker: number | null
    inference_queue_depth: number | null
    estimated_full_rotation_seconds: number | null
    cameras_without_frames: number
    critical_cameras_total: number
    critical_cameras_overdue: number | null
    critical_max_gap_seconds: number | null
    critical_gap_sla_seconds: number
    standard_cameras_total: number
    standard_cameras_overdue: number | null
    standard_max_gap_seconds: number | null
    standard_gap_sla_seconds: number
  } | null
  inference_batch_shadow: {
    mode: 'shadow'; authoritative: false
    configured_cameras: number; cameras_served: number; fresh_candidates: number
    batch_size_limit: number; batches_processed: number
    frames_processed: number; detections_observed_not_emitted: number
    errors: number; p50_batch_ms: number | null; p95_batch_ms: number | null
    p95_per_frame_ms: number | null
    max_camera_schedule_wait_seconds: number
  } | null
}

interface CapacityAcceptance {
  status: 'pending' | 'failed' | 'capacity_ready'
  capacity_gate_passed: boolean
  promotion_ready: false
  accuracy_gate_evaluated: false
  accuracy_note: string
  checks: {
    name: string; passed: boolean; actual: unknown; required: string
  }[]
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
  const [capacity, setCapacity] = useState<CapacityAcceptance | null>(null)
  useEffect(() => {
    const fetch = () => api<SystemHealth>('/system/health').then(setData).catch(console.error)
    fetch()
    const t = setInterval(fetch, 5000)
    return () => clearInterval(t)
  }, [])
  useEffect(() => {
    const f = () => api<CapacityAcceptance>('/system/inference-acceptance').then(setCapacity).catch(() => {})
    f()
    const t = setInterval(f, 60_000)
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
  const duration = (seconds: number | null) => {
    if (seconds == null) return '—'
    if (seconds < 60) return `${Math.round(seconds)}s`
    return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
  }
  const inferenceDegraded = data.inference_pipeline != null && (
    data.inference_pipeline.cameras_without_frames > 0 ||
    (data.inference_pipeline.cameras_waiting_for_worker ?? 0) > 0 ||
    (data.inference_pipeline.critical_cameras_overdue ?? 0) > 0 ||
    (data.inference_pipeline.standard_cameras_overdue ?? 0) > 0
  )

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

      <Card className="p-4 mb-4 dark:bg-slate-900 dark:border-slate-800">
        <div className="flex items-center justify-between mb-3">
          <div className="text-sm font-semibold text-slate-700 dark:text-slate-200">
            Inference Coverage and Capacity
          </div>
          {data.inference_pipeline && (
            <Badge color={inferenceDegraded ? 'amber' : 'green'}>
              {inferenceDegraded ? 'Degraded' : 'Current'}
            </Badge>
          )}
        </div>
        {!data.inference_pipeline ? (
          <div className="text-sm text-slate-400 dark:text-slate-400">No inference supervisor telemetry.</div>
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-5 gap-4 text-sm">
            <div><div className="text-xs text-slate-500">AI cameras</div><div className="text-xl font-semibold">{data.inference_pipeline.cameras_total}</div></div>
            <div><div className="text-xs text-slate-500">Fresh feeds</div><div className="text-xl font-semibold">{data.inference_pipeline.cameras_fresh}</div></div>
            <div><div className="text-xs text-slate-500">Active tasks</div><div className="text-xl font-semibold">{data.inference_pipeline.cameras_actively_inferencing ?? '—'}</div></div>
            <div><div className="text-xs text-slate-500">Waiting / queue</div><div className="text-xl font-semibold">{data.inference_pipeline.cameras_waiting_for_worker ?? '—'} / {data.inference_pipeline.inference_queue_depth ?? '—'}</div></div>
            <div><div className="text-xs text-slate-500">Estimated full rotation</div><div className="text-xl font-semibold">{data.inference_pipeline.estimated_full_rotation_seconds == null ? '—' : `${Math.ceil(data.inference_pipeline.estimated_full_rotation_seconds / 60)} min`}</div></div>
          </div>
        )}
        <div className="text-xs text-slate-400 mt-3">
          Waiting cameras have fresh video but are not actively being analysed. Rotation is a scheduling estimate, not alert-delivery latency.
        </div>
        {data.inference_pipeline && (
          <div className="mt-4 grid grid-cols-1 gap-3 border-t border-slate-100 pt-3 text-sm dark:border-slate-800 md:grid-cols-2">
            <div>
              <div className="flex items-center justify-between">
                <span className="font-medium">Critical-camera coverage age</span>
                <Badge color={(data.inference_pipeline.critical_cameras_overdue ?? 0) > 0 ? 'red' : 'green'}>
                  {data.inference_pipeline.critical_cameras_overdue ?? '—'} overdue
                </Badge>
              </div>
              <div className="mt-1 text-lg font-semibold">
                {duration(data.inference_pipeline.critical_max_gap_seconds)} / {duration(data.inference_pipeline.critical_gap_sla_seconds)} SLA
              </div>
              <div className="text-xs text-slate-400">Oldest last completed analysis across {data.inference_pipeline.critical_cameras_total} critical cameras.</div>
            </div>
            <div>
              <div className="flex items-center justify-between">
                <span className="font-medium">Standard-camera coverage age</span>
                <Badge color={(data.inference_pipeline.standard_cameras_overdue ?? 0) > 0 ? 'red' : 'green'}>
                  {data.inference_pipeline.standard_cameras_overdue ?? '—'} overdue
                </Badge>
              </div>
              <div className="mt-1 text-lg font-semibold">
                {duration(data.inference_pipeline.standard_max_gap_seconds)} / {duration(data.inference_pipeline.standard_gap_sla_seconds)} SLA
              </div>
              <div className="text-xs text-slate-400">Oldest last completed analysis across {data.inference_pipeline.standard_cameras_total} standard cameras.</div>
            </div>
          </div>
        )}
        <div className="mt-2 text-xs font-medium text-amber-700 dark:text-amber-400">
          Fresh video does not prove continuous AI coverage. Use the measured coverage ages and overdue counts above; alert volume alone is not a health signal.
        </div>
      </Card>

      {data.inference_batch_shadow && (
        <Card className="p-4 mb-4 border-violet-200 dark:bg-slate-900 dark:border-violet-900">
          <div className="flex items-center justify-between mb-3">
            <div className="text-sm font-semibold text-slate-700 dark:text-slate-200">
              GPU Batch Validation
            </div>
            <Badge color={data.inference_batch_shadow.errors > 0 ? 'red' : 'slate'}>
              Shadow only · not alerting
            </Badge>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-6 gap-4 text-sm">
            <div><div className="text-xs text-slate-500">Cameras served</div><div className="text-xl font-semibold">{data.inference_batch_shadow.cameras_served} / {data.inference_batch_shadow.configured_cameras}</div></div>
            <div><div className="text-xs text-slate-500">Batch limit</div><div className="text-xl font-semibold">{data.inference_batch_shadow.batch_size_limit}</div></div>
            <div><div className="text-xs text-slate-500">Frames tested</div><div className="text-xl font-semibold">{data.inference_batch_shadow.frames_processed.toLocaleString()}</div></div>
            <div><div className="text-xs text-slate-500">Per-frame p95</div><div className="text-xl font-semibold">{data.inference_batch_shadow.p95_per_frame_ms == null ? '—' : `${data.inference_batch_shadow.p95_per_frame_ms} ms`}</div></div>
            <div><div className="text-xs text-slate-500">Max schedule wait</div><div className="text-xl font-semibold">{data.inference_batch_shadow.max_camera_schedule_wait_seconds.toFixed(1)}s</div></div>
            <div><div className="text-xs text-slate-500">Errors</div><div className="text-xl font-semibold">{data.inference_batch_shadow.errors}</div></div>
          </div>
          <div className="text-xs text-slate-400 mt-3">
            Detections are measured for capacity only and are not saved, notified or used for training. The existing inference loop remains authoritative.
          </div>
          {capacity && <div className="mt-4 border-t border-violet-100 pt-3 dark:border-violet-900">
            <div className="flex items-center justify-between">
              <div className="text-sm font-medium">Two-hour capacity acceptance</div>
              <Badge color={capacity.capacity_gate_passed ? 'green' : 'amber'}>
                {capacity.status.replace('_', ' ')}
              </Badge>
            </div>
            <div className="mt-2 grid grid-cols-1 gap-1 text-xs md:grid-cols-2">
              {capacity.checks.map(check => (
                <div key={check.name} className={check.passed ? 'text-emerald-700 dark:text-emerald-400' : 'text-amber-700 dark:text-amber-400'}>
                  {check.passed ? '✓' : '○'} {check.name.replaceAll('_', ' ')} · {check.required}
                </div>
              ))}
            </div>
            <div className="mt-2 text-xs font-medium text-amber-700 dark:text-amber-400">
              Capacity passing never proves 99% alert accuracy. {capacity.accuracy_note}
            </div>
          </div>}
        </Card>
      )}

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
