// Live View — true paged streaming. Only the active page's tiles
// ever exist in the DOM, so the WebSocket count is bounded by
// `layout × layout`. When the operator pages forward, the old
// tiles unmount (their useEffect cleanup closes their /ws/stream
// socket) and the new tiles mount fresh.
//
// Three features built on top of that:
//   • Store filter — narrow the working set to one store's cams.
//   • Auto-cycle — security-guard mode that pages every 30s.
//   • Full-screen — click any tile to expand; Esc returns.
//
// The previous "Load more" behaviour grew the visible set
// monotonically, which meant after a few clicks you had 48-64
// WebSockets open. At 117 cameras that crushed both browser and
// server. True paging caps it at 16.

import { useEffect, useMemo, useRef, useState } from 'react'
import { Badge, Button, Card, PageHeader, Select, useToast } from '@/components/ui/Primitives'
import { cameras as camsApi, type Camera } from '@/api/cameras'
import { stores as storesApi, type Store } from '@/api/stores'
import { alerts as alertsApi } from '@/api/alerts'
import { api, wsUrl } from '@/api/client'

type LiveViewMode = 'cameras' | 'activity'
const ACTIVITY_REFRESH_MS = 30_000     // spec: 30 s minimum

type Detection = { camera_id: number; bbox_norm: number[]; detection_type: string; ts: number }
type GridSize = 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9
const GRID_OPTIONS: GridSize[] = [1, 2, 3, 4, 5, 6, 7, 8, 9]
const AUTO_CYCLE_MS = 30_000           // 30s — security-guard cadence
const PAGE_STORAGE_KEY = 'vg:live:pageStart'
const FILTER_STORAGE_KEY = 'vg:live:storeFilter'

