// Per-camera footfall heatmap viewer — overlays the colourised
// heatmap PNG (from /analytics/heatmap/{id}/image) on the camera
// snapshot. Use the opacity slider to tune the blend; click Download
// to grab the overlay as a standalone PNG.
//
// Both the overlay <img> and the Download click fetch the PNG via
// `fetch(..., { Authorization: Bearer <jwt> })` and convert the blob
// to an object URL. Earlier code used a bare <img src> / <a href>
// which the browser issues without the JWT — the API returned 401 and
// Chrome reported "file wasn't available on site" for downloads.

import { useEffect, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { Button, Card, PageHeader } from '@/components/ui/Primitives'
import { cameras } from '@/api/cameras'

export default function HeatmapPage() {
  const { id } = useParams()
  const cameraId = Number(id)

  const [snap, setSnap] = useState<string | null>(null)
  const [heatmapObjUrl, setHeatmapObjUrl] = useState<string | null>(null)
  const [opacity, setOpacity] = useState(0.65)
  const [bust, setBust] = useState(0)              // forces refresh every 30s + on Refresh click
  const [error, setError] = useState<string | null>(null)

  // Snapshot — JSON endpoint returns base64; no auth issue with the api wrapper.
  useEffect(() => {
    cameras.snapshot(cameraId)
      .then(s => setSnap(s.jpeg_b64))
      .catch(e => setError(String(e)))
    const t = setInterval(() => setBust(b => b + 1), 30_000)
    return () => clearInterval(t)
  }, [cameraId])

  // Heatmap PNG — authed fetch → blob → object URL.
  useEffect(() => {
    let cancelled = false
    let created: string | null = null
    const tok = localStorage.getItem('vg_access_token') ?? ''
    fetch(`/api/analytics/heatmap/${cameraId}/image?alpha=${opacity}`,
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
  }, [cameraId, opacity, bust])

  async function download() {
    const tok = localStorage.getItem('vg_access_token') ?? ''
    try {
      const res = await fetch(`/api/analytics/heatmap/${cameraId}/image?alpha=${opacity}`,
                              { headers: { Authorization: `Bearer ${tok}` } })
      if (!res.ok) {
        setError(`Download failed: ${res.status} ${res.statusText}`)
        return
      }
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `heatmap_camera_${cameraId}.png`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (e) {
      setError(String(e))
    }
  }

  return (
    <div className="p-6">
      <PageHeader
        title={`Footfall heatmap — camera #${cameraId}`}
        actions={<>
          <Link to="/cameras"><Button variant="ghost">Cameras</Button></Link>
          <Button onClick={download} disabled={!heatmapObjUrl}>Download PNG</Button>
        </>}
      />

      <Card className="p-3 mb-3 flex items-center gap-3">
        <span className="text-sm text-slate-600">Overlay opacity</span>
        <input type="range" min={0} max={1} step={0.05}
               value={opacity} onChange={e => setOpacity(Number(e.target.value))}
               className="flex-1" />
        <span className="text-sm w-12 text-right">{Math.round(opacity * 100)}%</span>
        <Button variant="ghost" onClick={() => setBust(b => b + 1)}>Refresh</Button>
      </Card>

      <Card className="p-3">
        <div className="relative w-full bg-slate-900">
          {snap ? (
            <img src={`data:image/jpeg;base64,${snap}`}
                 className="block w-full h-auto" alt="camera snapshot" />
          ) : (
            <div className="aspect-video flex items-center justify-center text-slate-400">
              {error ?? 'Loading snapshot…'}
            </div>
          )}
          {heatmapObjUrl && (
            <img src={heatmapObjUrl} alt=""
                 className="absolute inset-0 w-full h-full pointer-events-none mix-blend-screen" />
          )}
        </div>
        <div className="text-xs text-slate-500 mt-2">
          Heatmap updates every 30 seconds from the inference worker.
          Enable <code>heatmap</code> in this camera's AI settings to start
          accumulating data.
        </div>
        {error && <div className="text-red-600 text-sm mt-2">{error}</div>}
      </Card>
    </div>
  )
}
