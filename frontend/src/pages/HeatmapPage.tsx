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
  const [heatmapObjUrl, setHeatmapObjUrl] = useState<string | null>(null)
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

  // Active layer for the PNG overlay. Multi-select for engagement +
  // paths is fine, but only ONE raster layer (engagement / traffic /
  // congestion) renders as the heatmap PNG underneath — they share
  // the same colour space. Precedence matches the spec: engagement is
  // the default lead layer, then traffic, then congestion.
  function activeRasterLayer(): 'engagement' | 'traffic' | 'congestion' {
    if (layers.engagement) return 'engagement'
    if (layers.traffic)    return 'traffic'
    if (layers.congestion) return 'congestion'
    return 'engagement'
  }

  function heatmapUrl(forDownload = false): string {
    const params = new URLSearchParams({
      alpha: String(forDownload ? Math.max(0.85, opacity) : opacity),
      window: apiWindow(),
      layer:  activeRasterLayer(),
    })
    return `/api/analytics/heatmap/${cameraId}/image?${params}`
  }

  // Fetch the JSON grid alongside the image — it carries the
  // human-readable period label and the busiest-cell hint.
  useEffect(() => {
    let cancelled = false
    api<HeatmapGridResp>(`/analytics/heatmap/${cameraId}?window=${apiWindow()}`)
      .then(d => {
        if (cancelled) return
        setPeriodLabel(d.period_label ?? null)
        setPeakLabel(d.peak_label ?? null)
      })
      .catch(() => {
        if (!cancelled) { setPeriodLabel(null); setPeakLabel(null) }
      })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cameraId, bust, windowSel])

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

  // Re-fetch the PNG whenever the active raster layer changes too.
  // (The dependency on `layers` makes the next effect re-run.)

  // Heatmap PNG — authed fetch → blob → object URL.
  useEffect(() => {
    let cancelled = false
    let created: string | null = null
    const tok = localStorage.getItem('vg_access_token') ?? ''
    fetch(heatmapUrl(),
          { headers: { Authorization: `Bearer ${tok}` } })
      .then(res => res.ok ? res.blob() : null)
      .then(blob => {
        if (cancelled || !blob) return
        created = URL.createObjectURL(blob)
        setHeatmapObjUrl(created)
      })
      .catch(() => {})
    return () => {
      cancelled = true
      if (created) URL.revokeObjectURL(created)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cameraId, opacity, bust, windowSel, layers.engagement, layers.traffic, layers.congestion])

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
          <Button onClick={download} disabled={!heatmapObjUrl}>Download PNG</Button>
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
          {heatmapObjUrl && (layers.engagement || layers.traffic || layers.congestion) && (
            <img src={heatmapObjUrl} alt=""
                 className="absolute inset-0 w-full h-full pointer-events-none mix-blend-screen" />
          )}
          {layers.paths && paths && paths.edges && paths.edges.length > 0 && (
            <PathArrowsOverlay edges={paths.edges} />
          )}
          {heatmapObjUrl && (
            <div className="absolute top-3 right-3 px-2.5 py-1 rounded bg-black/70 text-white text-xs font-semibold
                            animate-pulse pointer-events-none">
              {layers.engagement ? '🔴 High Interest Zone'
                : layers.congestion ? '⚠️ Bottleneck'
                : '🔥 Busiest Area'}
            </div>
          )}
        </div>

        <div className="mt-3 px-1">
          <div className="text-[10px] uppercase tracking-wider text-slate-400 mb-1">
            Activity intensity
          </div>
          <div className="h-3 w-full rounded"
               style={{
                 background: 'linear-gradient(90deg, ' +
                   '#0a1628 0%, ' +
                   '#1e3a8a 20%, ' +
                   '#3b82f6 45%, ' +
                   '#f97316 65%, ' +
                   '#ea580c 85%, ' +
                   '#ff4500 100%)',
               }} />
          <div className="flex justify-between text-[10px] text-slate-400 mt-1">
            <span>🔵 Quiet</span>
            <span>🟠 Active</span>
            <span>🔥 Hotspot</span>
          </div>
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
