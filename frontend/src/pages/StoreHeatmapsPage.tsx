// Composite heatmap grid for a whole store.
//
// Two modes:
//   • Hotspot — one tile per camera with its colourised heatmap
//     overlay (existing). Answers "where do customers stand?".
//   • Flow    — single combined view with zone labels and arrows
//     between consecutive zone visits, sized by how many tracks
//     took that path. Answers "where do customers GO?".
//
// Flow mode is powered by /analytics/store/{id}/customer-flow which
// aggregates over customer_journeys.zone_sequence_json. Zone arrows
// are SVG so they scale crisply on top of the heatmap PNG.

import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Badge, Button, Card, PageHeader, Skeleton } from '@/components/ui/Primitives'
import { api } from '@/api/client'
import { cameras as camsApi } from '@/api/cameras'

interface CameraHeatmap {
  camera_id: number
  camera_name: string
  status: string
  heatmap_url: string
  rank_label?: string | null
  hotspot: { value: number; norm_x: number; norm_y: number } | null
}

type Mode = 'hotspot' | 'flow'

export default function StoreHeatmapsPage() {
  const { id } = useParams()
  const storeId = Number(id)
  const [data, setData] = useState<{ cameras: CameraHeatmap[] } | null>(null)
  const [mode, setMode] = useState<Mode>('hotspot')

  useEffect(() => {
    api<{ cameras: CameraHeatmap[] }>(`/analytics/store/${storeId}/heatmaps`)
      .then(setData).catch(console.error)
  }, [storeId])

  if (!data) {
    return (
      <div className="p-6">
        <PageHeader title="Store heatmaps" />
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {[1,2,3,4].map(i => <Skeleton key={i} className="h-64" />)}
        </div>
      </div>
    )
  }

  return (
    <div className="p-6">
      <PageHeader
        title="Store heatmaps"
        actions={<>
          <Button variant={mode === 'hotspot' ? 'primary' : 'ghost'}
                  onClick={() => setMode('hotspot')}>🔥 Hotspot</Button>
          <Button variant={mode === 'flow' ? 'primary' : 'ghost'}
                  onClick={() => setMode('flow')}>🧭 Customer flow</Button>
          <Link to={`/stores/${storeId}`}
                className="text-sky-600 hover:underline self-center ml-2">← Store dashboard</Link>
        </>}
      />

      {data.cameras.length === 0 && (
        <Card className="p-8 text-center text-slate-500">No cameras in this store yet.</Card>
      )}

      {mode === 'hotspot' ? (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {data.cameras.map(c => <HeatmapCard key={c.camera_id} c={c} />)}
        </div>
      ) : (
        <FlowView storeId={storeId} backdrop={data.cameras[0] ?? null} />
      )}
    </div>
  )
}

