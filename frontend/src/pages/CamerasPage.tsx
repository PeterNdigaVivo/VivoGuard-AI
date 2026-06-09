// Camera management page — list all cameras with status, quick actions,
// and an inline Store dropdown so operators attach/detach without curl.

import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge, Button, Card, Input, PageHeader, Select, useToast } from '@/components/ui/Primitives'
import { api } from '@/api/client'
import { cameras as camsApi, type Camera } from '@/api/cameras'
import { stores as storesApi, type Store } from '@/api/stores'

function statusColor(s: string): 'green' | 'red' | 'amber' | 'slate' {
  if (s === 'online')   return 'green'
  if (s === 'offline')  return 'red'
  if (s === 'degraded') return 'amber'
  return 'slate'
}

// Tiny snapshot thumbnail — lazy-loaded via IntersectionObserver so a
// store with 50 cameras doesn't queue 50 snapshot requests on mount.
// Reuses /cameras/{id}/snapshot which serves a Redis-cached frame from
// the inference worker (no extra RTSP load when AI is enabled).
function CameraThumb({ cameraId, name, onClick }: {
  cameraId: number
  name: string
  onClick?: () => void
}) {
  const ref = useRef<HTMLDivElement | null>(null)
  const [state, setState] = useState<'idle' | 'loading' | 'ready' | 'fail'>('idle')
  const [src, setSrc] = useState<string | null>(null)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const io = new IntersectionObserver(entries => {
      for (const e of entries) {
        if (e.isIntersecting && state === 'idle') {
          setState('loading')
          api<{ jpeg_b64: string }>(`/cameras/${cameraId}/snapshot`)
            .then(r => { setSrc(`data:image/jpeg;base64,${r.jpeg_b64}`); setState('ready') })
            .catch(() => setState('fail'))
          io.disconnect()
        }
      }
    }, { rootMargin: '200px' })
    io.observe(el)
    return () => io.disconnect()
    // cameraId is stable per row; intentional one-shot fetch
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cameraId])

  const baseCls = "w-24 h-16 rounded border border-slate-200 bg-slate-100 " +
                  "flex items-center justify-center overflow-hidden text-slate-400"
  const interactiveCls = onClick ? " cursor-zoom-in hover:border-sky-400" : ""

  return (
    <div ref={ref} className={baseCls + interactiveCls}
         title={state === 'fail' ? 'Snapshot unavailable' : name}
         onClick={state === 'ready' && onClick ? onClick : undefined}>
      {state === 'ready' && src
        ? <img src={src} alt={name} loading="lazy"
               className="w-full h-full object-cover" />
        : <CameraIconPlaceholder dim={state === 'loading'} />}
    </div>
  )
}

function CameraIconPlaceholder({ dim }: { dim?: boolean }) {
  return (
    <svg viewBox="0 0 24 24" width="28" height="28" fill="none"
         stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"
         strokeLinejoin="round" className={dim ? 'animate-pulse' : ''}>
      <path d="M3 7h11l3 3v7a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2z"
            transform="translate(2 0)" />
      <circle cx="10.5" cy="13" r="2.8" />
    </svg>
  )
}

