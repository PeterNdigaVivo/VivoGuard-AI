// Multi-camera store view (Feature 3) — aggregated TODAY footfall for a
// store plus a grid of its entrance cameras (all cameras when none are
// entry_exit-tagged), each tile carrying the live DetBoxes overlay from
// the Live View feature. Data refreshes every 30s; overlays at 1Hz via
// the shared useLiveDetections hook. Snapshot-based tiles (no per-tile
// WebSockets) keep the page light for 9-camera stores.
import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Badge, Card, PageHeader, Skeleton } from '@/components/ui/Primitives'
import { api } from '@/api/client'
import { DetBoxes, useLiveDetections } from '@/pages/LiveViewPage'

const REFRESH_MS = 30_000

interface CamRow {
  camera_id: number; name: string; status: string
  entrance: boolean; in: number; out: number; occupancy: number
}
interface MultiCam {
  store_id: number; store_name: string; as_of: string
  totals: { in: number; out: number; net: number; occupancy: number }
  cameras: CamRow[]
}

export default function StoreMultiCameraView() {
  const { id } = useParams()
  const [data, setData] = useState<MultiCam | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!id) return
    let alive = true
    const load = () => api<MultiCam>(`/analytics/store/${id}/multi-camera`)
      .then(d => { if (alive) { setData(d); setErr(null) } })
      .catch(e => { if (alive) setErr(String(e)) })
    load()
    const t = setInterval(load, REFRESH_MS)
    return () => { alive = false; clearInterval(t) }
  }, [id])

  const camIds = useMemo(() => (data?.cameras ?? []).map(c => c.camera_id), [data])
  const liveDets = useLiveDetections(camIds)

  if (err) return (
    <div className="p-6"><PageHeader title="Multi-Camera View" />
      <Card className="p-6 text-sm text-red-600">Could not load. {err}</Card></div>
  )
  if (!data) return (
    <div className="p-6"><PageHeader title="Multi-Camera View" />
      <div className="grid gap-3 md:grid-cols-2">
        {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-56" />)}
      </div></div>
  )

  const n = data.cameras.length
  const cols = n <= 4 ? 'grid-cols-1 sm:grid-cols-2' : 'grid-cols-2 xl:grid-cols-3'

  return (
    <div className="p-6">
      <PageHeader
        title={`${data.store_name} — Multi-Camera View`}
        actions={
          <Link to={`/stores/${data.store_id}`}
                className="text-sm text-sky-600 hover:underline">← Store</Link>
        }
      />

      {/* Aggregated header — today's footfall across the shown cameras. */}
      <Card className="p-4 mb-4 flex flex-wrap items-center gap-x-6 gap-y-2">
        <div className="text-sm font-semibold text-slate-800 dark:text-slate-100">
          Total Footfall today:
          <span className="text-emerald-600"> {data.totals.in} in</span>
          {' / '}<span className="text-amber-600">{data.totals.out} out</span>
        </div>
        <div className="text-sm text-slate-700 dark:text-slate-200">
          Net: <strong>{data.totals.net} visitors</strong>
        </div>
        <div className="text-sm text-slate-700 dark:text-slate-200">
          👥 Current occupancy: <strong>{data.totals.occupancy}</strong>
        </div>
        <span className="ml-auto text-xs text-slate-500 dark:text-slate-400">
          {n} camera{n === 1 ? '' : 's'} · auto-refresh 30s ·
          updated {new Date(data.as_of).toLocaleTimeString()}
        </span>
      </Card>

      {n === 0 ? (
        <Card className="p-8 text-center text-slate-500 dark:text-slate-300">
          No cameras attached to this store.
        </Card>
      ) : (
        <div className={`grid gap-3 ${cols}`}>
          {data.cameras.map(cam => (
            <CamTile key={cam.camera_id} cam={cam}
                     dets={liveDets[cam.camera_id] ?? []} />
          ))}
        </div>
      )}
    </div>
  )
}

function CamTile({ cam, dets }: {
  cam: CamRow
  dets: Parameters<typeof DetBoxes>[0]['dets']
}) {
  const [src, setSrc] = useState<string | null>(null)
  // Snapshot refreshes on the parent's 30s data cycle (cam object
  // identity changes per fetch) — no extra timers per tile.
  useEffect(() => {
    let alive = true
    api<{ jpeg_b64: string }>(`/cameras/${cam.camera_id}/snapshot`)
      .then(r => { if (alive) setSrc(`data:image/jpeg;base64,${r.jpeg_b64}`) })
      .catch(() => { if (alive) setSrc(null) })
    return () => { alive = false }
  }, [cam])

  return (
    <Card className="p-2">
      <div className="flex items-center justify-between mb-1 text-xs">
        <div className="truncate flex-1 font-medium text-slate-800 dark:text-slate-100">
          {cam.name}
          {cam.entrance && (
            <span className="ml-1 text-[10px] text-sky-600">entrance</span>
          )}
        </div>
        <Badge color={cam.status === 'online' ? 'green' : 'amber'}>{cam.status}</Badge>
      </div>
      <div className="relative bg-black rounded overflow-hidden aspect-video">
        {src
          ? <img src={src} alt={cam.name} className="block w-full h-full object-cover" />
          : <div className="absolute inset-0 flex items-center justify-center
                            text-slate-400 text-xs">Snapshot unavailable</div>}
        <DetBoxes dets={dets} />
      </div>
      <div className="mt-1.5 text-xs text-slate-600 dark:text-slate-300 flex gap-4">
        <span>In: <strong className="text-emerald-600">{cam.in}</strong></span>
        <span>Out: <strong className="text-amber-600">{cam.out}</strong></span>
        <span>Current: <strong>{cam.occupancy}</strong></span>
      </div>
    </Card>
  )
}