// Each card fetches its own snapshot + heatmap PNG WITH AUTH (so the
// JWT is sent), converts them to object URLs, and uses those URLs for
// both the <img> preview and the Download anchor. A plain <a href=...>
// to the API doesn't carry the JWT and 401s — that's why downloads
// were failing.
function HeatmapCard({ c }: { c: CameraHeatmap }) {
  const [snapshot, setSnapshot] = useState<string | null>(null)
  const [heatmap,  setHeatmap]  = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    let createdUrls: string[] = []

    async function fetchPNG(url: string): Promise<string | null> {
      const tok = localStorage.getItem('vg_access_token') ?? ''
      const res = await fetch(url, { headers: { Authorization: `Bearer ${tok}` } })
      if (!res.ok) return null
      const blob = await res.blob()
      const objUrl = URL.createObjectURL(blob)
      createdUrls.push(objUrl)
      return objUrl
    }

    // Snapshot comes from a JSON endpoint as base64 → data URL.
    camsApi.snapshot(c.camera_id)
      .then(s => { if (!cancelled) setSnapshot(`data:image/jpeg;base64,${s.jpeg_b64}`) })
      .catch(() => {})

    // Heatmap PNG needs an authed fetch.
    fetchPNG(c.heatmap_url)
      .then(u => { if (!cancelled) setHeatmap(u) })
      .catch(e => { if (!cancelled) setError(String(e)) })

    return () => {
      cancelled = true
      // Revoke any object URLs we created so we don't leak memory.
      createdUrls.forEach(u => URL.revokeObjectURL(u))
    }
  }, [c.camera_id, c.heatmap_url])

  async function download() {
    // Re-fetch the PNG fresh so the operator gets the latest grid,
    // not a stale cached copy. Same authed-blob pattern.
    const tok = localStorage.getItem('vg_access_token') ?? ''
    try {
      const res = await fetch(c.heatmap_url, { headers: { Authorization: `Bearer ${tok}` } })
      if (!res.ok) {
        setError(`Download failed: ${res.status} ${res.statusText}`)
        return
      }
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `heatmap_camera_${c.camera_id}.png`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      // Free the blob after the click completes.
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (e) {
      setError(String(e))
    }
  }

  return (
    <Card className="p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="font-medium truncate">{c.camera_name}</div>
        <Badge color={c.status === 'online' ? 'green' : 'amber'}>{c.status}</Badge>
      </div>
      <div className="relative bg-slate-900 rounded overflow-hidden aspect-video">
        {snapshot && <img src={snapshot} alt="" className="w-full h-full object-cover" />}
        {heatmap && (
          <img src={heatmap} alt=""
               className="absolute inset-0 w-full h-full pointer-events-none mix-blend-screen" />
        )}
        {!snapshot && !heatmap && (
          <div className="absolute inset-0 flex items-center justify-center text-slate-500 text-sm">
            Loading…
          </div>
        )}
        {c.hotspot && (
          <div className="absolute"
               style={{
                 left: `${c.hotspot.norm_x * 100}%`,
                 top:  `${c.hotspot.norm_y * 100}%`,
                 transform: 'translate(-50%, -100%)',
               }}>
            <div className="bg-amber-400 text-black text-xs px-2 py-0.5 rounded shadow whitespace-nowrap">
              {c.rank_label ?? 'Hotspot'}
            </div>
          </div>
        )}
      </div>
      <div className="mt-2 flex justify-between items-center text-xs">
        <span className="text-slate-500">
          {c.hotspot ? `${c.hotspot.value} hits at brightest cell` : 'No heatmap data yet'}
        </span>
        <button onClick={download}
                disabled={!heatmap}
                className="text-sky-600 hover:underline disabled:text-slate-400 disabled:no-underline disabled:cursor-not-allowed">
          Download PNG
        </button>
      </div>
      {error && <div className="text-red-600 text-xs mt-1">{error}</div>}
    </Card>
  )
}


// ---------------------------------------------------------------------------
// FlowView — customer-flow mode. Renders zone labels + directional
// arrows from /analytics/store/{id}/customer-flow on top of the first
// camera's heatmap PNG (used as a visual backdrop).
//
// Coordinate system: zones live in normalised image space [0..1] for
// each camera. We render them on a single shared SVG (also 0..1
// normalised via viewBox=0 0 100 100), so positions are approximate
// across cameras with different framings. For Vivo retail stores with
// one-camera-per-area floorplans that's fine for showing the
// topological flow (entry → aisle → counter).

interface FlowPayload {
  zones: { zone_id: number; name: string; cx: number; cy: number }[]
  edges: { from: number; to: number; count: number }[]
  total_journeys: number
}

