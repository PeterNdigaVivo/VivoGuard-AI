// Operator-friendly zone setup. No technical strings on screen.
//
// Steps:
//   1. Pick a purpose from a plain-English dropdown
//      ("Count people entering", "Monitor checkout queue", …)
//   2. Click on the snapshot to drop polygon (or line) points
//   3. Name the zone, save
//
// Backend persists with the right detection_types_json + auto-enables
// the matching detector.

import { useEffect, useState } from 'react'
import { Link, useParams, useNavigate } from 'react-router-dom'
import { Badge, Button, Card, Input, PageHeader, Select } from '@/components/ui/Primitives'
import { api } from '@/api/client'
import { cameras as camsApi } from '@/api/cameras'

interface Purpose {
  label: string
  shape: 'polygon' | 'line'
  types: string[]
  description: string
}

interface ZoneRow {
  id: number; name: string; shape: string
  detection_types_json: string[]
  polygon_coords_json: [number, number][]
}

export default function CameraSetupPage() {
  const { id } = useParams()
  const cameraId = Number(id)
  const nav = useNavigate()

  const [snap, setSnap] = useState<string | null>(null)
  const [purposes, setPurposes] = useState<Record<string, Purpose>>({})
  const [zones, setZones] = useState<ZoneRow[]>([])
  const [purposeKey, setPurposeKey] = useState<string>('count_entries')
  const [name, setName] = useState('')
  const [points, setPoints] = useState<[number, number][]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api<Record<string, Purpose>>('/zone-purposes').then(setPurposes).catch(console.error)
    camsApi.snapshot(cameraId).then(s => setSnap(s.jpeg_b64)).catch(() => setSnap(null))
    camsApi.listZones(cameraId).then(setZones).catch(console.error)
  }, [cameraId])

  const purpose = purposes[purposeKey]
  const pointsLimit = purpose?.shape === 'line' ? 2 : Infinity

  function onClick(e: React.MouseEvent<HTMLImageElement>) {
    const r = e.currentTarget.getBoundingClientRect()
    const x = (e.clientX - r.left) / r.width
    const y = (e.clientY - r.top) / r.height
    setPoints(p => p.length >= pointsLimit ? [[x, y]] : [...p, [x, y]])
  }

  async function save() {
    if (!purpose) return
    const minPoints = purpose.shape === 'line' ? 2 : 3
    if (points.length < minPoints) {
      setError(`Need at least ${minPoints} points for ${purpose.shape}`); return
    }
    setBusy(true); setError(null)
    try {
      await api('/cameras/' + cameraId + '/zones/by-purpose', {
        method: 'POST',
        body: {
          name: name || purpose.label,
          purpose: purposeKey,
          polygon_coords_json: points,
        },
      })
      setName(''); setPoints([])
      setZones(await camsApi.listZones(cameraId))
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function deleteZone(zid: number) {
    if (!confirm('Remove this zone?')) return
    await camsApi.deleteZone(cameraId, zid)
    setZones(await camsApi.listZones(cameraId))
  }

  function purposeLabelOf(types: string[]): string {
    const match = Object.values(purposes).find(p => types.includes(p.types[0]))
    return match?.label ?? types.join(', ')
  }

  return (
    <div className="p-6">
      <PageHeader
        title="Camera setup"
        actions={<>
          <Button variant="ghost" onClick={() => nav('/cameras')}>Back to cameras</Button>
          <Link to={`/cameras/${cameraId}/detection`}
                className="text-sm text-slate-500 hover:underline self-center">Advanced settings →</Link>
        </>}
      />

      <div className="grid grid-cols-12 gap-4">
        {/* Live snapshot + draw */}
        <Card className="col-span-8 p-4">
          <div className="text-sm text-slate-600 mb-2">
            <ol className="list-decimal list-inside space-y-1">
              <li>Choose what this zone should do.</li>
              <li>Click on the snapshot to drop points (
                {purpose?.shape === 'line' ? 'two points for a line' : 'three or more for a polygon'}
                ).</li>
              <li>Name it and save.</li>
            </ol>
          </div>

          {snap ? (
            <div className="relative inline-block bg-slate-200 rounded overflow-hidden cursor-crosshair">
              <img src={`data:image/jpeg;base64,${snap}`}
                   onClick={onClick}
                   className="block max-w-full h-auto" alt="" />
              <svg className="absolute inset-0 w-full h-full pointer-events-none"
                   viewBox="0 0 100 100" preserveAspectRatio="none">
                {points.length > 1 && purpose?.shape === 'polygon' && (
                  <polygon points={points.map(p => `${p[0]*100},${p[1]*100}`).join(' ')}
                           fill="rgba(245,158,11,0.2)" stroke="#f59e0b" strokeWidth="0.4" />
                )}
                {points.length === 2 && purpose?.shape === 'line' && (
                  <line x1={points[0][0]*100} y1={points[0][1]*100}
                        x2={points[1][0]*100} y2={points[1][1]*100}
                        stroke="#f59e0b" strokeWidth="0.7" />
                )}
                {points.map((p, i) => (
                  <circle key={i} cx={p[0]*100} cy={p[1]*100} r="0.7" fill="#f59e0b" />
                ))}
              </svg>
            </div>
          ) : (
            <div className="bg-slate-200 aspect-video rounded flex items-center justify-center text-slate-500">
              No snapshot available.
            </div>
          )}
        </Card>

        {/* Right panel: purpose + name + save + existing zones */}
        <div className="col-span-4 space-y-3">
          <Card className="p-4">
            <div className="text-sm font-medium mb-2">1. What should this zone do?</div>
            <Select value={purposeKey} onChange={e => { setPurposeKey(e.target.value); setPoints([]) }}
                    className="w-full">
              {Object.entries(purposes).map(([key, p]) => (
                <option key={key} value={key}>{p.label}</option>
              ))}
            </Select>
            {purpose && (
              <div className="mt-2 text-xs text-slate-500">{purpose.description}</div>
            )}

            <div className="text-sm font-medium mt-4 mb-2">2. Name it</div>
            <Input value={name} placeholder={purpose?.label ?? 'Zone name'}
                   onChange={e => setName(e.target.value)} className="w-full" />

            <div className="mt-4 flex gap-2">
              <Button onClick={save}
                      disabled={busy || (purpose?.shape === 'polygon' ? points.length < 3 : points.length < 2)}>
                {busy ? 'Saving…' : 'Save zone'}
              </Button>
              <Button variant="ghost" onClick={() => setPoints([])} disabled={!points.length}>
                Reset
              </Button>
            </div>
            {error && <div className="text-red-600 text-sm mt-2">{error}</div>}
          </Card>

          <Card className="p-4">
            <div className="text-sm font-medium mb-2">Existing zones on this camera</div>
            {zones.length === 0 && <div className="text-slate-400 text-sm">None yet.</div>}
            {zones.map(z => (
              <div key={z.id} className="flex items-center justify-between text-sm py-1 border-t first:border-t-0">
                <div>
                  <div className="font-medium">{z.name}</div>
                  <div className="text-xs text-slate-500">{purposeLabelOf(z.detection_types_json)}</div>
                </div>
                <button className="text-red-600 hover:underline text-xs"
                        onClick={() => deleteZone(z.id)}>Remove</button>
              </div>
            ))}
          </Card>
        </div>
      </div>
    </div>
  )
}
