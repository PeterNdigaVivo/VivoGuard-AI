// Live grid: 1×1 … 9×9 layouts (up to 81 tiles). For large fleets only
// the visible tiles open WebSockets — browsers cap ~250 concurrent WS
// per origin.

import { useEffect, useMemo, useRef, useState } from 'react'
import { Badge, Button, Card, PageHeader, Select } from '@/components/ui/Primitives'
import { cameras as camsApi, type Camera } from '@/api/cameras'
import { alerts as alertsApi } from '@/api/alerts'
import { wsUrl } from '@/api/client'

type Detection = { camera_id: number; bbox_norm: number[]; detection_type: string; ts: number }
type GridSize = 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9
const GRID_OPTIONS: GridSize[] = [1, 2, 3, 4, 5, 6, 7, 8, 9]

export default function LiveViewPage() {
  const [cams, setCams] = useState<Camera[]>([])
  const [layout, setLayout] = useState<GridSize>(2)
  // Up to 81 tiles (9×9).
  const [picked, setPicked] = useState<(number | null)[]>(Array(81).fill(null))
  const [overlays, setOverlays] = useState<Record<number, Detection[]>>({})

  useEffect(() => { camsApi.list().then(setCams) }, [])

  useEffect(() => {
    setPicked(p => {
      const next = [...p]
      cams.forEach((c, i) => { if (i < next.length && next[i] === null) next[i] = c.id })
      return next
    })
  }, [cams])

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
          {GRID_OPTIONS.map(n => (
            <Button key={n} variant={layout === n ? 'primary' : 'ghost'}
                    onClick={() => setLayout(n)}>{n}×{n}</Button>
          ))}
        </>}
      />
      {layout >= 6 && (
        <div className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded px-3 py-2 mb-3">
          Showing {tileCount} tiles. Browsers cap concurrent WebSockets per origin
          (~250 in Chrome); each tile holds one. Empty slots don't open a WS.
        </div>
      )}
      <div className="grid gap-1"
           style={{ gridTemplateColumns: `repeat(${layout}, minmax(0, 1fr))` }}>
        {visible.map((camId, i) => {
          const cam = cams.find(c => c.id === camId)
          return (
            <Card key={i} className="p-1">
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
  const [diagOpen, setDiagOpen] = useState(false)
  const [diagReport, setDiagReport] = useState<any>(null)
  const [diagBusy, setDiagBusy] = useState(false)
  const lastFrameAt = useRef<number>(0)

  async function openDiagnose(fresh: boolean) {
    setDiagBusy(true)
    try {
      const tok = localStorage.getItem('vg_access_token') ?? ''
      const r = await fetch(
        `/api/cameras/${cameraId}/transport-diagnose?fresh=${fresh}`,
        { headers: { Authorization: `Bearer ${tok}` } },
      )
      if (r.ok) setDiagReport(await r.json())
    } finally {
      setDiagBusy(false)
      setDiagOpen(true)
    }
  }

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
        <div className="absolute inset-0 flex flex-col items-center justify-center text-white text-xs gap-2 bg-black/70 p-2">
          <div className="font-medium">
            {state === 'connecting' ? 'Connecting…' : 'Stream paused'}
          </div>
          {diag && (
            <div className="max-w-[90%] text-center text-amber-300 leading-snug">{diag}</div>
          )}
          {/* Diagnose: surfaces auto-failover's per-port probe results
              so operators see exactly which ports were tried and why
              each failed. Shown only on stale tiles to keep the
              'Connecting…' first-load view clean. */}
          {state === 'stale' && (
            <button
              className="mt-1 px-2 py-1 rounded bg-amber-500 hover:bg-amber-400 text-black text-[11px] font-medium"
              onClick={(e) => { e.stopPropagation(); openDiagnose(true) }}
              disabled={diagBusy}>
              {diagBusy ? 'Probing…' : '🛠 Diagnose & auto-fix'}
            </button>
          )}
        </div>
      )}

      {/* Diagnostic modal — full transport-probe report. */}
      {diagOpen && diagReport && (
        <div className="absolute inset-0 bg-black/90 text-white p-3 overflow-auto text-[11px] z-10"
             onClick={(e) => e.stopPropagation()}>
          <div className="flex justify-between items-center mb-2">
            <div className="font-medium">{diagReport.name} — transport probe</div>
            <button className="text-slate-300 hover:text-white"
                    onClick={() => setDiagOpen(false)}>✕</button>
          </div>
          <div className="text-amber-300 mb-2">{diagReport.explanation}</div>
          <div className="font-mono text-slate-400 leading-tight">
            host: {diagReport.host}<br/>
            rtsp_port: {diagReport.rtsp_port} · http_port: {diagReport.http_port}<br/>
            transport now: <b className="text-white">{diagReport.transport}</b>
          </div>
          {diagReport.diagnostic && (
            <div className="mt-2">
              <div className="text-slate-400">RTSP reachable: {String(diagReport.diagnostic.rtsp_reachable)}</div>
              <div className="text-slate-400 mt-1">HTTP attempts:</div>
              {(diagReport.diagnostic.http_attempts || []).map((a: any, i: number) => (
                <div key={i} className="ml-2 mt-1">
                  <div>
                    port {a.port}: TCP {a.tcp ? '✓' : '✗'}
                  </div>
                  {(a.templates || []).map((t: any, j: number) => (
                    <div key={j} className={'ml-3 ' + (t.ok ? 'text-emerald-400' : 'text-slate-500')}>
                      {t.ok ? '✓' : '✗'} {t.vendor}: {t.reason}
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}
          <button
            className="mt-3 px-2 py-1 rounded bg-sky-600 hover:bg-sky-500"
            onClick={() => openDiagnose(true)}
            disabled={diagBusy}>
            {diagBusy ? 'Probing…' : 'Re-probe now'}
          </button>
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
