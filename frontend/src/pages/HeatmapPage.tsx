// Per-camera footfall heatmap viewer — Premier League / Opta style.
//
// Overlays the colourised heatmap PNG (from /analytics/heatmap/{id}/
// image) on the camera snapshot. Use the opacity slider to tune the
// blend; click Download to grab the overlay as a standalone PNG.

import { useEffect, useMemo, useRef, useState } from 'react'
import { useParams, Link, useNavigate } from 'react-router-dom'
import { Button, Card, PageHeader } from '@/components/ui/Primitives'
import { cameras } from '@/api/cameras'
import { stores } from '@/api/stores'
import { api } from '@/api/client'

// "3h" = rolling 3-hour bucket (resets at 00/03/06/09/12/15/18/21).
type TimeWindow = '3h' | 'today' | 'this_week'

interface HeatmapGridResp {
  grid?: number[][]
  rows?: number
  cols?: number
  window?: string
  period_label?: string
  // peak/busiest cell metadata. Optional — server may omit on cold start.
  peak_hour?: string | null
  peak_label?: string | null
  updated_at?: string | null
}

export default function HeatmapPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const cameraId = Number(id)

  const [snap, setSnap] = useState<string | null>(null)
  const [snapFailed, setSnapFailed] = useState<boolean>(false)
  const [cameraName, setCameraName] = useState<string | null>(null)
  const [storeId, setStoreId] = useState<number | null>(null)
  const [storeName, setStoreName] = useState<string | null>(null)
  // (server-rendered PNG was previously used as an <img> overlay;
  // we now paint the heatmap client-side from the JSON grid so it
  // blends properly. The PNG endpoint stays around for downloads.)
  const [opacity, setOpacity] = useState(0.85)
  const [bust, setBust] = useState(0)
  const [windowSel, setWindowSel] = useState<TimeWindow>('3h')
  const [periodLabel, setPeriodLabel] = useState<string | null>(null)
  const [peakLabel, setPeakLabel] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  // 4-layer heatmap toggles. Engagement defaults on per spec; paths
  // is checked too so the customer-flow arrows show by default.
  const [layers, setLayers] = useState<{ engagement: boolean
                                          traffic: boolean
                                          congestion: boolean
                                          paths: boolean }>({
    engagement: true, traffic: false, congestion: false, paths: true,
  })
  const [paths, setPaths] = useState<{ edges: { from: number[]; to: number[]; count: number }[] } | null>(null)

  // Pull the camera + its store up-front so the placeholder reads
  // "Vivo Junction - Camera 3" and the back link knows where to go.
  useEffect(() => {
    cameras.get(cameraId).then(c => {
      setCameraName(c.name)
      setStoreId(c.store_id)
      if (c.store_id) {
        stores.get(c.store_id).then(s => setStoreName(s.name)).catch(() => {})
      }
    }).catch(() => {})
  }, [cameraId])

  useEffect(() => {
    setSnapFailed(false)
    cameras.snapshot(cameraId)
      .then(s => { setSnap(s.jpeg_b64); setSnapFailed(false) })
      .catch(() => { setSnap(null); setSnapFailed(true) })
    const t = setInterval(() => setBust(b => b + 1), 30_000)
    return () => clearInterval(t)
  }, [cameraId, bust])

  // Map UI window → API window param.
  function apiWindow(): string {
    return windowSel === '3h' ? '3h' : windowSel === 'this_week' ? 'week' : 'day'
  }

  // Active raster layer (engagement > traffic > congestion). Only
  // ONE of these renders the colour overlay at a time so the legend
  // is unambiguous; the Paths layer renders on top independently.
  function activeRasterLayer(): 'engagement' | 'traffic' | 'congestion' {
    if (layers.engagement) return 'engagement'
    if (layers.traffic)    return 'traffic'
    if (layers.congestion) return 'congestion'
    return 'engagement'
  }

  // Fetch the JSON grid for the active raster layer. We render the
  // overlay client-side (SVG with gaussian blur) rather than rely on
  // the server-rendered PNG — that gives us per-layer colour scales,
  // proper opacity blending on the snapshot, smooth gaussian blobs
  // instead of grid squares, and a graceful "empty" baseline.
  const [gridResp, setGridResp] = useState<HeatmapGridResp | null>(null)
  const [gridLoading, setGridLoading] = useState<boolean>(true)

  // Per-layer in-memory cache so toggling between Engagement /
  // Traffic / Congestion doesn't re-fetch the same grid every click.
  // Entries live for GRID_CACHE_MS; the Refresh button (bust++) and a
  // window switch both clear the cache, so this only shortcuts rapid
  // toggle clicks — never hides genuinely stale data.
  const GRID_CACHE_MS = 30_000
  const gridCacheRef = useRef<Map<string, { data: HeatmapGridResp; ts: number }>>(new Map())
  useEffect(() => { gridCacheRef.current.clear() }, [cameraId, bust, windowSel])

  // Pull the active raster layer's grid + period/peak labels.
  useEffect(() => {
    let cancelled = false
    const layer = activeRasterLayer()
    const cacheKey = `${windowSel}:${layer}`
    const cached = gridCacheRef.current.get(cacheKey)
    if (cached && Date.now() - cached.ts < GRID_CACHE_MS) {
      setGridResp(cached.data)
      setPeriodLabel(cached.data.period_label ?? null)
      setPeakLabel(cached.data.peak_label ?? null)
      setGridLoading(false)
      return
    }
    setGridLoading(true)
    const ep = layer === 'traffic'
      ? `/analytics/heatmap/${cameraId}?window=${apiWindow()}`
      : `/analytics/heatmap/${cameraId}/${layer}?window=${apiWindow()}`
    api<HeatmapGridResp>(ep)
      .then(d => {
        if (cancelled) return
        gridCacheRef.current.set(cacheKey, { data: d, ts: Date.now() })
        setGridResp(d)
        setPeriodLabel(d.period_label ?? null)
        setPeakLabel(d.peak_label ?? null)
        setGridLoading(false)
      })
      .catch(() => {
        if (!cancelled) {
          setGridResp(null); setPeriodLabel(null); setPeakLabel(null)
          setGridLoading(false)
        }
      })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cameraId, bust, windowSel, layers.engagement, layers.traffic, layers.congestion])

  // Server-rendered PNG — still used for the Download PNG button so
  // operators get a high-resolution image they can drop into a deck.
  function heatmapUrl(forDownload = false): string {
    const params = new URLSearchParams({
      alpha: String(forDownload ? Math.max(0.85, opacity) : opacity),
      window: apiWindow(),
      layer:  activeRasterLayer(),
    })
    return `/api/analytics/heatmap/${cameraId}/image?${params}`
  }

  // Path edges — only fetched when the Paths layer is on.
  useEffect(() => {
    if (!layers.paths) { setPaths(null); return }
    let cancelled = false
    api<{ edges: { from: number[]; to: number[]; count: number }[] }>(
      `/analytics/heatmap/${cameraId}/paths?window=${apiWindow()}`
    ).then(d => { if (!cancelled) setPaths(d) })
     .catch(() => { if (!cancelled) setPaths(null) })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cameraId, bust, windowSel, layers.paths])

  async function download() {
    const tok = localStorage.getItem('vg_access_token') ?? ''
    try {
      const res = await fetch(heatmapUrl(true),
                              { headers: { Authorization: `Bearer ${tok}` } })
      if (!res.ok) {
        setError(`Download failed: ${res.status} ${res.statusText}`)
        return
      }
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      const safeStore = (storeName ?? 'store').replace(/\s+/g, '_')
      const safeCam   = (cameraName ?? `camera_${cameraId}`).replace(/\s+/g, '_')
      a.download = `heatmap_${safeStore}_${safeCam}_${apiWindow()}.png`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (e) {
      setError(String(e))
    }
  }

  const backHref = storeId ? `/stores/${storeId}` : '/cameras'
  const backLabel = storeName ? `← Back to ${storeName} Dashboard` : '← Back'

  // "No heatmap data yet" — true when the grid is missing OR every
  // cell is zero. Combined with !gridLoading so the empty-state
  // overlay doesn't flash during the initial fetch.
  const gridEmpty = useMemo(() => {
    const g = gridResp?.grid
    if (!g || !g.length) return true
    return g.every(row => row.every(v => !v))
  }, [gridResp])
  const showEmptyState = !gridLoading && gridEmpty
                         && (layers.engagement || layers.traffic || layers.congestion)

  return (
    <div className="p-6">
      <div className="mb-3">
        <button
          onClick={() => navigate(backHref)}
          className="text-sm text-slate-600 dark:text-slate-300 hover:text-slate-900 dark:hover:text-slate-100 font-medium">
          {backLabel}
        </button>
      </div>

      <PageHeader
        title={cameraName
          ? `${storeName ? storeName + ' — ' : ''}${cameraName} heatmap`
          : `Footfall heatmap — camera #${cameraId}`}
        actions={<>
          <Link to="/cameras"><Button variant="ghost">Cameras</Button></Link>
          <Button onClick={download}>Download PNG</Button>
        </>}
      />

      <Card className="p-3 mb-3 bg-slate-900 text-white border-slate-800">
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-xs uppercase tracking-wide text-slate-400">Time window</span>
          {(['3h', 'today', 'this_week'] as TimeWindow[]).map(w => (
            <button key={w}
                    onClick={() => setWindowSel(w)}
                    title={w === '3h' ? 'Rolling 3-hour bucket — resets at 00, 03, 06, 09, 12, 15, 18, 21'
                         : w === 'today' ? 'Since local midnight today'
                         : 'Since the start of this iso-week (Mon 00:00)'}
                    className={'px-3 py-1 rounded text-xs font-medium ' +
                      (windowSel === w
                        ? 'bg-orange-500 text-white'
                        : 'bg-slate-800 text-slate-300 hover:bg-slate-700')}>
              {w === '3h' ? 'Last 3h' : w === 'today' ? 'Today' : 'This Week'}
            </button>
          ))}
          {periodLabel && (
            <span className="text-xs text-slate-300 bg-slate-800 rounded px-2 py-1">
              Last updated: {periodLabel}
            </span>
          )}
          <div className="flex-1" />
          <span className="text-xs text-slate-400">Opacity</span>
          <input type="range" min={0} max={1} step={0.05}
                 value={opacity} onChange={e => setOpacity(Number(e.target.value))}
                 className="w-32" />
          <span className="text-xs w-10 text-right">{Math.round(opacity * 100)}%</span>
          <Button variant="ghost" onClick={() => setBust(b => b + 1)}>Refresh</Button>
        </div>
        {peakLabel && (
          <div className="mt-2 text-xs text-orange-300">
            🔥 Busiest period today: {peakLabel}
          </div>
        )}
        {/* 4-layer toggle — engagement default on. */}
        <div className="mt-3 flex flex-wrap items-center gap-3 text-xs">
          <span className="uppercase tracking-wide text-slate-400">Layers</span>
          <LayerToggle label="Engagement"  swatch="#ef4444"
                       on={layers.engagement}
                       onChange={v => setLayers(s => ({ ...s, engagement: v }))}
                       hint="Customer interest — aisle/shelf zones, dwell-weighted." />
          <LayerToggle label="Traffic"     swatch="#3b82f6"
                       on={layers.traffic}
                       onChange={v => setLayers(s => ({ ...s, traffic: v }))}
                       hint="Foot-traffic density — every zone, every person." />
          <LayerToggle label="Congestion"  swatch="#f59e0b"
                       on={layers.congestion}
                       onChange={v => setLayers(s => ({ ...s, congestion: v }))}
                       hint="Queue / counter dwell — waiting, not interest." />
          <LayerToggle label="Paths"       swatch="#a78bfa"
                       on={layers.paths}
                       onChange={v => setLayers(s => ({ ...s, paths: v }))}
                       hint="Movement arrows — thickness scales with traffic." />
        </div>
      </Card>

      <Card className="p-3 bg-[#0d1b2a] text-white border-slate-800">
        <div className="relative w-full bg-[#0a1628] rounded overflow-hidden">
          {/* Camera snapshot: render the <img> as soon as we have bytes
              and fade it in via onLoad. While the bytes are in flight,
              show a pulsing skeleton rectangle so the layout doesn't
              flicker — the snapshot endpoint is fast (Redis cache
              hit) most of the time but can take a few seconds on a
              cold camera. */}
          {snap && (
            <img src={`data:image/jpeg;base64,${snap}`}
                 alt="camera snapshot"
                 className="block w-full h-auto opacity-0 transition-opacity duration-300"
                 onLoad={(e) => {
                   const el = e.currentTarget
                   el.classList.remove('opacity-0')
                   el.classList.add('opacity-90')
                 }} />
          )}
          {!snap && !snapFailed && (
            <div className="aspect-video relative overflow-hidden bg-slate-800"
                 aria-busy="true" aria-label="Loading camera snapshot">
              <div className="absolute inset-0 bg-gradient-to-r from-slate-800
                              via-slate-700 to-slate-800 animate-pulse" />
              <div className="absolute inset-0 flex flex-col items-center
                              justify-center text-slate-400 gap-1">
                <div className="text-3xl opacity-50">📷</div>
                <div className="text-xs">Loading snapshot…</div>
              </div>
            </div>
          )}
          {!snap && snapFailed && (
            <div className="aspect-video flex flex-col items-center justify-center
                            bg-slate-800 text-slate-300 gap-1">
              <div className="text-3xl opacity-50">📷</div>
              <div className="text-sm font-medium">
                {cameraName ?? `Camera ${cameraId}`}
              </div>
              <div className="text-xs text-slate-500">
                Snapshot unavailable — heatmap still updating
              </div>
            </div>
          )}
          {(layers.engagement || layers.traffic || layers.congestion) && (
            <HeatmapSvgOverlay
              grid={gridResp?.grid ?? null}
              layer={activeRasterLayer()}
              opacity={opacity} />
          )}
          {layers.paths && paths && paths.edges && paths.edges.length > 0 && (
            <PathArrowsOverlay edges={paths.edges} />
          )}

          {/* Empty-state overlay — sits ON TOP of the snapshot so the
              operator immediately sees why the heatmap is blank.
              Pointer-events-none so the legend / Refresh button stays
              clickable behind it. */}
          {showEmptyState && (
            <div className="absolute inset-0 flex items-center justify-center
                            p-4 pointer-events-none">
              <div className="max-w-sm bg-slate-900/85 border border-slate-700
                              rounded-lg px-5 py-4 text-center text-slate-200
                              shadow-lg backdrop-blur-sm">
                <div className="text-3xl mb-1">📷</div>
                <div className="font-medium text-sm mb-1">
                  Heatmap data is being collected for this camera
                </div>
                <div className="text-xs text-slate-400 leading-snug">
                  Check back in 15–30 minutes as customers are detected.
                  Data updates every 30 seconds.
                </div>
              </div>
            </div>
          )}

          <div className="absolute top-3 right-3 px-2.5 py-1 rounded bg-black/70 text-white text-xs font-semibold
                          animate-pulse pointer-events-none">
            {layers.engagement ? '🔴 High Interest Zone'
              : layers.congestion ? '⚠️ Bottleneck'
              : layers.traffic ? '🔥 Busiest Area'
              : '🧭 Customer flow'}
          </div>
        </div>

        {/* Per-layer legend + peak value */}
        <HeatmapLegend layer={activeRasterLayer()}
                       grid={gridResp?.grid ?? null} />

        {/* Interpretation guide — always visible so operators learn
            what the colours mean without scrolling away. */}
        <div className="mt-3 grid grid-cols-1 md:grid-cols-2 gap-1.5 text-[11px] text-slate-300 bg-slate-900/60 rounded px-3 py-2">
          <div>🔴 <span className="text-slate-200">Red zones:</span> customers spending the most time — prime product placement.</div>
          <div>🟠 <span className="text-slate-200">Orange zones:</span> high interest — strong candidates for promotions.</div>
          <div>🟢 <span className="text-slate-200">Green zones:</span> regular foot traffic.</div>
          <div>🔵 <span className="text-slate-200">Blue zones:</span> quick pass-through areas.</div>
          <div className="md:col-span-2">⚠️ Toggle the <span className="text-slate-200">Congestion</span> layer for queue / checkout-bottleneck analysis.</div>
        </div>

        <div className="text-xs text-slate-400 mt-3">
          Heatmap updates every 30 seconds from the inference worker.
          Enable <code className="text-slate-300">heatmap</code> in this camera's
          AI settings to start accumulating data.
        </div>
        {error && <div className="text-red-400 text-sm mt-2">{error}</div>}
      </Card>
    </div>
  )
}