function FlowView({ storeId, backdrop }: {
  storeId: number
  backdrop: CameraHeatmap | null
}) {
  const [flow, setFlow] = useState<FlowPayload | null>(null)
  const [backdropUrl, setBackdropUrl] = useState<string | null>(null)

  useEffect(() => {
    api<FlowPayload>(`/analytics/store/${storeId}/customer-flow`)
      .then(setFlow).catch(() => setFlow(null))
  }, [storeId])

  useEffect(() => {
    if (!backdrop) return
    let created: string | null = null
    const tok = localStorage.getItem('vg_access_token') ?? ''
    fetch(backdrop.heatmap_url, { headers: { Authorization: `Bearer ${tok}` } })
      .then(r => r.ok ? r.blob() : null)
      .then(b => { if (b) { created = URL.createObjectURL(b); setBackdropUrl(created) } })
      .catch(() => {})
    return () => { if (created) URL.revokeObjectURL(created) }
  }, [backdrop])

  if (!flow) {
    return <Card className="p-8 text-center text-slate-500">
      Loading customer flow…
    </Card>
  }
  if (flow.zones.length === 0 || flow.edges.length === 0) {
    return (
      <Card className="p-8 text-center text-slate-500">
        <div className="text-3xl mb-2">🧭</div>
        <div className="font-medium text-slate-700 mb-1">No customer-flow data yet</div>
        <div className="text-xs text-slate-500 max-w-md mx-auto">
          Flow arrows appear once zones are drawn on each camera AND
          customer-journey tracks have been recorded. The journey
          detector needs at least one entry zone and one destination
          zone to start producing edges.
        </div>
      </Card>
    )
  }

  // Scale arrow width by edge.count. The thickest edge maps to ~6px,
  // the thinnest to ~1px — keeps the diagram readable when the
  // counts span an order of magnitude.
  const maxCount = Math.max(...flow.edges.map(e => e.count), 1)
  const zoneById = Object.fromEntries(flow.zones.map(z => [z.zone_id, z]))

  return (
    <Card className="p-3 bg-[#0d1b2a] text-white">
      <div className="text-xs text-slate-300 mb-2">
        Aggregated over <strong>{flow.total_journeys}</strong> customer journeys.
        Arrows show the number of customers who moved between zones.
      </div>
      <div className="relative w-full bg-[#0a1628] rounded overflow-hidden aspect-video">
        {/* Backdrop: the first camera's heatmap PNG, dimmed so the
            arrows + labels read clearly on top. */}
        {backdropUrl && (
          <img src={backdropUrl} alt=""
               className="absolute inset-0 w-full h-full object-cover opacity-30" />
        )}
        {/* Arrow + label layer. 0..100 viewBox so we can drop
            normalised zone centroids in directly. */}
        <svg viewBox="0 0 100 100" preserveAspectRatio="none"
             className="absolute inset-0 w-full h-full">
          <defs>
            <marker id="flow-arrow" viewBox="0 0 10 10"
                    refX="9" refY="5" markerUnits="strokeWidth"
                    markerWidth="6" markerHeight="6" orient="auto">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#fb923c" />
            </marker>
          </defs>
          {flow.edges.map((e, i) => {
            const a = zoneById[e.from], b = zoneById[e.to]
            if (!a || !b) return null
            const x1 = a.cx * 100, y1 = a.cy * 100
            const x2 = b.cx * 100, y2 = b.cy * 100
            const width = 1 + (e.count / maxCount) * 5
            const opacity = 0.4 + 0.6 * (e.count / maxCount)
            return (
              <line key={i} x1={x1} y1={y1} x2={x2} y2={y2}
                    stroke="#fb923c" strokeWidth={width}
                    opacity={opacity} markerEnd="url(#flow-arrow)" />
            )
          })}
          {flow.zones.map(z => (
            <g key={z.zone_id}>
              <circle cx={z.cx * 100} cy={z.cy * 100} r={1.5}
                      fill="#fb923c" stroke="white" strokeWidth={0.4} />
            </g>
          ))}
        </svg>
        {/* Zone labels — HTML overlay so text doesn't stretch with
            the SVG. Pointer-events:none so it never blocks clicks. */}
        <div className="absolute inset-0 pointer-events-none">
          {flow.zones.map(z => (
            <div key={z.zone_id}
                 className="absolute -translate-x-1/2 translate-y-1.5
                            bg-black/60 text-white text-[10px] px-1.5 py-0.5
                            rounded whitespace-nowrap"
                 style={{
                   left: `${z.cx * 100}%`,
                   top:  `${z.cy * 100}%`,
                 }}>
              {z.name}
            </div>
          ))}
        </div>
      </div>
      {/* Top 5 paths summary */}
      <div className="mt-2 text-xs text-slate-300">
        <div className="font-medium mb-1">Most-travelled paths</div>
        <ul className="space-y-0.5">
          {flow.edges.slice(0, 5).map((e, i) => {
            const a = zoneById[e.from]?.name ?? `Zone ${e.from}`
            const b = zoneById[e.to]?.name ?? `Zone ${e.to}`
            return (
              <li key={i}>
                <strong>{e.count}×</strong> {a} → {b}
              </li>
            )
          })}
        </ul>
      </div>
    </Card>
  )
}
