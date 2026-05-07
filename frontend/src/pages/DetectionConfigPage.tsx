// Per-camera AI settings: detection toggles + sensitivity sliders + simple
// zone draw editor (polygon) on a snapshot of the live feed.

import { useEffect, useMemo, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { Badge, Button, Card, Input, PageHeader, Select } from '@/components/ui/Primitives'
import { cameras as camsApi, type Camera } from '@/api/cameras'

const DETECTION_TYPES = [
  'person', 'vehicle', 'animal', 'lpr', 'face',
  'weapon', 'weapon_brandished', 'crowd',
  'trespass', 'tripwire', 'tailgating', 'loitering',
  'fall', 'smoke', 'fire', 'abandoned_object',
  'shelf', 'occupancy', 'heatmap', 'custom',
]

interface Cfg {
  detection_type: string
  enabled: boolean
  confidence_threshold: number
  min_object_size: number
  detection_every_n_frames: number
  dwell_time_seconds: number | null
  crowd_threshold: number | null
}

export default function DetectionConfigPage() {
  const { id } = useParams()
  const cameraId = Number(id)
  const nav = useNavigate()
  const [camera, setCamera] = useState<Camera | null>(null)
  const [snap, setSnap] = useState<string | null>(null)
  const [config, setConfig] = useState<Record<string, Cfg>>({})
  const [zones, setZones] = useState<any[]>([])
  const [zoneName, setZoneName] = useState('')
  const [zoneTypes, setZoneTypes] = useState<string[]>(['trespass'])
  const [drawing, setDrawing] = useState<number[][]>([])

  useEffect(() => {
    camsApi.get(cameraId).then(setCamera)
    camsApi.snapshot(cameraId).then(s => setSnap(s.jpeg_b64)).catch(() => setSnap(null))
    camsApi.getDetectionConfig(cameraId).then(rows => {
      const m: Record<string, Cfg> = {}
      DETECTION_TYPES.forEach(t => {
        const found = rows.find((r: any) => r.detection_type === t)
        m[t] = found ?? { detection_type: t, enabled: false, confidence_threshold: 0.6,
          min_object_size: 0, detection_every_n_frames: 1, dwell_time_seconds: null, crowd_threshold: null }
      })
      setConfig(m)
    })
    camsApi.listZones(cameraId).then(setZones)
  }, [cameraId])

  async function saveConfig() {
    const payload = Object.values(config)
    await camsApi.upsertDetectionConfig(cameraId, payload)
    alert('Saved.')
  }

  // Add a click-to-place point to the in-progress polygon.
  function clickSnapshot(e: React.MouseEvent<HTMLImageElement>) {
    const r = e.currentTarget.getBoundingClientRect()
    const x = (e.clientX - r.left) / r.width
    const y = (e.clientY - r.top)  / r.height
    setDrawing(d => [...d, [x, y]])
  }

  async function saveZone() {
    if (drawing.length < 3 && zoneTypes.includes('tripwire') === false) {
      alert('Polygon needs at least 3 points'); return
    }
    await camsApi.createZone(cameraId, {
      name: zoneName || `Zone ${zones.length + 1}`,
      shape: zoneTypes.includes('tripwire') && drawing.length === 2 ? 'line' : 'polygon',
      polygon_coords_json: drawing,
      detection_types_json: zoneTypes,
      suppressed: false,
    })
    setZones(await camsApi.listZones(cameraId))
    setDrawing([])
    setZoneName('')
  }

  async function deleteZone(zid: number) {
    await camsApi.deleteZone(cameraId, zid)
    setZones(await camsApi.listZones(cameraId))
  }

  return (
    <div className="p-6">
      <PageHeader
        title={`AI settings — ${camera?.name ?? '…'}`}
        actions={<>
          <Button variant="ghost" onClick={() => nav('/cameras')}>Back</Button>
          <Button onClick={saveConfig}>Save</Button>
        </>}
      />

      <div className="grid grid-cols-12 gap-4">
        {/* Detection toggles */}
        <Card className="col-span-7 p-4">
          <div className="font-medium mb-3">Detection types</div>
          <div className="grid grid-cols-2 gap-2">
            {DETECTION_TYPES.map(t => {
              const c = config[t]; if (!c) return null
              return (
                <div key={t} className="border rounded p-3">
                  <label className="flex items-center gap-2">
                    <input type="checkbox" checked={c.enabled}
                           onChange={e => setConfig({ ...config, [t]: { ...c, enabled: e.target.checked }})} />
                    <span className="font-medium capitalize">{t.replace('_', ' ')}</span>
                  </label>
                  {c.enabled && (
                    <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
                      <label>Confidence
                        <Input type="number" step="0.05" min="0.1" max="0.99" value={c.confidence_threshold}
                               onChange={e => setConfig({ ...config, [t]: { ...c, confidence_threshold: Number(e.target.value) }})} />
                      </label>
                      <label>Skip every N
                        <Input type="number" min="1" value={c.detection_every_n_frames}
                               onChange={e => setConfig({ ...config, [t]: { ...c, detection_every_n_frames: Number(e.target.value) }})} />
                      </label>
                      {(t === 'loitering' || t === 'abandoned_object' || t === 'tailgating') && (
                        <label>Dwell (sec)
                          <Input type="number" value={c.dwell_time_seconds ?? ''}
                                 onChange={e => setConfig({ ...config, [t]: { ...c, dwell_time_seconds: e.target.value ? Number(e.target.value) : null }})} />
                        </label>
                      )}
                      {(t === 'crowd' || t === 'occupancy') && (
                        <label>Threshold
                          <Input type="number" value={c.crowd_threshold ?? ''}
                                 onChange={e => setConfig({ ...config, [t]: { ...c, crowd_threshold: e.target.value ? Number(e.target.value) : null }})} />
                        </label>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </Card>

        {/* Zone editor */}
        <Card className="col-span-5 p-4">
          <div className="font-medium mb-3">Zones</div>
          <div className="text-xs text-slate-500 mb-2">
            Click on the snapshot to drop polygon points. ≥ 3 for an area, 2 for a tripwire line.
          </div>
          {snap ? (
            <div className="relative">
              <img src={`data:image/jpeg;base64,${snap}`}
                   onClick={clickSnapshot}
                   className="w-full rounded border cursor-crosshair" alt="" />
              {/* Render in-progress polygon */}
              <svg className="absolute inset-0 w-full h-full pointer-events-none"
                   viewBox="0 0 100 100" preserveAspectRatio="none">
                {drawing.map((p, i) => (
                  <circle key={i} cx={p[0] * 100} cy={p[1] * 100} r="0.6" fill="#f59e0b" />
                ))}
                {drawing.length >= 2 && (
                  <polyline points={drawing.map(p => `${p[0]*100},${p[1]*100}`).join(' ')}
                            fill="none" stroke="#f59e0b" strokeWidth="0.5" />
                )}
              </svg>
            </div>
          ) : (
            <div className="bg-slate-200 aspect-video rounded flex items-center justify-center text-slate-500">
              Snapshot unavailable
            </div>
          )}

          <div className="mt-3 grid grid-cols-1 gap-2">
            <Input placeholder="Zone name" value={zoneName} onChange={e => setZoneName(e.target.value)} />
            <Select multiple value={zoneTypes}
                    onChange={e => setZoneTypes(Array.from(e.target.selectedOptions).map(o => o.value))}>
              {DETECTION_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
            </Select>
            <div className="flex gap-2">
              <Button onClick={saveZone} disabled={drawing.length < 2}>Save zone</Button>
              <Button variant="ghost" onClick={() => setDrawing([])}>Reset</Button>
            </div>
          </div>

          <div className="mt-4 space-y-1">
            {zones.map(z => (
              <div key={z.id} className="flex items-center justify-between text-sm border-t pt-1">
                <div>
                  <span className="font-medium">{z.name}</span>
                  <span className="ml-2 text-slate-500 text-xs">{z.shape}</span>
                  <span className="ml-2">
                    {(z.detection_types_json || []).map((t: string) => <Badge key={t} color="sky">{t}</Badge>)}
                  </span>
                </div>
                <button className="text-red-600 hover:underline" onClick={() => deleteZone(z.id)}>×</button>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  )
}