function LayerToggle({ label, swatch, on, onChange, hint }: {
  label: string; swatch: string; on: boolean
  onChange: (v: boolean) => void; hint?: string
}) {
  return (
    <label className="inline-flex items-center gap-1.5 cursor-pointer"
           title={hint}>
      <input type="checkbox" checked={on}
             onChange={e => onChange(e.target.checked)}
             className="accent-orange-500" />
      <span className="inline-block w-3 h-3 rounded-sm"
            style={{ background: swatch }} />
      <span className={on ? 'text-white' : 'text-slate-400'}>{label}</span>
    </label>
  )
}

// Sharp 5-bucket discrete colour scale — no interpolation, no
// gradient. Each cell falls into exactly one bucket so the overlay
// reads as a colour-coded grid instead of a blurred blob. Identical
// stops across all three raster layers per the operator's request;
// the layer name only changes the legend wording and unit.
type Bucket = { upper: number; color: string; label: string; emoji: string }
const BUCKETS: Bucket[] = [
  { upper: 0.2, color: '#1e40af', label: 'Low',      emoji: '🔵' },
  { upper: 0.4, color: '#16a34a', label: 'Moderate', emoji: '🟢' },
  { upper: 0.6, color: '#ca8a04', label: 'Active',   emoji: '🟡' },
  { upper: 0.8, color: '#ea580c', label: 'High',     emoji: '🟠' },
  { upper: 1.0, color: '#dc2626', label: 'Peak',     emoji: '🔴' },
]