export default function CamerasPage() {
  const [cams, setCams]   = useState<Camera[]>([])
  const [stores, setStores] = useState<Store[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<number | null>(null)
  const toast = useToast()

  const reload = () => Promise.all([
    camsApi.list().then(setCams),
    storesApi.list().then(setStores),
  ]).catch(e => setError(String(e)))
  useEffect(() => { reload() }, [])

  async function remove(id: number) {
    if (!confirm('Remove this camera?')) return
    await camsApi.remove(id)
    reload()
  }

  // One-click: probe every camera, switch ones whose RTSP/554 is
  // blocked over to HTTP-snapshot polling on whichever HTTP port
  // answers with a JPEG. Use after adding a store where you don't
  // know which transport will work (Vivo Moi Ave / TRM / Acacia
  // pattern — port 554 not forwarded by the store router).
  const [failingOver, setFailingOver] = useState(false)
  async function autoFailover() {
    if (!confirm('Probe all cameras and switch unreachable RTSP ones to HTTP snapshot polling?')) return
    setFailingOver(true)
    try {
      const res = await api<{ checked: number; switched: number; report: any[] }>(
        '/cameras/auto-failover', { method: 'POST', body: {} },
      )
      toast.push(`Checked ${res.checked}, switched ${res.switched} to HTTP snapshot`)
      reload()
    } catch (e) {
      toast.push(`Auto-failover failed: ${e}`, 'err')
    } finally {
      setFailingOver(false)
    }
  }

  // Quick Add NVR — operator-friendly replacement for the manual
  // SQL workflow. One form: store + brand + host + port + creds +
  // channel count; backend materialises all channels in one
  // transaction with the right per-brand RTSP URL template.
  const [quickAddOpen, setQuickAddOpen] = useState(false)
  const [qaForm, setQaForm] = useState({
    store_id: 0,
    brand: 'dahua' as 'dahua' | 'hikvision' | 'uniview',
    host: '',
    rtsp_port: 7000,           // Dahua-on-7000 is the dominant Vivo pattern
    http_port: 80,
    username: 'admin',
    password: '',
    channel_count: 16,
    name_prefix: '',
    rtsp_transport: 'http' as 'tcp' | 'http' | 'udp',   // HTTP tunnel by default
    network_type: 'wan',
  })
  const [qaBusy, setQaBusy] = useState(false)
  // When the operator picks a Dahua/Hik/Uniview brand, snap the
  // RTSP port to the brand default — mirrors the wizard behaviour.
  function setQaBrand(b: 'dahua' | 'hikvision' | 'uniview') {
    setQaForm(f => ({
      ...f, brand: b,
      rtsp_port: b === 'dahua' ? 7000 : 554,
    }))
  }
  async function quickAddSubmit() {
    if (!qaForm.store_id) { toast.push('Pick a store first.', 'err'); return }
    if (!qaForm.host || !qaForm.username) {
      toast.push('Host and username are required.', 'err'); return
    }
    if (!confirm(`Create ${qaForm.channel_count} cameras for the NVR at ${qaForm.host}?`)) return
    setQaBusy(true)
    try {
      const created = await api<Camera[]>('/nvr/quick-add', {
        method: 'POST',
        body: {
          ...qaForm,
          name_prefix: qaForm.name_prefix || null,
        },
      })
      toast.push(`Created ${created.length} cameras for ${qaForm.host}`)
      setQuickAddOpen(false)
      reload()
    } catch (e) {
      toast.push(`Quick Add failed: ${e}`, 'err')
    } finally {
      setQaBusy(false)
    }
  }

  // Bulk port update — operators have a row of N cameras at one
  // store IP (Moi Avenue: 7 cameras at 197.155.67.50) that all need
  // the same port flipped to 7000. Without this they'd PATCH each
  // camera individually. The endpoint requires at least one filter,
  // so empty inputs trigger a clear error instead of a fleet-wide
  // rewrite.
  // Search filter — client-side. Matches camera name OR store name
  // (case-insensitive substring). Empty string = no filter.
  const [search, setSearch] = useState('')
  const [bulkHost, setBulkHost] = useState('')
  const [bulkPort, setBulkPort] = useState<number>(7000)
  const [bulkTunnel, setBulkTunnel] = useState(true)
  const [bulkBusy, setBulkBusy] = useState(false)
  async function applyBulkPort() {
    if (!bulkHost) {
      toast.push('Enter the host (public IP or DDNS) to filter by.', 'err'); return
    }
    if (!confirm(`Set port ${bulkPort} (HTTP tunnel: ${bulkTunnel ? 'yes' : 'no'}) on every camera at ${bulkHost}?`)) return
    setBulkBusy(true)
    try {
      const res = await api<{ matched: number; updated: number; report: any[] }>(
        '/cameras/bulk-update-port', {
          method: 'POST',
          body: { host_filter: bulkHost, new_port: bulkPort, use_http_tunnel: bulkTunnel },
        },
      )
      toast.push(`Matched ${res.matched}, updated ${res.updated} cameras at ${bulkHost}`)
      reload()
    } catch (e) {
      toast.push(`Bulk update failed: ${e}`, 'err')
    } finally {
      setBulkBusy(false)
    }
  }

  // Single-PATCH approach. Backend's CameraUpdate now accepts
  // store_id (nullable). Empty dropdown value → detach (store_id=null).
  // We optimistically update the camera locally and fire a toast on
  // success so the operator sees the change even before the refetch.
  async function setStore(camId: number, storeIdRaw: string) {
    const cam = cams.find(c => c.id === camId)
    if (!cam) return
    const targetId: number | null = storeIdRaw ? Number(storeIdRaw) : null
    if (cam.store_id === targetId) return

    setBusyId(camId)
    // Optimistic update so the dropdown 'sticks' immediately.
    setCams(prev => prev.map(c => c.id === camId ? { ...c, store_id: targetId } : c))
    try {
      const updated = await camsApi.update(camId, { store_id: targetId })
      // Reconcile with server response (in case backend coerced anything).
      setCams(prev => prev.map(c => c.id === camId ? { ...c, ...updated } : c))
      const storeName = targetId
        ? (stores.find(s => s.id === targetId)?.name ?? `store ${targetId}`)
        : '(unattached)'
      toast.push(`Saved: ${cam.name} → ${storeName}`)
      // Fire-and-forget metric backfill so the store dashboard picks up
      // this camera's historical metrics immediately.
      fetch('/api/analytics/admin/backfill-store-ids', {
        method: 'POST',
        headers: { Authorization: `Bearer ${localStorage.getItem('vg_access_token') ?? ''}` },
      }).catch(() => {})
    } catch (e) {
      // Roll back optimistic update.
      setCams(prev => prev.map(c => c.id === camId ? { ...c, store_id: cam.store_id } : c))
      toast.push(`Failed to save: ${e}`, 'err')
      setError(String(e))
    } finally {
      setBusyId(null)
    }
  }

  // Clicking a thumbnail opens a larger view. State is just the camera
  // id; the overlay re-fetches a fresh snapshot at full size.
  const [zoomed, setZoomed] = useState<Camera | null>(null)

  // Group by store now (was grouped by `site` text label before).
  // Apply the search filter first — match either the camera name OR
  // the store name (case-insensitive substring), then drop empty
  // groups so a query like "Greenspan" hides every unrelated store
  // entirely instead of just emptying the table inside its card.
  const q = search.trim().toLowerCase()
  const filteredCams = q
    ? cams.filter(c => {
        const storeName = c.store_id
          ? (stores.find(s => s.id === c.store_id)?.name ?? '')
          : ''
        return (c.name || '').toLowerCase().includes(q)
            || storeName.toLowerCase().includes(q)
      })
    : cams
  const groups: Record<string, Camera[]> = {}
  for (const c of filteredCams) {
    const label = c.store_id
      ? (stores.find(s => s.id === c.store_id)?.name ?? `Store #${c.store_id}`)
      : '(unattached)'
    ;(groups[label] ??= []).push(c)
  }

  return (
    <div className="p-6">
      <PageHeader
        title="Cameras"
        actions={<>
          <Button variant="ghost" onClick={() => setQuickAddOpen(true)}>
            ⚡ Quick Add NVR
          </Button>
          <Button variant="ghost" onClick={autoFailover} disabled={failingOver}>
            {failingOver ? 'Probing…' : 'Auto-fix offline cameras'}
          </Button>
          <Link to="/cameras/add"><Button>+ Add camera</Button></Link>
        </>}
      />
      {error && <div className="text-red-600 mb-2">{error}</div>}

      {/* Search — filters the loaded camera list by store name OR
          camera name. Client-side only, no API call. Empty groups
          are hidden so a query for "Greenspan" doesn't leave empty
          cards for every other store. */}
      <Card className="p-3 mb-3">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-medium">Search:</span>
          <input className="border rounded px-2 py-1 text-sm flex-1 min-w-[240px]"
                 placeholder="Type a store name or camera name…"
                 value={search}
                 onChange={e => setSearch(e.target.value)} />
          {search && (
            <>
              <button onClick={() => setSearch('')}
                      className="text-xs text-sky-600 hover:underline">
                clear
              </button>
              <span className="text-xs text-slate-500">
                Showing <strong>{filteredCams.length}</strong> of <strong>{cams.length}</strong>
                {' '}cameras across <strong>{Object.keys(groups).length}</strong>{' '}
                store{Object.keys(groups).length === 1 ? '' : 's'}
              </span>
            </>
          )}
        </div>
      </Card>

      {/* Bulk port update — set the RTSP port on every camera at a
          given host in one shot. The most common use is "every Moi
          Ave camera at 197.155.67.50 → port 7000 with HTTP tunnel". */}
      <Card className="p-3 mb-3">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-medium">Bulk port update:</span>
          <input className="border rounded px-2 py-1 text-sm w-64"
                 placeholder="host (e.g. 197.155.67.50)"
                 value={bulkHost}
                 onChange={e => setBulkHost(e.target.value)} />
          <span>→ port</span>
          <select className="border rounded px-2 py-1 text-sm"
                  value={bulkPort}
                  onChange={e => setBulkPort(Number(e.target.value))}>
            {[7000, 554, 80, 800, 8000, 8080].map(p => (
              <option key={p} value={p}>{p}</option>
            ))}
          </select>
          <label className="flex items-center gap-1">
            <input type="checkbox" checked={bulkTunnel}
                   onChange={e => setBulkTunnel(e.target.checked)} />
            <span className="text-xs">use HTTP tunnel</span>
          </label>
          <Button onClick={applyBulkPort} disabled={bulkBusy || !bulkHost}>
            {bulkBusy ? 'Applying…' : 'Apply'}
          </Button>
        </div>
        <div className="text-xs text-slate-500 mt-1">
          Updates every camera whose host exactly matches. Most stores
          use Dahua on port 7000 with HTTP tunneling.
        </div>
      </Card>

      {Object.entries(groups).map(([label, list]) => (
        <Card key={label} className="mb-4">
          <div className="px-4 py-2 bg-slate-50 text-slate-600 text-sm font-medium border-b">
            {label}
          </div>
          <table className="w-full text-sm">
            <thead className="text-slate-600">
              <tr>
                <th className="text-left p-3 w-28"></th>
                <th className="text-left p-3">Name</th>
                <th className="text-left p-3">Type</th>
                <th className="text-left p-3">Host</th>
                <th className="text-left p-3">Status</th>
                <th className="text-left p-3">Store</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {list.map(c => (
                <tr key={c.id} className="border-t hover:bg-slate-50">
                  <td className="p-3">
                    <CameraThumb cameraId={c.id} name={c.name}
                                 onClick={() => setZoomed(c)} />
                  </td>
                  <td className="p-3 font-medium">{c.name}</td>
                  <td className="p-3 capitalize">{c.connection_type.replace('_', ' ')}</td>
                  <td className="p-3 font-mono text-xs">
                    {c.host}:{c.rtsp_port}
                    {c.channel_number ? ` · ch${c.channel_number}` : ''}
                  </td>
                  <td className="p-3">
                    <Badge color={statusColor(c.status)}>{c.status}</Badge>
                  </td>
                  <td className="p-3">
                    <Select value={c.store_id ?? ''}
                            disabled={busyId === c.id}
                            onChange={e => setStore(c.id, e.target.value)}>
                      <option value="">— unattached —</option>
                      {stores.map(s => (
                        <option key={s.id} value={s.id}>{s.name}</option>
                      ))}
                    </Select>
                  </td>
                  <td className="p-3 text-right whitespace-nowrap">
                    <Link className="text-sky-600 hover:underline mr-3"
                          to={`/cameras/${c.id}/setup`}>Set up</Link>
                    <Link className="text-slate-500 hover:underline text-xs mr-3"
                          to={`/cameras/${c.id}/detection`}>advanced</Link>
                    <button className="text-red-600 hover:underline"
                            onClick={() => remove(c.id)}>Remove</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      ))}

      {cams.length > 0 && filteredCams.length === 0 && (
        <Card className="p-8 text-center text-slate-500">
          No cameras match "<strong>{search}</strong>".
          {' '}
          <button onClick={() => setSearch('')}
                  className="text-sky-600 hover:underline">Clear search</button>
        </Card>
      )}

      {cams.length === 0 && !error && (
        <Card className="p-8 text-center text-slate-500">
          No cameras yet — click <Link to="/cameras/add" className="text-sky-600 underline">Add camera</Link>.
        </Card>
      )}

      {cams.length > 0 && stores.length === 0 && (
        <Card className="p-4 text-amber-700 bg-amber-50 border-amber-200">
          You have cameras but no stores yet. <Link to="/stores" className="underline">Create a store</Link> so you can attach them.
        </Card>
      )}

      {/* Click-to-enlarge snapshot overlay. Re-fetches the snapshot at
          full size so the operator sees the current frame rather than
          the cached thumbnail. */}
      {zoomed && (
        <ZoomedSnapshot cam={zoomed} onClose={() => setZoomed(null)} />
      )}

      {/* Quick Add NVR modal — replaces the manual SQL workflow.
          Operator picks store + brand + creds + N channels and the
          backend materialises every channel in one POST. */}
      {quickAddOpen && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4"
             onClick={() => !qaBusy && setQuickAddOpen(false)}>
          {/* Stop-propagation wrapper because <Card> doesn't accept
              onClick — clicks inside the modal would otherwise bubble
              up to the backdrop and close the dialog. */}
          <div onClick={e => e.stopPropagation()} className="max-w-xl w-full">
            <Card className="p-5">
              <div className="flex justify-between items-center mb-3">
                <div className="text-lg font-semibold">⚡ Quick Add NVR</div>
                <button className="text-slate-400 hover:text-slate-700"
                        onClick={() => !qaBusy && setQuickAddOpen(false)}>✕</button>
              </div>
            <div className="text-xs text-slate-500 mb-3">
              Adds every channel of an NVR in one click. RTSP URLs are
              built per-brand; password is encrypted server-side; no
              probing — we trust your channel count.
            </div>
            <div className="grid grid-cols-2 gap-3">
              <label className="col-span-2 block">
                <div className="text-xs text-slate-600 mb-1">Store *</div>
                <Select className="w-full" value={qaForm.store_id || ''}
                        onChange={e => setQaForm(f => ({ ...f, store_id: Number(e.target.value) }))}>
                  <option value="">— pick a store —</option>
                  {stores.map(s => (
                    <option key={s.id} value={s.id}>
                      {s.name} — {s.city ?? '—'}, {s.country}
                    </option>
                  ))}
                </Select>
              </label>
              <label className="block">
                <div className="text-xs text-slate-600 mb-1">NVR brand</div>
                <Select className="w-full" value={qaForm.brand}
                        onChange={e => setQaBrand(e.target.value as any)}>
                  <option value="dahua">Dahua</option>
                  <option value="hikvision">Hikvision</option>
                  <option value="uniview">Uniview</option>
                </Select>
              </label>
              <label className="block">
                <div className="text-xs text-slate-600 mb-1">Channels *</div>
                <Input type="number" min={1} max={64} value={qaForm.channel_count}
                       onChange={e => setQaForm(f => ({ ...f, channel_count: Number(e.target.value) || 1 }))} />
              </label>
              <label className="col-span-2 block">
                <div className="text-xs text-slate-600 mb-1">Host (public IP or DDNS) *</div>
                <Input value={qaForm.host}
                       onChange={e => setQaForm(f => ({ ...f, host: e.target.value }))}
                       placeholder="197.155.67.50" />
              </label>
              <label className="block">
                <div className="text-xs text-slate-600 mb-1">RTSP port</div>
                <Select className="w-full" value={qaForm.rtsp_port}
                        onChange={e => setQaForm(f => ({ ...f, rtsp_port: Number(e.target.value) }))}>
                  {[7000, 554, 80, 800, 8000, 8080].map(p => (
                    <option key={p} value={p}>{p}</option>
                  ))}
                </Select>
              </label>
              <label className="block">
                <div className="text-xs text-slate-600 mb-1">RTSP transport</div>
                <Select className="w-full" value={qaForm.rtsp_transport}
                        onChange={e => setQaForm(f => ({ ...f, rtsp_transport: e.target.value as any }))}>
                  <option value="tcp">TCP (port 554 unblocked)</option>
                  <option value="http">HTTP tunnel (port 554 blocked, default)</option>
                  <option value="udp">UDP</option>
                </Select>
              </label>
              <label className="block">
                <div className="text-xs text-slate-600 mb-1">Username</div>
                <Input value={qaForm.username}
                       onChange={e => setQaForm(f => ({ ...f, username: e.target.value }))} />
              </label>
              <label className="block">
                <div className="text-xs text-slate-600 mb-1">Password</div>
                <Input type="password" value={qaForm.password}
                       onChange={e => setQaForm(f => ({ ...f, password: e.target.value }))} />
              </label>
              <label className="col-span-2 block">
                <div className="text-xs text-slate-600 mb-1">
                  Name prefix (optional — defaults to store name)
                </div>
                <Input value={qaForm.name_prefix}
                       onChange={e => setQaForm(f => ({ ...f, name_prefix: e.target.value }))}
                       placeholder="Moi Avenue" />
                <div className="text-xs text-slate-400 mt-1">
                  Channels will be named "{qaForm.name_prefix || (stores.find(s => s.id === qaForm.store_id)?.name ?? 'Store')} - Channel N"
                </div>
              </label>
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <Button variant="ghost" onClick={() => !qaBusy && setQuickAddOpen(false)}>
                Cancel
              </Button>
              <Button onClick={quickAddSubmit} disabled={qaBusy}>
                {qaBusy ? 'Creating…' : `Add ${qaForm.channel_count} cameras`}
              </Button>
            </div>
            </Card>
          </div>
        </div>
      )}
    </div>
  )
}

function ZoomedSnapshot({ cam, onClose }: { cam: Camera; onClose: () => void }) {
  const [src, setSrc] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    let alive = true
    setSrc(null); setErr(null)
    api<{ jpeg_b64: string }>(`/cameras/${cam.id}/snapshot`)
      .then(r => { if (alive) setSrc(`data:image/jpeg;base64,${r.jpeg_b64}`) })
      .catch(e => { if (alive) setErr(String(e)) })
    return () => { alive = false }
  }, [cam.id, tick])

  return (
    <div className="fixed inset-0 bg-black/80 flex items-center justify-center z-50 p-6"
         onClick={onClose}>
      <div onClick={e => e.stopPropagation()}
           className="bg-white rounded shadow-lg max-w-4xl w-full">
        <div className="flex items-center justify-between p-3 border-b">
          <div>
            <div className="font-medium">{cam.name}</div>
            <div className="text-xs text-slate-500 font-mono">
              {cam.host}:{cam.rtsp_port}{cam.channel_number ? ` · ch${cam.channel_number}` : ''}
            </div>
          </div>
          <div className="flex gap-2">
            <Button variant="ghost" onClick={() => setTick(t => t + 1)}>Refresh</Button>
            <button className="text-slate-400 hover:text-slate-700 px-2"
                    onClick={onClose}>✕</button>
          </div>
        </div>
        <div className="bg-slate-900 min-h-[320px] flex items-center justify-center">
          {src && <img src={src} alt={cam.name}
                       className="max-h-[70vh] w-auto object-contain" />}
          {!src && !err && (
            <div className="text-slate-400 text-sm py-12">Loading snapshot…</div>
          )}
          {err && (
            <div className="text-slate-300 text-sm py-12">
              Snapshot unavailable — the camera may be offline.
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
