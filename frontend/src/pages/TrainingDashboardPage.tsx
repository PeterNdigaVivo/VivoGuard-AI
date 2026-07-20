// Training dashboard — live progress for a single training job.
// Polls /training/jobs/{id}/status every 2s and tails the WebSocket
// at /ws/training/{id} for per-epoch metric pushes.

import { useEffect, useRef, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { Badge, Card, PageHeader } from '@/components/ui/Primitives'
import { training, type TrainingJob } from '@/api/training'
import { wsUrl } from '@/api/client'

interface MetricPoint { epoch: number; mAP50?: number; loss?: number }

export default function TrainingDashboardPage() {
  const { jobId } = useParams()
  const id = Number(jobId)
  const [job, setJob] = useState<TrainingJob | null>(null)
  const [metrics, setMetrics] = useState<MetricPoint[]>([])
  const [logs, setLogs] = useState<string[]>([])
  const wsRef = useRef<WebSocket | null>(null)

  // Polling for job state.
  useEffect(() => {
    let cancelled = false
    const tick = async () => {
      try {
        const j = await training.jobStatus(id)
        if (!cancelled) setJob(j)
      } catch {/* ignore */}
    }
    tick()
    const t = setInterval(tick, 2000)
    return () => { cancelled = true; clearInterval(t) }
  }, [id])

  // WebSocket for the live training log.
  useEffect(() => {
    const ws = new WebSocket(wsUrl(`/ws/training/${id}`))
    wsRef.current = ws
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)
        if (msg.epoch !== undefined) {
          const m: MetricPoint = {
            epoch: msg.epoch,
            mAP50: msg.metrics?.['metrics/mAP50(B)'],
            loss:  msg.metrics?.['train/box_loss'],
          }
          setMetrics(arr => [...arr, m])
        }
        setLogs(l => [...l.slice(-100), JSON.stringify(msg)])
      } catch {
        setLogs(l => [...l.slice(-100), e.data])
      }
    }
    return () => ws.close()
  }, [id])

  const status = job?.status ?? 'loading…'
  const pct = job ? Math.round((job.current_epoch / Math.max(1, job.total_epochs)) * 100) : 0

  return (
    <div className="p-6">
      <PageHeader
        title={`Training Job #${id}`}
        actions={<Link to="/training" className="text-sky-600 hover:underline">Back to studio</Link>}
      />

      <Card className="p-4 mb-4">
        <div className="flex items-center gap-4 mb-3">
          <Badge color={
            status === 'done' ? 'green' :
            status === 'failed' ? 'red' :
            status === 'running' ? 'sky' : 'amber'
          }>{status}</Badge>
          <div className="text-sm">
            Epoch {job?.current_epoch ?? 0} / {job?.total_epochs ?? 0}
          </div>
          <div className="text-sm text-slate-500 dark:text-slate-300">
            best mAP@50: {job?.best_map50?.toFixed(3) ?? '—'}
          </div>
        </div>
        <div className="h-2 bg-slate-200 rounded">
          <div className="h-2 bg-sky-600 rounded" style={{ width: `${pct}%` }} />
        </div>
        {job?.error_message && (
          <div className="text-red-600 text-sm mt-2">{job.error_message}</div>
        )}
      </Card>

      {/* Tiny inline charts (no external chart lib — pure SVG). */}
      <div className="grid grid-cols-2 gap-4 mb-4">
        <Card className="p-3">
          <div className="text-sm font-medium mb-2">mAP@50 over epochs</div>
          <Spark points={metrics.map(m => m.mAP50)} color="#0284c7" />
        </Card>
        <Card className="p-3">
          <div className="text-sm font-medium mb-2">Box loss over epochs</div>
          <Spark points={metrics.map(m => m.loss)} color="#dc2626" />
        </Card>
      </div>

      <Card className="p-3">
        <div className="text-sm font-medium mb-2">Training log</div>
        <pre className="bg-slate-900 text-slate-100 text-xs p-3 rounded h-64 overflow-auto whitespace-pre-wrap">
          {logs.length ? logs.join('\n') : '(no log lines yet)'}
        </pre>
      </Card>
    </div>
  )
}

function Spark({ points, color }: { points: Array<number | undefined>; color: string }) {
  const ps = points.filter((p): p is number => typeof p === 'number')
  if (ps.length < 2) return <div className="text-slate-400 dark:text-slate-400 text-sm">Not enough data yet.</div>
  const max = Math.max(...ps), min = Math.min(...ps)
  const W = 320, H = 80
  const path = ps.map((y, i) => {
    const x = (i / (ps.length - 1)) * W
    const yy = H - ((y - min) / Math.max(1e-6, max - min)) * H
    return `${i === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${yy.toFixed(1)}`
  }).join(' ')
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-24">
      <path d={path} stroke={color} strokeWidth={2} fill="none" />
    </svg>
  )
}