function bucketColor(score: number): string {
  for (const b of BUCKETS) if (score <= b.upper) return b.color
  return BUCKETS[BUCKETS.length - 1].color
}

// SVG-based heat overlay. Renders a 20×20 grid of sharp coloured
// rects on top of the camera snapshot. No blur, no gradient — each
// non-zero cell is one of the five discrete bucket colours at 0.55
// opacity, so the snapshot is always visible underneath.
function HeatmapSvgOverlay({ grid, layer, opacity: _opacity }: {
  grid: number[][] | null
  layer: 'engagement' | 'traffic' | 'congestion'
  opacity: number
}) {
  void layer  // colour scale is shared across raster layers now
  const G = 20
  const hasData = !!(grid && grid.length && grid.some(r => r.some(v => v > 0)))
  const max = hasData
    ? Math.max(...(grid as number[][]).flatMap(r => r))
    : 1
  return (
    <svg className="absolute inset-0 w-full h-full pointer-events-none"
         style={{ zIndex: 10 }}
         viewBox={`0 0 ${G} ${G}`} preserveAspectRatio="none">
      {hasData
        ? (grid as number[][]).flatMap((row, y) => row.map((v, x) => {
            if (!v) return null
            const t = v / max
            return (
              <rect key={`${y}-${x}`}
                    x={x} y={y} width={1} height={1}
                    fill={bucketColor(t)}
                    opacity={0.55} />
            )
          }))
        : null}
    </svg>
  )
}

