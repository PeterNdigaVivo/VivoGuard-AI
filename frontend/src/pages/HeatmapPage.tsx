// Per-camera footfall heatmap viewer — overlays the colourised
// heatmap PNG (from /analytics/heatmap/{id}/image) on the camera
// snapshot. Use the opacity slider to tune the blend; click Download
// to grab the overlay as a standalone PNG (useful for slide decks).

import { useEffect, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { Button, Card, PageHeader } from '@/components/ui/Primitives'
// (api import removed — download() now uses an anchor)
import { cameras } from '@/api/cameras'

export default function HeatmapPage() {
  const { id } = useParams()
  const cameraId = Number(id)

  const [snap, setSnap] = useState<string | null>(null)
  const [opacity, setOpacity] = useState(0.65)
  const [bust, setBust] = useState(0)              // forces image refresh
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    cameras.snapshot(cameraId)
      .then(s => setSnap(s.jpeg_b64))
      .catch(e => setError(String(e)))
    const t = setInterval(() => setBust(b => b + 1), 30000)
    return () => clearInterval(t)
  }, [cameraId])

  const heatmapUrl = `/api/analytics/heatmap/${cameraId}/image?alpha=${opacity}&_=${bust}`

  function download() {
    // Plain anchor click — the browser handles the GET, the Authorization
    // header rides on the same-origin cookie/session. No fetch-to-Blob
    // detour and no TS cast.
    const a = document.createElement('a')
    a.href = `/api/analytics/heatmap/${cameraId}/image?alpha=${opacity}`
    a.download = `heatmap_camera_${cameraId}.png`
    a.click()
  }

  return (
    <div className="p-6">
      <PageHeader
        title={`Footfall heatmap — camera #${cameraId}`}
        actions={<>
          <Link to="/cameras"><Button variant="ghost">Cameras</Button></Link>
          <Button onClick={download}>Download PNG</Button>
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
          {/* Heatmap overlay — falls back silently if /image returns 404. */}
          <img src={heatmapUrl}
               className="absolute inset-0 w-full h-full pointer-events-none mix-blend-screen"
               alt="" onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }} />
        </div>
        <div className="text-xs text-slate-500 mt-2">
          Heatmap updates every 30 seconds from the inference worker.
          Enable <code>heatmap</code> in this camera's AI settings to start
          accumulating data.
        </div>
      </Card>
    </div>
  )
}
