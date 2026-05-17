// Composite heatmap grid for a whole store (Rule 5).
// One tile per camera with its colourised heatmap overlay, the top
// hotspot labelled ("Busiest area" etc.) and a download button.

import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Badge, Card, PageHeader, Skeleton } from '@/components/ui/Primitives'
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

export default function StoreHeatmapsPage() {
  const { id } = useParams()
  const storeId = Number(id)
  const [data, setData] = useState<{ cameras: CameraHeatmap[] } | null>(null)

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
        actions={<Link to={`/stores/${storeId}`} className="text-sky-600 hover:underline self-center">← Store dashboard</Link>}
      />

      {data.cameras.length === 0 && (
        <Card className="p-8 text-center text-slate-500">No cameras in this store yet.</Card>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {data.cameras.map(c => <HeatmapCard key={c.camera_id} c={c} />)}
      </div>
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