// Per-layer legend strip — discrete colour chips matching the
// overlay buckets, plus an "X active zones" cell count so the
// operator knows how much data is on screen. When there's no data
// at all we surface the "still accumulating" hint instead.
function HeatmapLegend({ grid, layer }: {
  grid: number[][] | null
  layer: 'engagement' | 'traffic' | 'congestion'
}) {
  const flat = grid && grid.length ? grid.flatMap(r => r) : []
  const activeCells = flat.filter(v => v > 0).length
  const max = flat.length ? Math.max(...flat) : 0
  const unit = layer === 'engagement' ? 'engagement score'
             : layer === 'congestion' ? 'congestion score'
             : 'visits'
  return (
    <div className="mt-3 px-1">
      <div className="flex flex-wrap items-baseline justify-between gap-2 mb-1.5">
        <div className="text-[10px] uppercase tracking-wider text-slate-400">
          {layer === 'engagement' ? 'Customer engagement'
            : layer === 'congestion' ? 'Congestion / wait'
            : 'Foot-traffic intensity'} · {unit}
        </div>
        <div className="text-[11px] text-slate-300 tabular-nums">
          Showing {activeCells} active zone{activeCells === 1 ? '' : 's'}
          {max > 0 ? ` · peak ${layer === 'traffic' ? Math.round(max) : max.toFixed(1)}` : ''}
        </div>
      </div>
      <div className="flex flex-wrap gap-2 text-[11px] text-slate-200">
        {BUCKETS.map(b => (
          <span key={b.color} className="inline-flex items-center gap-1.5">
            <span className="inline-block w-3 h-3 rounded-sm"
                  style={{ background: b.color, opacity: 0.85 }} />
            <span>{b.emoji} {b.label}</span>
          </span>
        ))}
      </div>
      {activeCells === 0 && (
        <div className="mt-2 text-xs text-amber-300">
          Heatmap data accumulating — check back in 15 minutes.
        </div>
      )}
    </div>
  )
}

