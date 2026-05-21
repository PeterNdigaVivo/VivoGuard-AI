// Live View — scales to 117+ cameras via pagination + viewport-lazy
// streaming.
//
// Three layers of "only fetch what you can see":
//   1. Store filter dropdown — narrow the working set.
//   2. Page-size cap (default 16) with "Load more" → operators
//      explicitly grow the grid; nothing autoloads everything.
//   3. Per-tile Intersection Observer — even within the picked set,
//      a tile only opens its /ws/stream WebSocket when it's actually
//      in the viewport. Scroll past, the socket closes; scroll back,
//      it reconnects. Browsers cap ~250 WS/origin and at 117 cameras
//      we'd burn through that on a 9×9 grid.

import { useEffect, useMemo, useRef, useState } from 'react'
import { Badge, Button, Card, PageHeader, Select, useToast } from '@/components/ui/Primitives'
import { cameras as camsApi, type Camera } from '@/api/cameras'
import { stores as storesApi, type Store } from '@/api/stores'
import { alerts as alertsApi } from '@/api/alerts'
import { api, wsUrl } from '@/api/client'

type Detection = { camera_id: number; bbox_norm: number[]; detection_type: string; ts: number }
type GridSize = 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9
const GRID_OPTIONS: GridSize[] = [1, 2, 3, 4, 5, 6, 7, 8, 9]
const PAGE_INCREMENT = 16   // 4×4

export default function LiveViewPage() {
  const [cams, setCams]       = useState<Camera[]>([])
  const [stores, setStores]   = useState<Store[]>([])
  const [storeFilter, setStoreFilter] = useState<number | ''>('')
  const [layout, setLayout]   = useState<GridSize>(4)         // start 4×4
  const [visibleCount, setVisibleCount] = useState(PAGE_INCREMENT)
  const [overlays, setOverlays] = useState<Record<number, Detection[]>>({})
  const [fixing, setFixing]   = useState(false)
  const toast = useToast()

  useEffect(() => { camsApi.list().then(setCams) }, [])
  useEffect(() => { storesApi.list().then(setStores) }, [])

  // Filtered + paginated working set. The grid renders the first
  // `tileCount` of these.
  const filteredCams = useMemo(() => {
    if (storeFilter === '') return cams
    return cams.filter(c => c.store_id === storeFilter)
  }, [cams, storeFilter])

  // When the filter changes, reset to the first page so the operator
  // doesn't get stuck mid-list with no visible tiles.
  useEffect(() => { setVisibleCount(PAGE_INCREMENT) }, [storeFilter])

  // Subscribe to /ws/alerts for live bbox overlays (this is a single
  // WS, not per-camera, so no pagination concerns).
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

  async function autoFixAll() {
    if (!confirm('Probe every camera and switch unreachable RTSP ones to HTTP snapshot polling?')) return
    setFixing(true)
    try {
      const res = await api<{ checked: number; switched: number; report: any[] }>(
        '/cameras/auto-failover', { method: 'POST', body: {} },
      )
      toast.push(`Checked ${res.checked} cameras, switched ${res.switched} to HTTP snapshot.`)
      camsApi.list().then(setCams)
    } catch (e) {
      toast.push(`Auto-fix failed: ${e}`, 'err')
    } finally {
      setFixing(false)
    }
  }

  const tileCount = Math.min(visibleCount, filteredCams.length)
  const visible = filteredCams.slice(0, tileCount)
  const hasMore = filteredCams.length > tileCount

  return (
    <div className="p-6">
      <PageHeader
        title="Live View"
        actions={<>
          <Select value={storeFilter}
                  onChange={e => setStoreFilter(e.target.value ? Number(e.target.value) : '')}>
            <option value="">All stores ({cams.length} cams)</option>
            {stores.map(s => {
              const n = cams.filter(c => c.store_id === s.id).length
              return <option key={s.id} value={s.id}>{s.name} ({n})</option>
            })}
          </Select>
          <Button variant="ghost" onClick={autoFixAll} disabled={fixing}>
            {fixing ? 'Probing…' : '🛠 Auto-fix offline'}
          </Button>
          {GRID_OPTIONS.map(n => (
            <Button key={n} variant={layout === n ? 'primary' : 'ghost'}
                    onClick={() => setLayout(n)}>{n}×{n}</Button>
          ))}
        </>}
      />

      <div className="text-xs text-slate-500 mb-3 flex items-center gap-3">
        Showing <strong>{tileCount}</strong> of <strong>{filteredCams.length}</strong> cameras
        {storeFilter !== '' && (
          <button onClick={() => setStoreFilter('')}
                  className="text-sky-600 hover:underline">clear filter</button>
        )}
        {layout >= 6 && (
          <span className="text-amber-700">
            · Tiles open WebSockets only when scrolled into view — browser cap is ~250.
          </span>
        )}
      </div>

      <div className="grid gap-1"
           style={{ gridTemplateColumns: `repeat(${layout}, minmax(0, 1fr))` }}>
        {visible.map(cam => (
          <Card key={cam.id} className="p-1">
            <div className="flex items-center justify-between mb-1 text-xs">
              <span className="truncate" title={cam.name}>{cam.name}</span>
              <Badge color={cam.status === 'online' ? 'green' : 'amber'}>{cam.status}</Badge>
            </div>
            <LazyTile cameraId={cam.id} overlays={overlays[cam.id] ?? []} />
          </Card>
        ))}
      </div>

      {hasMore && (
        <div className="mt-4 text-center">
          <Button onClick={() => setVisibleCount(n => n + PAGE_INCREMENT)}>
            Load {Math.min(PAGE_INCREMENT, filteredCams.length - tileCount)} more
          </Button>
          <div className="text-xs text-slate-400 mt-1">
            {filteredCams.length - tileCount} cameras hidden
          </div>
        </div>
      )}
      {filteredCams.length === 0 && (
        <Card className="p-8 text-center text-slate-500">
          {storeFilter !== '' ? 'No cameras attached to this store.' : 'No cameras yet.'}
        </Card>
      )}
    </div>
  )
}


