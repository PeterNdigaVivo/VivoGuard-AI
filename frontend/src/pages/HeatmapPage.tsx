// Per-camera footfall heatmap viewer — Premier League / Opta style.
//
// Overlays the colourised heatmap PNG (from /analytics/heatmap/{id}/
// image) on the camera snapshot. Use the opacity slider to tune the
// blend; click Download to grab the overlay as a standalone PNG.
//
// Visual style:
//   • Dark navy chrome — matches the navy floor of the heatmap and
//     keeps focus on the orange hotspot.
//   • Sharp banding rather than smooth gradient (server side).
//   • Hotspot annotation badge ("🔥 Busiest Area") in the corner.
//   • Legend bar showing the cool→hot ramp.
//   • Time-window selector (Last hour / Today / This week) so
//     non-technical operators can change scope without surveying SQL.
//
// Both the overlay <img> and the Download click fetch the PNG via
// `fetch(..., { Authorization: Bearer <jwt> })` and convert the blob
// to an object URL.

import { useEffect, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { Button, Card, PageHeader } from '@/components/ui/Primitives'
import { cameras } from '@/api/cameras'

type TimeWindow = 'last_hour' | 'today' | 'this_week'

export default function HeatmapPage() {
  const { id } = useParams()
  const cameraId = Number(id)

  const [snap, setSnap] = useState<string | null>(null)
  const [heatmapObjUrl, setHeatmapObjUrl] = useState<string | null>(null)
  // Default 0.85 — the new colour scheme is dark enough that the
  // snapshot still reads through clearly.
  const [opacity, setOpacity] = useState(0.85)
  const [bust, setBust] = useState(0)              // forces refresh every 30s + on Refresh click
  const [windowSel, setWindowSel] = useState<TimeWindow>('today')
  const [error, setError] = useState<string | null>(null)

  // Snapshot — JSON endpoint returns base64; no auth issue with the api wrapper.
  useEffect(() => {
    cameras.snapshot(cameraId)
      .then(s => setSnap(s.jpeg_b64))
      .catch(e => setError(String(e)))
    const t = setInterval(() => setBust(b => b + 1), 30_000)
    return () => clearInterval(t)
  }, [cameraId])

  // Build the heatmap image URL with the current window selection
  // baked in. The server keeps three independent accumulators
  // (vg:heatmap:hour|day|week:{id}) so switching this query param
  // gets a fresh grid with no client-side filtering.
  function heatmapUrl(forDownload = false): string {
    const win = windowSel === 'last_hour' ? 'hour'
              : windowSel === 'this_week' ? 'week'
              : 'day'
    const params = new URLSearchParams({
      alpha: String(forDownload ? Math.max(0.85, opacity) : opacity),
      window: win,
    })
    return `/api/analytics/heatmap/${cameraId}/image?${params}`
  }

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
    // Re-fetch on window change too — that's the whole point.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cameraId, opacity, bust, windowSel])

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

      {/* Time-window selector — football-analytics style: large, bold,
          unmistakeable. Wired up visually now; server-side time
          windowing on the heatmap accumulator is a separate change. */}
      <Card className="p-3 mb-3 bg-slate-900 text-white border-slate-800">
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-xs uppercase tracking-wide text-slate-400">Time window</span>
          {(['last_hour', 'today', 'this_week'] as TimeWindow[]).map(w => (
            <button key={w}
                    onClick={() => setWindowSel(w)}
                    title={w === 'last_hour' ? 'Rolling last 60 minutes — resets every hour'
                         : w === 'today'     ? 'Since local midnight today'
                         : 'Since the start of this iso-week (Mon 00:00)'}
                    className={'px-3 py-1 rounded text-xs font-medium ' +
                      (windowSel === w
                        ? 'bg-orange-500 text-white'
                        : 'bg-slate-800 text-slate-300 hover:bg-slate-700')}>
              {w === 'last_hour' ? 'Last hour'
                : w === 'today'   ? 'Today'
                : 'This week'}
            </button>
          ))}
          <div className="flex-1" />
          <span className="text-xs text-slate-400">Opacity</span>
          <input type="range" min={0} max={1} step={0.05}
                 value={opacity} onChange={e => setOpacity(Number(e.target.value))}
                 className="w-32" />
          <span className="text-xs w-10 text-right">{Math.round(opacity * 100)}%</span>
          <Button variant="ghost" onClick={() => setBust(b => b + 1)}>Refresh</Button>
        </div>
      </Card>

      {/* Heatmap canvas — dark navy backdrop for football-analytics
          look. Mix-blend-screen keeps the snapshot visible through the
          orange hotspots. */}
      <Card className="p-3 bg-[#0d1b2a] text-white border-slate-800">
        <div className="relative w-full bg-[#0a1628] rounded overflow-hidden">
          {snap ? (
            <img src={`data:image/jpeg;base64,${snap}`}
                 className="block w-full h-auto opacity-90" alt="camera snapshot" />
          ) : (
            <div className="aspect-video flex items-center justify-center text-slate-500">
              {error ?? 'Loading snapshot…'}
            </div>
          )}
          {heatmapObjUrl && (
            <img src={heatmapObjUrl} alt=""
                 className="absolute inset-0 w-full h-full pointer-events-none mix-blend-screen" />
          )}
          {/* Hotspot annotation — pulses for emphasis. */}
          {heatmapObjUrl && (
            <div className="absolute top-3 right-3 px-2.5 py-1 rounded bg-black/70 text-white text-xs font-semibold
                            animate-pulse pointer-events-none">
              🔥 Peak activity zone
            </div>
          )}
        </div>

        {/* Legend bar — cool → hot. Matches the server-side stops. */}
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