export default function LiveViewPage() {
  const [cams, setCams]       = useState<Camera[]>([])
  const [stores, setStores]   = useState<Store[]>([])
  // Persist filter + page so navigating away and back leaves the
  // operator looking at the same tiles they had open.
  const [storeFilter, setStoreFilter] = useState<number | ''>(
    () => {
      const raw = (typeof localStorage !== 'undefined' && localStorage.getItem(FILTER_STORAGE_KEY)) || ''
      return raw ? Number(raw) : ''
    }
  )
  const [layout, setLayout]   = useState<GridSize>(4)         // 4×4 = 16 tiles
  const [pageStart, setPageStart] = useState<number>(
    () => {
      const raw = (typeof localStorage !== 'undefined' && localStorage.getItem(PAGE_STORAGE_KEY)) || '0'
      const n = Number(raw)
      return Number.isFinite(n) && n >= 0 ? n : 0
    }
  )
  const [overlays, setOverlays] = useState<Record<number, Detection[]>>({})
  const [fixing, setFixing]   = useState(false)
  const [autoCycle, setAutoCycle] = useState(false)
  // null = grid view; number = camera_id rendered full-screen.
  const [fullscreenId, setFullscreenId] = useState<number | null>(null)
  // Camera grid (default) vs Activity grid (top-15 most-active cams).
  const [viewMode, setViewMode] = useState<LiveViewMode>('cameras')
  const toast = useToast()

  useEffect(() => { camsApi.list().then(setCams) }, [])
  useEffect(() => { storesApi.list().then(setStores) }, [])

  // Persist on change.
  useEffect(() => {
    try { localStorage.setItem(PAGE_STORAGE_KEY, String(pageStart)) } catch {}
  }, [pageStart])
  useEffect(() => {
    try { localStorage.setItem(FILTER_STORAGE_KEY, storeFilter === '' ? '' : String(storeFilter)) } catch {}
  }, [storeFilter])

  // Filtered working set.
  const filteredCams = useMemo(() => {
    if (storeFilter === '') return cams
    return cams.filter(c => c.store_id === storeFilter)
  }, [cams, storeFilter])

  const pageSize = layout * layout       // 4×4 = 16, 3×3 = 9, etc.
  const total = filteredCams.length
  // Clamp pageStart to a valid page boundary whenever the filter or
  // page size changes (e.g. switching from 4×4 to 2×2 with 32 cams
  // on page 2 → snap to the nearest page boundary).
  const safePageStart = Math.max(
    0,
    Math.min(Math.floor(pageStart / pageSize) * pageSize,
             Math.max(0, total - 1) - ((total - 1) % pageSize)),
  )
  useEffect(() => {
    if (safePageStart !== pageStart) setPageStart(safePageStart)
  }, [safePageStart, pageStart])
  // Reset to first page when filter changes.
  useEffect(() => { setPageStart(0) }, [storeFilter])

  const pageEnd = Math.min(safePageStart + pageSize, total)
  const visible = filteredCams.slice(safePageStart, pageEnd)
  const hasPrev = safePageStart > 0
  const hasNext = pageEnd < total
  const pageNumber = Math.floor(safePageStart / pageSize) + 1
  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  function goPrev() { setPageStart(s => Math.max(0, s - pageSize)) }
  function goNext() {
    setPageStart(s => {
      const nxt = s + pageSize
      if (nxt >= total) return 0          // wrap to start
      return nxt
    })
  }

  // Auto-cycle: every AUTO_CYCLE_MS, advance one page. Wraps to 0
  // when past the last page. Disabled by default.
  useEffect(() => {
    if (!autoCycle) return
    const id = setInterval(() => goNext(), AUTO_CYCLE_MS)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoCycle, total, pageSize])

  // Esc → close fullscreen.
  useEffect(() => {
    if (fullscreenId === null) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setFullscreenId(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [fullscreenId])

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

  // Build a quick lookup so the fullscreen renderer knows the camera
  // it's expanding without re-searching the array.
  const fullscreenCam = fullscreenId !== null
    ? cams.find(c => c.id === fullscreenId) ?? null
    : null

  return (
    <div className="p-6">
      <PageHeader
        title="Live View"
        actions={<>
          {/* View toggle — Activity reuses the cached snapshot
              endpoint and refreshes once every 30 s, so flipping
              between modes doesn't open or tear down any live
              WebSockets beyond what each view already does. */}
          <Button variant={viewMode === 'cameras' ? 'primary' : 'ghost'}
                  onClick={() => setViewMode('cameras')}>
            📡 All Cameras
          </Button>
          {/* Live Activity toggle — distinct orange / white / bold so
              it reads as the "spotlight on what's happening now"
              control, not just another grid mode. Darker orange when
              selected. Inline button because the shared Button
              primitive only ships primary/ghost/danger variants. */}
          <button onClick={() => setViewMode('activity')}
                  className={
                    'px-3 py-1.5 rounded text-sm font-bold text-white ' +
                    (viewMode === 'activity'
                      ? 'bg-orange-600 hover:bg-orange-700'
                      : 'bg-orange-500 hover:bg-orange-600')}>
            🔴 Live Activity
          </button>
          {viewMode === 'cameras' && (
            <Select value={storeFilter}
                    onChange={e => setStoreFilter(e.target.value ? Number(e.target.value) : '')}>
              <option value="">All stores ({cams.length} cams)</option>
              {stores.map(s => {
                const n = cams.filter(c => c.store_id === s.id).length
                return <option key={s.id} value={s.id}>{s.name} ({n})</option>
              })}
            </Select>
          )}
          {viewMode === 'cameras' && (
            <Button variant="ghost" onClick={autoFixAll} disabled={fixing}>
              {fixing ? 'Probing…' : '🛠 Auto-fix offline'}
            </Button>
          )}
          {viewMode === 'cameras' && GRID_OPTIONS.map(n => (
            <Button key={n} variant={layout === n ? 'primary' : 'ghost'}
                    onClick={() => setLayout(n)}>{n}×{n}</Button>
          ))}
        </>}
      />

      {viewMode === 'activity' ? (
        <ActivityGrid onPickFullscreen={setFullscreenId} />
      ) : (
      <>
      {/* Page navigation + auto-cycle + camera count */}
      <Card className="p-3 mb-3 flex flex-wrap items-center gap-3 text-sm">
        <Button variant="ghost" onClick={goPrev} disabled={!hasPrev}>
          ← Previous {pageSize}
        </Button>
        <Button variant="ghost" onClick={goNext} disabled={total === 0}>
          {hasNext ? `Next ${Math.min(pageSize, total - pageEnd)} →` : 'Wrap to start →'}
        </Button>
        <div className="text-slate-700">
          Cameras{' '}
          <strong>{total === 0 ? 0 : safePageStart + 1}–{pageEnd}</strong>{' '}
          of <strong>{total}</strong>
          {total !== cams.length && <span className="text-slate-500"> (filtered from {cams.length})</span>}
          <span className="text-slate-400"> · page {pageNumber}/{totalPages}</span>
        </div>
        <label className="ml-auto flex items-center gap-1 text-xs text-slate-700">
          <input type="checkbox" checked={autoCycle}
                 onChange={e => setAutoCycle(e.target.checked)} />
          🔄 Auto-cycle every 30s
        </label>
        {storeFilter !== '' && (
          <button onClick={() => setStoreFilter('')}
                  className="text-sky-600 hover:underline text-xs">clear filter</button>
        )}
      </Card>

      <div className="grid gap-1"
           style={{ gridTemplateColumns: `repeat(${layout}, minmax(0, 1fr))` }}>
        {visible.map(cam => (
          <Card key={cam.id} className="p-1">
            <div className="flex items-center justify-between mb-1 text-xs">
              <button onClick={() => setFullscreenId(cam.id)}
                      className="truncate hover:underline text-left flex-1 text-sky-700"
                      title="Click to view full screen">
                {cam.name}
              </button>
              <Badge color={cam.status === 'online' ? 'green' : 'amber'}>{cam.status}</Badge>
            </div>
            <div onClick={() => setFullscreenId(cam.id)} className="cursor-zoom-in">
              <Tile cameraId={cam.id} overlays={overlays[cam.id] ?? []} />
            </div>
          </Card>
        ))}
      </div>

      {total === 0 && (
        <Card className="p-8 text-center text-slate-500">
          {storeFilter !== '' ? 'No cameras attached to this store.' : 'No cameras yet.'}
        </Card>
      )}
      </>
      )}

      {/* Fullscreen overlay — renders ONE tile expanded. Esc closes. */}
      {fullscreenCam && (
        <div className="fixed inset-0 bg-black z-50 flex flex-col"
             onClick={() => setFullscreenId(null)}>
          <div className="flex justify-between items-center p-3 text-white">
            <div className="font-medium">{fullscreenCam.name}</div>
            <div className="text-xs text-slate-400">Press <kbd className="bg-slate-700 px-1 rounded">Esc</kbd> or click anywhere to return</div>
          </div>
          <div className="flex-1 flex items-center justify-center p-4"
               onClick={e => e.stopPropagation()}>
            <div className="w-full h-full max-w-[1600px]">
              <Tile cameraId={fullscreenCam.id} overlays={overlays[fullscreenCam.id] ?? []} />
            </div>
          </div>
        </div>
      )}
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
    // Lifecycle guard for the whole effect: when the tile unmounts
    // (page changed, layout changed, navigated away from Live View)
    // every async branch — pending reconnect timer, in-flight health
    // fetch, queued WebSocket events — has to see this flip and
    // bail. Without it the reconnect setTimeout would fire AFTER
    // cleanup and silently open a new WebSocket on an off-screen
    // camera, which is exactly the CPU leak we're trying to kill.
    let aborted = false
    let ws: WebSocket | null = null
    let retryMs = 1000
    let healthTimer: ReturnType<typeof setInterval> | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    const healthAbort = new AbortController()

    function connect() {
      if (aborted) return
      ws = new WebSocket(wsUrl(`/ws/stream/${cameraId}`))
      ws.binaryType = 'blob'
      ws.onmessage = (e) => {
        if (aborted) return
        lastFrameAt.current = Date.now()
        setState('live'); setDiag(null)
        const blob = e.data as Blob
        const url = URL.createObjectURL(blob)
        const img = new Image()
        img.onload = () => {
          if (aborted) { URL.revokeObjectURL(url); return }
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
        if (aborted) return
        setState(s => s === 'live' ? 'stale' : 'connecting')
        reconnectTimer = setTimeout(connect, retryMs)
        retryMs = Math.min(30_000, retryMs * 2)
      }
    }
    connect()

    healthTimer = setInterval(async () => {
      if (aborted) return
      const silentMs = Date.now() - lastFrameAt.current
      if (silentMs > 10_000) {
        setState(lastFrameAt.current ? 'stale' : 'connecting')
        try {
          const tok = localStorage.getItem('vg_access_token') ?? ''
          const r = await fetch(`/api/system/cameras/${cameraId}/stream-health`,
                                { headers: { Authorization: `Bearer ${tok}` },
                                  signal: healthAbort.signal })
          if (aborted) return
          if (r.ok) {
            const h = await r.json()
            if (aborted) return
            if (h.error)            setDiag(h.error)
            else if (!h.is_streaming) setDiag('Streamer not yet attempting this camera. Wait ~10s after attaching, or check streamer logs.')
            else                     setDiag('Streamer connected but no frames received yet.')
          }
        } catch { /* abort or transient — ignore */ }
      }
    }, 5000)

    return () => {
      // Order matters: flip the guard first so any in-flight onmessage
      // / onclose / setTimeout / setInterval callback short-circuits
      // before it touches state or opens a fresh socket.
      aborted = true
      if (reconnectTimer) clearTimeout(reconnectTimer)
      if (healthTimer)    clearInterval(healthTimer)
      healthAbort.abort()
      if (ws) {
        // Drop handlers so the close event can't trigger a reconnect
        // race even if the browser fires it after `aborted` was set.
        ws.onmessage = null
        ws.onclose   = null
        try { ws.close() } catch { /* ignore */ }
      }
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


// ---------------------------------------------------------------
// Activity grid — top 15 cameras by recent detection_event count
// ---------------------------------------------------------------

interface ActivityCamera {
  camera_id: number
  camera_name: string
  store_id: number | null
  store_name: string | null
  status: string
  // Raw people count seen RIGHT NOW (this frame).
  people: number
  // Ranking score — currently == people; kept as its own field so
  // future tweaks (motion energy, dwell weighting) don't require a
  // breaking change to the API shape.
  score: number
  // Epoch seconds of the most recent activity write in Redis.
  last_activity_at: number
}

const SLOT_HOLD_MS = 90 * 1000   // 90-second minimum hold per tile
const MIN_HOLD_BEFORE_EVICT = 30 * 1000  // idle tiles (people=0, score=0)
                                          // can be evicted after 30 s if a
                                          // busier camera is waiting
const REPLACE_RATIO = 2              // candidate must be 2× the held tile's score

function ActivityGrid({ onPickFullscreen }: {
  onPickFullscreen: (id: number) => void
}) {
  // Most recent fetch (top 15 by current score per backend).
  const [latest, setLatest] = useState<ActivityCamera[] | null>(null)
  // Currently-displayed slots, in order. Each entry is the camera id
  // and the moment it entered its slot — drives the hold timer +
  // the "X:YY" countdown label.
  type Slot = { camera_id: number; entered_at: number }
  const [slots, setSlots] = useState<Slot[]>([])
  const [lastAt, setLastAt] = useState<number | null>(null)
  const [err, setErr] = useState<string | null>(null)
  // Tick once a second so the per-tile countdown / "Updated Ns ago"
  // pills stay live between fetches without another network call.
  const [, forceTick] = useState(0)

  // Latest-known cam metadata + score, keyed by camera_id. Slots
  // dereference this to render — so a held slot whose camera dropped
  // off the top-15 list keeps its label + (last known) score.
  const known = useMemo(() => {
    const map = new Map<number, ActivityCamera>()
    for (const c of latest ?? []) map.set(c.camera_id, c)
    return map
  }, [latest])

  useEffect(() => {
    let alive = true
    const tick = () => {
      api<{ cameras: ActivityCamera[]; as_of: number; source: string }>(
        '/cameras/activity/live?limit=15')
        .then(d => {
          if (!alive) return
          setLatest(d.cameras ?? [])
          setLastAt(Date.now()); setErr(null)
        })
        .catch(e => { if (alive) setErr(String(e)) })
    }
    tick()
    const refresh = setInterval(tick, ACTIVITY_REFRESH_MS)
    const seconds = setInterval(() => forceTick(n => n + 1), 1000)
    return () => { alive = false; clearInterval(refresh); clearInterval(seconds) }
  }, [])

  // Slot management — apply the 5-min hold + 2× replacement rule.
  useEffect(() => {
    if (!latest) return
    setSlots(prev => {
      const now = Date.now()
      const inLatest = new Set(latest.map(c => c.camera_id))
      const heldIds  = new Set(prev.map(s => s.camera_id))

      // First load: take the top 15 as-is.
      if (prev.length === 0) {
        return latest.slice(0, 15).map(c => ({ camera_id: c.camera_id, entered_at: now }))
      }

      // Build the candidate pool — cameras in latest that aren't
      // already held, sorted by current score desc.
      const candidates = latest
        .filter(c => !heldIds.has(c.camera_id))
        .sort((a, b) => b.score - a.score)
      let nextSlots: Slot[] = [...prev]

      // Annotate each held slot with its current people / score so
      // both eviction tiers below can read them without re-walking
      // the known map.
      const annotated = nextSlots.map((s, i) => {
        const cam = known.get(s.camera_id)
        return {
          s, i,
          score:    cam?.score  ?? 0,
          people:   cam?.people ?? 0,
          ageMs:    now - s.entered_at,
        }
      })

      // ---- Tier 1: IDLE EVICTION ---------------------------------
      // A slot showing the "📷 Active" chip (people === 0) AND with
      // score === 0 is contributing nothing visible. Once it's held
      // for MIN_HOLD_BEFORE_EVICT it's eligible for immediate
      // replacement by ANY candidate that has a non-zero score —
      // no 2× ratio requirement. Surfaces busier cameras as fast
      // as the 30-s poll cadence allows.
      const idleSlots = annotated
        .filter(x => x.people === 0
                  && x.score  === 0
                  && x.ageMs  >= MIN_HOLD_BEFORE_EVICT)
        // Replace oldest idle slots first so the longest-idle ones
        // turn over before more-recent idle ones.
        .sort((a, b) => b.ageMs - a.ageMs)

      for (const { i } of idleSlots) {
        if (!candidates.length) break
        const top = candidates[0]
        if (top.score > 0 || top.people > 0) {
          nextSlots[i] = { camera_id: top.camera_id, entered_at: now }
          candidates.shift()
        }
      }

      // ---- Tier 2: 2× RATIO HOLD EVICTION ------------------------
      // Slots with actual activity (people > 0 OR score > 0) get
      // the full 90-second hold. After that, they only yield to a
      // candidate scoring at least REPLACE_RATIO × the held score
      // (floor of 1 so a 0-score-but-people>0 slot is still
      // replaceable by something stronger). Process weakest first
      // so the strongest candidates get the best slots.
      const heldIdsAfterIdle = new Set(nextSlots.map(s => s.camera_id))
      const stale = annotated
        .filter(x => heldIdsAfterIdle.has(nextSlots[x.i].camera_id))
        .filter(x => now - nextSlots[x.i].entered_at >= SLOT_HOLD_MS)
        .filter(x => x.people > 0 || x.score > 0)
        .sort((a, b) => a.score - b.score)

      for (const { i, score } of stale) {
        if (!candidates.length) break
        const top = candidates[0]
        const needBeat = Math.max(score * REPLACE_RATIO, 1)
        if (top.score >= needBeat) {
          nextSlots[i] = { camera_id: top.camera_id, entered_at: now }
          candidates.shift()
        }
      }

      // Fill empty slots (first poll yielded < 15 cameras) with any
      // remaining candidates, no hold required.
      while (nextSlots.length < 15 && candidates.length) {
        const top = candidates.shift()!
        nextSlots.push({ camera_id: top.camera_id, entered_at: now })
      }
      return nextSlots
    })
  }, [latest, known])

  return (
    <div>
      {/* Orange banner. */}
      <div className="rounded p-3 mb-3 flex flex-wrap items-center gap-3
                      text-sm text-white bg-orange-500 shadow-sm">
        <span className="font-bold tracking-wide">🔴 LIVE ACTIVITY</span>
        <span className="text-white/90">
          Top {slots.length} cameras by current activity · 90-second hold per tile
        </span>
        <span className="ml-auto text-xs text-white/80">
          {lastAt
            ? `Updated ${Math.max(0, Math.floor((Date.now() - lastAt) / 1000))}s ago`
            : 'Loading…'} · auto-refresh 30s
        </span>
      </div>
      {err && (
        <Card className="p-4 text-sm text-red-600">
          Could not load activity. {err}
        </Card>
      )}
      {latest && slots.length === 0 && !err && (
        <Card className="p-8 text-center text-slate-500">
          No cameras reporting activity right now.
        </Card>
      )}
      {slots.length > 0 && (
        // Monitoring-wall layout. Grid height is pinned to the
        // remaining viewport so the three rows split evenly and
        // each tile feels like a wall-screen panel, not a
        // thumbnail. min-h-[640px] keeps tiles usable on shorter
        // displays. The tile itself is `h-full flex flex-col` so
        // the snapshot grabs the leftover space (no aspect-video
        // constraint), and the snapshot uses object-cover to fill
        // the cell cleanly regardless of native resolution.
        <div className="grid grid-cols-4 xl:grid-cols-5 gap-1.5
                        h-[calc(100vh-220px)] min-h-[640px]">
          {slots.map(slot => {
            const cam = known.get(slot.camera_id)
            // If we lost the camera from the latest fetch entirely
            // (e.g. it stopped reporting) but its hold isn't up, we
            // can't render without metadata — drop the slot quietly.
            if (!cam) return null
            return (
              <ActivityTile key={slot.camera_id} cam={cam}
                            enteredAt={slot.entered_at}
                            onClick={() => onPickFullscreen(slot.camera_id)} />
            )
          })}
        </div>
      )}
      {/* Fade keyframes injected once — no tailwind.config edit. */}
      <style>{`
        @keyframes vg-act-fade-in { from { opacity: 0; transform: scale(0.97) }
                                    to   { opacity: 1; transform: scale(1)    } }
        .vg-act-tile { animation: vg-act-fade-in 320ms ease-out both; }
        @keyframes vg-act-pulse { 0%,100% { box-shadow: 0 0 0 0 rgba(234,88,12,0.55) }
                                  50%     { box-shadow: 0 0 0 6px rgba(234,88,12,0.00) } }
        .vg-act-pulse { animation: vg-act-pulse 1800ms ease-out infinite; }
      `}</style>
    </div>
  )
}


function ActivityTile({ cam, enteredAt, onClick }: {
  cam: ActivityCamera
  enteredAt: number
  onClick: () => void
}) {
  const [src, setSrc] = useState<string | null>(null)
  const [busy, setBusy] = useState(true)
  // Re-fetch when the camera's freshest activity epoch advances so
  // the snapshot tracks the live feed. The parent owns polling
  // cadence — no extra timer here.
  useEffect(() => {
    let alive = true
    setBusy(true)
    api<{ jpeg_b64: string }>(`/cameras/${cam.camera_id}/snapshot`)
      .then(r => { if (alive) { setSrc(`data:image/jpeg;base64,${r.jpeg_b64}`); setBusy(false) } })
      .catch(() => { if (alive) { setSrc(null); setBusy(false) } })
    return () => { alive = false }
  }, [cam.camera_id, cam.last_activity_at])

  const headline = cam.people > 0
    ? `👥 ${cam.people} ${cam.people === 1 ? 'person' : 'people'} detected`
    : '📷 Active'

  // 90-second hold timer — both the bottom progress bar and the
  // "in slot Xm Ys" caption read from this. Math runs on every
  // parent forceTick (1 Hz), so the bar drains smoothly.
  const held = Math.max(0, Math.min(SLOT_HOLD_MS, Date.now() - enteredAt))
  const pct = Math.min(100, (held / SLOT_HOLD_MS) * 100)
  const remainSec = Math.max(0, Math.ceil((SLOT_HOLD_MS - held) / 1000))
  const m = Math.floor(remainSec / 60)
  const s = remainSec % 60
  const holdLabel = remainSec === 0
    ? 'eligible for rotation'
    : `in slot ${m}:${s.toString().padStart(2, '0')}`

  return (
    <button type="button" onClick={onClick}
            // vg-act-tile = fade-in keyframes from the parent
            // <style> block. Re-runs whenever React mounts a new
            // ActivityTile (i.e. when a slot's camera_id changes),
            // so swapped tiles fade in smoothly. h-full so the
            // button takes up the full grid cell — paired with the
            // parent's viewport-pinned grid height, this gives the
            // monitoring-wall feel: tall, equal-sized panels.
            className="text-left cursor-zoom-in vg-act-tile h-full">
    <Card className="p-1 h-full flex flex-col">
      <div className="flex items-center justify-between mb-1 text-xs shrink-0">
        <div className="truncate flex-1">
          <span className="font-medium text-slate-800">{cam.camera_name}</span>
          <span className="text-slate-500">
            {cam.store_name ? ` · ${cam.store_name}` : ''}
          </span>
        </div>
        <Badge color={cam.status === 'online' ? 'green' : 'amber'}>{cam.status}</Badge>
      </div>
      {/* Snapshot fills whatever vertical space is left after the
          header row + the hold-bar below. object-cover crops to
          fit the cell instead of letterboxing at the source 16:9
          ratio, so the panel always looks full. */}
      <div className={'relative bg-black rounded overflow-hidden flex-1 min-h-0 '
                       + (cam.people > 0 ? 'vg-act-pulse' : '')}>
        {src
          ? <img src={src} alt={cam.camera_name}
                 className="block w-full h-full object-cover opacity-90" />
          : (
            <div className="absolute inset-0 flex items-center justify-center
                            text-slate-400 text-xs">
              {busy ? 'Loading…' : 'Snapshot unavailable'}
            </div>
          )}
        <div className="absolute top-1 left-1 px-2 py-0.5 rounded
                        bg-orange-600 text-white text-[11px] font-bold">
          {headline}
        </div>
      </div>
      {/* Hold bar — drains over the active-camera hold window so
          the operator can see when a tile becomes eligible for
          rotation. Caption sits just below in tiny grey. shrink-0
          so the flex column above (snapshot) doesn't steal the
          bar's height. */}
      <div className="mt-1 shrink-0">
        <div className="h-1 w-full bg-slate-200 rounded overflow-hidden">
          <div className="h-full bg-orange-500 transition-[width] duration-1000 ease-linear"
               style={{ width: `${pct}%` }} />
        </div>
        <div className="text-[10px] text-slate-500 mt-0.5 tabular-nums">
          {holdLabel}
        </div>
      </div>
    </Card>
    </button>
  )
}