// ---------------------------------------------------------------------------
// LazyTile — wraps Tile with Intersection Observer so the WebSocket
// only connects when the tile is in the viewport. Below the viewport
// (or scrolled out): no WS, no frames, no resources.

function LazyTile({ cameraId, overlays }: { cameraId: number; overlays: Detection[] }) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const [visible, setVisible] = useState(false)
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    // rootMargin pre-loads tiles ~200px before they enter the viewport
    // so scrolling feels instant — no blank flicker.
    const io = new IntersectionObserver(entries => {
      for (const e of entries) {
        if (e.isIntersecting) setVisible(true)
      }
    }, { rootMargin: '200px 0px' })
    io.observe(el)
    return () => io.disconnect()
  }, [])
  return (
    <div ref={wrapRef} className="relative">
      {visible ? <Tile cameraId={cameraId} overlays={overlays} /> :
                 <div className="aspect-video bg-slate-100 rounded flex items-center justify-center text-slate-400 text-xs">
                   ⌛ Tile loads when scrolled into view
                 </div>}
    </div>
  )
}


// ---------------------------------------------------------------------------
// One live tile — connects to /ws/stream/{cameraId} and paints incoming
// JPEG bytes to a <canvas>; draws bounding box overlays on top.
//
// Resilience for the 40+ camera fleet:
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

  const [portProbe, setPortProbe] = useState<{ trying: number | null; result: any } | null>(null)
  async function tryAlternatePorts() {
    setPortProbe({ trying: null, result: null })
    try {
      const tok = localStorage.getItem('vg_access_token') ?? ''
      const r = await fetch(
        `/api/cameras/${cameraId}/try-alternate-ports`,
        { method: 'POST', headers: { Authorization: `Bearer ${tok}` } },
      )
      const data = await r.json()
      setPortProbe({ trying: null, result: data })
    } catch (e) {
      setPortProbe({ trying: null, result: { ok: false, summary: String(e) } })
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
        retryMs = 1000
      }
      ws.onclose = () => {
        if (closed) return
        setState(s => s === 'live' ? 'stale' : 'connecting')
        setTimeout(connect, retryMs)
        retryMs = Math.min(30_000, retryMs * 2)
      }
    }
    connect()

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
      {state !== 'live' && (
        <div className="absolute inset-0 flex flex-col items-center justify-center text-white text-xs gap-2 bg-black/70 p-2">
          <div className="font-medium">
            {state === 'connecting' ? 'Connecting…' : 'Stream paused'}
          </div>
          {diag && (
            <div className="max-w-[90%] text-center text-amber-300 leading-snug">{diag}</div>
          )}
          {state === 'stale' && (
            <div className="flex gap-1">
              <button
                className="mt-1 px-2 py-1 rounded bg-sky-500 hover:bg-sky-400 text-white text-[11px] font-medium"
                onClick={(e) => { e.stopPropagation(); tryAlternatePorts() }}
                disabled={portProbe?.trying !== undefined && portProbe?.trying !== null}>
                {portProbe && !portProbe.result ? 'Trying ports…' : '🔄 Try alternate ports'}
              </button>
              <button
                className="mt-1 px-2 py-1 rounded bg-amber-500 hover:bg-amber-400 text-black text-[11px] font-medium"
                onClick={(e) => { e.stopPropagation(); openDiagnose(true) }}
                disabled={diagBusy}>
                {diagBusy ? 'Probing…' : '🛠 Diagnose'}
              </button>
            </div>
          )}
          {portProbe?.result && (
            <div className={'text-[11px] mt-1 text-center max-w-[90%] ' +
              (portProbe.result.ok ? 'text-emerald-300' : 'text-red-300')}>
              {portProbe.result.summary}
            </div>
          )}
        </div>
      )}

      {diagOpen && diagReport && (
        <div className="absolute inset-0 bg-black/90 text-white p-3 overflow-auto text-[11px] z-10"
             onClick={(e) => e.stopPropagation()}>
          <div className="flex justify-between items-center mb-2">
            <div className="font-medium">{diagReport.name} — transport probe</div>
            <button className="text-slate-300 hover:text-white"
                    onClick={() => setDiagOpen(false)}>✕</button>
          </div>
          <div className="text-amber-300 mb-2">{diagReport.explanation}</div>
          <button
            className="mt-3 px-2 py-1 rounded bg-sky-600 hover:bg-sky-500"
            onClick={() => openDiagnose(true)}
            disabled={diagBusy}>
            {diagBusy ? 'Probing…' : 'Re-probe now'}
          </button>
        </div>
      )}

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
      <div className="absolute top-1 right-1 text-[10px] text-white/60">
        {size.w}×{size.h}
      </div>
    </div>
  )
}
