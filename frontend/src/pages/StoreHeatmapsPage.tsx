// Composite heatmap grid for a whole store (Rule 5).
// One tile per camera with its colourised heatmap overlay, the top
// hotspot labelled ("Busiest area" etc.) and a download button.

import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Badge, Button, Card, PageHeader, Skeleton } from '@/components/ui/Primitives'
import { api } from '@/api/client'

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
  const [bust, setBust] = useState(0)

  useEffect(() => {
    api<{ cameras: CameraHeatmap[] }>(`/analytics/store/${storeId}/heatmaps`)
      .then(setData).catch(console.error)
    const t = setInterval(() => setBust(b => b + 1), 60_000)
    return () => clearInterval(t)
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
        {data.cameras.map(c => (
          <Card key={c.camera_id} className="p-3">
            <div className="flex items-center justify-between mb-2">
              <div className="font-medium truncate">{c.camera_name}</div>
              <Badge color={c.status === 'online' ? 'green' : 'amber'}>{c.status}</Badge>
            </div>
            <div className="relative bg-slate-900 rounded overflow-hidden">
              <img src={`/api/cameras/${c.camera_id}/snapshot`} alt=""
                   onError={(e) => ((e.target as HTMLImageElement).style.display = 'none')}
                   className="w-full" />
              <img src={`${c.heatmap_url}&_=${bust}`} alt=""
                   onError={(e) => ((e.target as HTMLImageElement).style.display = 'none')}
                   className="absolute inset-0 w-full h-full pointer-events-none mix-blend-screen" />
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
            <div className="mt-2 flex justify-between items-center text-xs text-slate-500">
              <span>{c.hotspot ? `${c.hotspot.value} hits at brightest cell` : 'No data yet'}</span>
              <a href={c.heatmap_url} download={`heatmap_${c.camera_id}.png`}
                 className="text-sky-600 hover:underline">Download</a>
            </div>
          </Card>
        ))}
      </div>
    </div>
  )
}
