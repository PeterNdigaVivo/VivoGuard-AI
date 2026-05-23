// Per-camera footfall heatmap viewer — Premier League / Opta style.
//
// Overlays the colourised heatmap PNG (from /analytics/heatmap/{id}/
// image) on the camera snapshot. Use the opacity slider to tune the
// blend; click Download to grab the overlay as a standalone PNG.

import { useEffect, useState } from 'react'
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

  // Pull the active raster layer's grid + period/peak labels.
  useEffect(() => {
    let cancelled = false
    const layer = activeRasterLayer()
    const ep = layer === 'traffic'
      ? `/analytics/heatmap/${cameraId}?window=${apiWindow()}`
      : `/analytics/heatmap/${cameraId}/${layer}?window=${apiWindow()}`
    api<HeatmapGridResp>(ep)
      .then(d => {
        if (cancelled) return
        setGridResp(d)
        setPeriodLabel(d.period_label ?? null)
        setPeakLabel(d.peak_label ?? null)
      })
      .catch(() => {
        if (!cancelled) {
          setGridResp(null); setPeriodLabel(null); setPeakLabel(null)
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

  return (
    <div className="p-6">
      <div className="mb-3">
        <button
          onClick={() => navigate(backHref)}
          className="text-sm text-slate-600 hover:text-slate-900 font-medium">
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
          {snap ? (
            <img src={`data:image/jpeg;base64,${snap}`}
                 className="block w-full h-auto opacity-90" alt="camera snapshot" />
          ) : (
            <div className="aspect-video flex flex-col items-center justify-center
                            bg-slate-800 text-slate-300 gap-1">
              <div className="text-3xl opacity-50">📷</div>
              <div className="text-sm font-medium">
                {cameraName ?? `Camera ${cameraId}`}
              </div>
              <div className="text-xs text-slate-500">
                {snapFailed ? 'Snapshot unavailable — heatmap still updating'
                            : 'Loading snapshot…'}
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

// Per-layer colour ramps. Each ramp is a list of (stop, "rgb")
// pairs; intensity is interpolated between adjacent stops in JS.
// Picking the ramp per layer is the user's request — engagement is
// the warm cool→hot interest scale; traffic is Premier-League navy
// blue→orange; congestion is green→amber→red.
type Ramp = [number, [number, number, number]][]
const RAMPS: Record<'engagement' | 'traffic' | 'congestion', Ramp> = {
  engagement: [
    [0.0, [ 30,  64, 175]],  // blue
    [0.3, [ 16, 185, 129]],  // green
    [0.6, [250, 204,  21]],  // yellow
    [0.8, [249, 115,  22]],  // orange
    [1.0, [220,  38,  38]],  // red
  ],
  traffic: [
    [0.0, [ 10,  22,  40]],  // deep navy
    [0.2, [ 30,  58, 138]],  // royal blue
    [0.45, [ 59, 130, 246]], // sky blue
    [0.65, [249, 115,  22]], // orange
    [0.85, [234,  88,  12]], // bright orange
    [1.0, [255,  69,   0]],  // vivid orange-red
  ],
  congestion: [
    [0.0, [ 16, 185, 129]],  // green
    [0.5, [245, 158,  11]],  // amber
    [1.0, [220,  38,  38]],  // red
  ],
}

function rampColor(ramp: Ramp, v: number): string {
  v = Math.max(0, Math.min(1, v))
  for (let i = 0; i < ramp.length - 1; i++) {
    const [lo, loC] = ramp[i]
    const [hi, hiC] = ramp[i + 1]
    if (v <= hi) {
      const t = (v - lo) / Math.max(1e-6, hi - lo)
      const r = Math.round(loC[0] + (hiC[0] - loC[0]) * t)
      const g = Math.round(loC[1] + (hiC[1] - loC[1]) * t)
      const b = Math.round(loC[2] + (hiC[2] - loC[2]) * t)
      return `rgb(${r},${g},${b})`
    }
  }
  const last = ramp[ramp.length - 1][1]
  return `rgb(${last[0]},${last[1]},${last[2]})`
}

// SVG-based heat overlay. Each grid cell is rendered as a coloured
// rect inside a <g> that runs through a gaussian-blur filter, so the
// hard cell boundaries melt into smooth retail-style blobs. Cells
// with zero intensity are skipped, but we paint a faint baseline
// wash over the whole frame so the operator always sees the layer
// is "live".
function HeatmapSvgOverlay({ grid, layer, opacity }: {
  grid: number[][] | null
  layer: 'engagement' | 'traffic' | 'congestion'
  opacity: number
}) {
  const G = 20
  const ramp = RAMPS[layer]
  // Empty-state baseline: faint blue wash so the operator knows the
  // heatmap is mounted even before any cell has data.
  const hasData = !!(grid && grid.length && grid.some(r => r.some(v => v > 0)))
  const max = hasData
    ? Math.max(...(grid as number[][]).flatMap(r => r))
    : 1
  // Clamp opacity so the camera image is always visible beneath.
  const cellOpacity = Math.min(0.65, Math.max(0.3, opacity))

  return (
    <svg className="absolute inset-0 w-full h-full pointer-events-none"
         viewBox={`0 0 ${G} ${G}`} preserveAspectRatio="none">
      <defs>
        {/* Generous blur so adjacent cells merge into blobs rather
            than reading as a Minecraft grid. stdDeviation is in
            viewBox units (i.e. cells), so ~1.5 covers ~3 cells. */}
        <filter id="heat-blur" x="-20%" y="-20%" width="140%" height="140%">
          <feGaussianBlur stdDeviation="1.5" />
        </filter>
      </defs>
      {/* Faint baseline so the layer is visibly "on" even at zero. */}
      <rect x="0" y="0" width={G} height={G}
            fill="rgb(30,64,175)" opacity={hasData ? 0.05 : 0.12} />
      {hasData && (
        <g filter="url(#heat-blur)" opacity={cellOpacity}>
          {(grid as number[][]).flatMap((row, y) => row.map((v, x) => {
            if (!v) return null
            const t = v / max
            // Sub-cell cells are 1×1 in viewBox; oversize slightly so
            // neighbours bleed into one continuous blob.
            return <rect key={`${y}-${x}`}
                         x={x - 0.2} y={y - 0.2}
                         width={1.4} height={1.4}
                         fill={rampColor(ramp, t)}
                         opacity={0.4 + 0.6 * t} />
          }))}
        </g>
      )}
    </svg>
  )
}

// Per-layer legend strip. Reads max value from the grid so the
// "Peak" tick shows an actual number, not just "100%".
function HeatmapLegend({ grid, layer }: {
  grid: number[][] | null
  layer: 'engagement' | 'traffic' | 'congestion'
}) {
  const ramp = RAMPS[layer]
  const max = grid && grid.length
    ? Math.max(...grid.flatMap(r => r))
    : 0
  const gradient = ramp
    .map(([s, c]) => `rgb(${c[0]},${c[1]},${c[2]}) ${Math.round(s * 100)}%`)
    .join(', ')
  const unit = layer === 'engagement' ? 'engagement score'
             : layer === 'congestion' ? 'congestion score'
             : 'visits'
  const tick = (frac: number) => layer === 'traffic'
    ? Math.round(max * frac)
    : (max * frac).toFixed(1)
  return (
    <div className="mt-3 px-1">
      <div className="text-[10px] uppercase tracking-wider text-slate-400 mb-1">
        {layer === 'engagement' ? 'Customer engagement'
          : layer === 'congestion' ? 'Congestion / wait'
          : 'Foot-traffic intensity'} · {unit}
      </div>
      <div className="h-3 w-full rounded"
           style={{ background: `linear-gradient(90deg, ${gradient})` }} />
      <div className="flex justify-between text-[10px] text-slate-300 mt-1 tabular-nums">
        <span>🔵 Low · 0</span>
        <span>🟢 Moderate · {tick(0.3)}</span>
        <span>🟡 Active · {tick(0.6)}</span>
        <span>🟠 High · {tick(0.8)}</span>
        <span>🔴 Peak · {tick(1)}</span>
      </div>
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
