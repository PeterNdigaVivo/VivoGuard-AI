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
import { Badge, Button, Card, Input, PageHeader, Select, useToast } from '@/components/ui/Primitives'
import { api } from '@/api/client'
import { cameras as camsApi, type Camera } from '@/api/cameras'

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
  const toast = useToast()

  const [cam, setCam] = useState<Camera | null>(null)
  const [snap, setSnap] = useState<string | null>(null)
  const [purposes, setPurposes] = useState<Record<string, Purpose>>({})
  const [zones, setZones] = useState<ZoneRow[]>([])
  const [purposeKey, setPurposeKey] = useState<string>('count_entries')
  const [name, setName] = useState('')
  const [points, setPoints] = useState<[number, number][]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // Transport switch — flip RTSP ↔ HTTP-snapshot without leaving the page.
  async function setTransport(mode: 'rtsp' | 'http_snapshot') {
    try {
      const updated = await camsApi.update(cameraId, { transport: mode })
      setCam(updated as Camera)
      toast.push(`Transport → ${mode === 'rtsp' ? 'RTSP (port 554)' : 'HTTP snapshot (CGI)'}`)
    } catch (e) {
      toast.push(String(e), 'err')
    }
  }

  // -rtsp_transport flag selector: 'tcp' (direct) vs 'http' (tunnel).
  async function setRtspTransport(mode: 'tcp' | 'http' | 'udp') {
    try {
      const updated = await camsApi.update(cameraId, { rtsp_transport: mode })
      setCam(updated as Camera)
      toast.push(`RTSP transport → ${mode.toUpperCase()}`)
    } catch (e) {
      toast.push(String(e), 'err')
    }
  }

  // One-click: probe alt RTSP ports (TCP then HTTP tunnel) and persist
  // a working one if found.
  const [tryingPorts, setTryingPorts] = useState(false)
  const [portResult, setPortResult] = useState<any>(null)
  async function tryAlternatePorts() {
    setTryingPorts(true); setPortResult(null)
    try {
      const r = await api(`/cameras/${cameraId}/try-alternate-ports`, { method: 'POST' })
      setPortResult(r)
      if ((r as any).ok) {
        toast.push((r as any).summary)
        camsApi.get(cameraId).then(setCam)
      } else {
        toast.push((r as any).summary, 'err')
      }
    } catch (e) {
      toast.push(String(e), 'err')
    } finally {
      setTryingPorts(false)
    }
  }

  useEffect(() => {
    api<Record<string, Purpose>>('/zone-purposes').then(setPurposes).catch(console.error)
    camsApi.get(cameraId).then(setCam).catch(() => setCam(null))
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
                className="text-sm text-slate-500 dark:text-slate-300 hover:underline self-center">Advanced settings →</Link>
        </>}
      />

      <div className="grid grid-cols-12 gap-4">
        {/* Live snapshot + draw */}
        <Card className="col-span-8 p-4">
          <div className="text-sm text-slate-600 dark:text-slate-300 mb-2">
            <ol className="list-decimal list-inside space-y-1">
              <li>Choose what this zone should do.</li>
              <li>Click on the snapshot to drop points (
                {purpose?.shape === 'line' ? 'two points for a line' : 'three or more for a polygon'}
                ).</li>
              <li>Name it and save.</li>
            </ol>
          </div>

          {snap ? (
            <div className="relative inline-block bg-slate-200 dark:bg-slate-800 rounded overflow-hidden cursor-crosshair">
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
            <div className="bg-slate-200 dark:bg-slate-800 aspect-video rounded flex items-center justify-center text-slate-500 dark:text-slate-300">
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
              <div className="mt-2 text-xs text-slate-500 dark:text-slate-300">{purpose.description}</div>
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

          {/* Transport switch — surfaces the RTSP↔HTTP-snapshot toggle
              for stores where port 554 isn't forwarded. */}
          {cam && (
            <Card className="p-4">
              <div className="text-sm font-medium mb-2">How to fetch frames</div>
              <Select className="w-full" value={cam.transport ?? 'rtsp'}
                      onChange={e => setTransport(e.target.value as 'rtsp' | 'http_snapshot')}>
                <option value="rtsp">RTSP video — default, smoother</option>
                <option value="http_snapshot">HTTP snapshot polling — last-resort fallback</option>
              </Select>

              {/* RTSP transport flag — relevant only when transport=='rtsp' */}
              {cam.transport !== 'http_snapshot' && (
                <>
                  <div className="text-sm font-medium mt-3 mb-2">RTSP transport</div>
                  <Select className="w-full" value={cam.rtsp_transport ?? 'tcp'}
                          onChange={e => setRtspTransport(e.target.value as any)}>
                    <option value="tcp">TCP (default)</option>
                    <option value="http">HTTP tunnel (use when 554 is blocked)</option>
                    <option value="udp">UDP (advanced)</option>
                  </Select>
                  <div className="text-xs text-slate-500 dark:text-slate-300 mt-1">
                    HTTP tunnel routes RTSP through the HTTP port
                    ({cam.rtsp_port}) so it works on routers that block 554.
                  </div>
                </>
              )}

              <div className="text-xs text-slate-500 dark:text-slate-300 mt-3">
                {cam.transport === 'http_snapshot' ? (
                  <>
                    Polling{' '}
                    <code className="bg-slate-100 dark:bg-slate-800 px-1">
                      /cgi-bin/snapshot.cgi?channel={cam.channel_number ?? 1}
                    </code>{' '}
                    on port {cam.http_port}. Lower fidelity than RTSP but
                    works wherever your browser does.
                  </>
                ) : (
                  <>
                    Pulling H.264 over RTSP on port {cam.rtsp_port}
                    {' '}({(cam.rtsp_transport ?? 'tcp').toUpperCase()}
                    {cam.rtsp_transport === 'http' ? ' tunnel' : ''}).
                  </>
                )}
              </div>

              <Button variant="ghost" className="mt-3"
                      onClick={tryAlternatePorts}
                      disabled={tryingPorts}>
                {tryingPorts ? 'Probing ports…' : '🔄 Try alternate ports'}
              </Button>
              {portResult && (
                <div className={'mt-2 text-xs ' + (portResult.ok ? 'text-emerald-700' : 'text-red-600')}>
                  {portResult.summary}
                  {portResult.attempts && (
                    <ul className="mt-1 ml-2 space-y-0.5">
                      {portResult.attempts.map((a: any, i: number) => (
                        <li key={i} className={a.ok ? 'text-emerald-700' : 'text-slate-500 dark:text-slate-300'}>
                          {a.ok ? '✅' : '❌'} port {a.port} ({a.transport}) — {a.reason}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </Card>
          )}

          <Card className="p-4">
            <div className="text-sm font-medium mb-2">Existing zones on this camera</div>
            {zones.length === 0 && <div className="text-slate-400 dark:text-slate-400 text-sm">None yet.</div>}
            {zones.map(z => (
              <div key={z.id} className="flex items-center justify-between text-sm py-1 border-t dark:border-slate-800 first:border-t-0">
                <div>
                  <div className="font-medium">{z.name}</div>
                  <div className="text-xs text-slate-500 dark:text-slate-300">{purposeLabelOf(z.detection_types_json)}</div>
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
