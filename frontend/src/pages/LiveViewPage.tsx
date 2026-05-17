// Live grid: 1×1 / 2×2 / 3×3 / 4×4 layout selector, drag-and-drop tiles,
// per-tile JPEG stream over a WebSocket, AI bbox overlay if a detection
// arrives for that camera within the last 5 seconds.

import { useEffect, useMemo, useRef, useState } from 'react'
import { Badge, Button, Card, PageHeader, Select } from '@/components/ui/Primitives'
import { cameras as camsApi, type Camera } from '@/api/cameras'
import { alerts as alertsApi } from '@/api/alerts'
import { wsUrl } from '@/api/client'

type Detection = { camera_id: number; bbox_norm: number[]; detection_type: string; ts: number }

export default function LiveViewPage() {
  const [cams, setCams] = useState<Camera[]>([])
  const [layout, setLayout] = useState<1 | 2 | 3 | 4>(2)
  const [picked, setPicked] = useState<(number | null)[]>(Array(16).fill(null))
  const [overlays, setOverlays] = useState<Record<number, Detection[]>>({})

  useEffect(() => { camsApi.list().then(setCams) }, [])

  // Auto-fill the picked slots with the first N cameras on first load.
  useEffect(() => {
    setPicked(p => {
      const next = [...p]
      cams.forEach((c, i) => { if (i < next.length && next[i] === null) next[i] = c.id })
      return next
    })
  }, [cams])

  // Subscribe to live alerts → keep last 5s of overlays per camera.
  useEffect(() => {
    const close = alertsApi.subscribe((d: any) => {
      if (!d?.camera_id || !d?.bbox_norm) return
      setOverlays(o => ({
        ...o,
        [d.camera_id]: [...((o[d.camera_id] || []).filter(x => Date.now() - x.ts < 5000)),
                         { camera_id: d.camera_id, bbox_norm: d.bbox_norm,
                           detection_type: d.detection_type, ts: Date.now() }],
      }))
    })
    const tick = setInterval(() => {
      // Trim stale overlays.
      setOverlays(o => Object.fromEntries(Object.entries(o).map(([k, arr]) =>
        [k, (arr as Detection[]).filter(x => Date.now() - x.ts < 5000)])))
    }, 1000)
    return () => { close(); clearInterval(tick) }
  }, [])

  const tileCount = layout * layout
  const visible = picked.slice(0, tileCount)

  return (
    <div className="p-6">
      <PageHeader
        title="Live View"
        actions={<>
          {[1, 2, 3, 4].map(n => (
            <Button key={n} variant={layout === n ? 'primary' : 'ghost'}
                    onClick={() => setLayout(n as 1 | 2 | 3 | 4)}>{n}×{n}</Button>
          ))}
        </>}
      />
      <div className="grid gap-2"
           style={{ gridTemplateColumns: `repeat(${layout}, minmax(0, 1fr))` }}>
        {visible.map((camId, i) => {
          const cam = cams.find(c => c.id === camId)
          return (
            <Card key={i} className="p-2">
              <div className="flex items-center justify-between mb-1">
                <Select className="text-xs"
                        value={camId ?? ''}
                        onChange={(e) => {
                          const v = e.target.value ? Number(e.target.value) : null
                          const next = [...picked]; next[i] = v; setPicked(next)
                        }}>
                  <option value="">— empty —</option>
                  {cams.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
                </Select>
                {cam && <Badge color={cam.status === 'online' ? 'green' : 'amber'}>{cam.status}</Badge>}
              </div>
              {camId ? <Tile cameraId={camId} overlays={overlays[camId] ?? []} /> :
                       <div className="aspect-video bg-slate-200 rounded" />}
            </Card>
          )
        })}
      </div>
    </div>
  )
}

// One live tile — connects to /ws/stream/{cameraId} and paints incoming
// JPEG bytes to a <canvas>; draws bounding box overlays on top.
//
// Resilience added for the 40+ camera fleet:
//   • Auto-reconnect WebSocket on close (exponential backoff up to 30s).
//   • "Connecting…" overlay until the first frame arrives.
//   • After 10s without a frame, polls /system/cameras/{id}/stream-health
//     and shows the actual streamer error (RTSP timeout / 401 / no
//     substream / etc.) instead of a silent black tile.
function Tile({ cameraId, overlays }: { cameraId: number; overlays: Detection[] }) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [size, setSize] = useState({ w: 640, h: 360 })
  const [state, setState] = useState<'connecting' | 'live' | 'stale'>('connecting')
  const [diag, setDiag] = useState<string | null>(null)
  const lastFrameAt = useRef<number>(0)

  useEffect(() => {
    let ws: WebSocket | null = null
    let closed = false
    let retryMs = 1000
    let healthTimer: ReturnType<typeof setInterval> | null = null

    function connect() {
      ws = new WebSocket(wsUrl(`/ws/stream/${cameraId}`))
      ws.binaryType = 'blob'
      ws.onmessage = (e) => {
        lastFrameAt.current = Date.now()
        setState('live'); setDiag(null)
        const blob = e.data as Blob
        const url = URL.createObjectURL(blob)
        const img = new Image()
        img.onload = () => {
          const c = canvasRef.current
          if (!c) { URL.revokeObjectURL(url); return }
          c.width = img.naturalWidth; c.height = img.naturalHeight
          setSize({ w: img.naturalWidth, h: img.naturalHeight })
          c.getContext('2d')!.drawImage(img, 0, 0)
          URL.revokeObjectURL(url)
        }
        img.src = url
        retryMs = 1000  // reset backoff after a real frame
      }
      ws.onclose = () => {
        if (closed) return
        setState(s => s === 'live' ? 'stale' : 'connecting')
        setTimeout(connect, retryMs)
        retryMs = Math.min(30_000, retryMs * 2)
      }
    }
    connect()

    // Stale-frame watchdog: after 10s without a frame, hit the health
    // endpoint so we can surface the actual reason.
    healthTimer = setInterval(async () => {
      const silentMs = Date.now() - lastFrameAt.current
      if (silentMs > 10_000) {
        setState(lastFrameAt.current ? 'stale' : 'connecting')
        try {
          const tok = localStorage.getItem('vg_access_token') ?? ''
          const r = await fetch(`/api/system/cameras/${cameraId}/stream-health`,
                                { headers: { Authorization: `Bearer ${tok}` } })
          if (r.ok) {
            const h = await r.json()
            if (h.error)            setDiag(h.error)
            else if (!h.is_streaming) setDiag('Streamer not yet attempting this camera. Wait ~10s after attaching, or check streamer logs.')
            else                     setDiag('Streamer connected but no frames received yet.')
          }
        } catch { /* ignore */ }
      }
    }, 5000)

    return () => {
      closed = true
      if (healthTimer) clearInterval(healthTimer)
      if (ws) ws.close()
    }
  }, [cameraId])

  return (
    <div className="relative bg-black rounded overflow-hidden aspect-video">
      <canvas ref={canvasRef} className="block w-full h-auto" />
      {/* Status overlay — visible until first frame, then again if stale. */}
      {state !== 'live' && (
        <div className="absolute inset-0 flex flex-col items-center justify-center text-white text-xs gap-2 bg-black/70">
          <div className="font-medium">
            {state === 'connecting' ? 'Connecting…' : 'Stream paused'}
          </div>
          {diag && (
            <div className="max-w-[90%] text-center text-amber-300 leading-snug">{diag}</div>
          )}
        </div>
      )}
      {/* Bounding-box overlay */}
      <div className="absolute inset-0 pointer-events-none">
        {overlays.map((o, i) => {
          const [x1, y1, x2, y2] = o.bbox_norm
          return (
            <div key={i}
                 className="absolute border-2 border-emerald-400"
                 style={{ left: `${x1*100}%`, top: `${y1*100}%`,
                          width: `${(x2-x1)*100}%`, height: `${(y2-y1)*100}%` }}>
              <span className="absolute -top-5 left-0 text-[10px] bg-emerald-600 text-white px-1 rounded">
                {o.detection_type}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