// SVG overlay rendering customer-flow arrows from the /paths layer.
// Edges are in 20×20 grid space; we draw them on a 100% viewBox so
// they stretch over whatever resolution the snapshot is.
function PathArrowsOverlay({ edges }: {
  edges: { from: number[]; to: number[]; count: number }[]
}) {
  const GRID = 20
  const top = edges.slice(0, 60)
  const max = top.reduce((m, e) => Math.max(m, e.count), 1)
  // Convert (gy, gx) cell coords → normalised 0..100 viewBox coords
  // (centre of the cell).
  const cx = (gx: number) => ((gx + 0.5) / GRID) * 100
  const cy = (gy: number) => ((gy + 0.5) / GRID) * 100
  return (
    <svg className="absolute inset-0 w-full h-full pointer-events-none"
         viewBox="0 0 100 100" preserveAspectRatio="none">
      <defs>
        <marker id="path-arrow" viewBox="0 0 10 10" refX="8" refY="5"
                markerWidth="6" markerHeight="6" orient="auto">
          <path d="M 0 0 L 10 5 L 0 10 z" fill="#a78bfa" />
        </marker>
      </defs>
      {top.map((e, i) => {
        const strength = e.count / max
        return (
          <line key={i}
                x1={cx(e.from[1])} y1={cy(e.from[0])}
                x2={cx(e.to[1])}   y2={cy(e.to[0])}
                stroke="#a78bfa"
                strokeOpacity={0.4 + 0.55 * strength}
                strokeWidth={0.3 + 1.2 * strength}
                vectorEffect="non-scaling-stroke"
                markerEnd="url(#path-arrow)" />
        )
      })}
    </svg>
  )
}
