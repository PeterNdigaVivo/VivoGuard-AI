// Camera management page — list all cameras with status, quick actions,
// and an inline Store dropdown so operators attach/detach without curl.

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge, Button, Card, PageHeader, Select, useToast } from '@/components/ui/Primitives'
import { api } from '@/api/client'
import { cameras as camsApi, type Camera } from '@/api/cameras'
import { stores as storesApi, type Store } from '@/api/stores'

function statusColor(s: string): 'green' | 'red' | 'amber' | 'slate' {
  if (s === 'online')   return 'green'
  if (s === 'offline')  return 'red'
  if (s === 'degraded') return 'amber'
  return 'slate'
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

  // Bulk port update — operators have a row of N cameras at one
  // store IP (Moi Avenue: 7 cameras at 197.155.67.50) that all need
  // the same port flipped to 7000. Without this they'd PATCH each
  // camera individually. The endpoint requires at least one filter,
  // so empty inputs trigger a clear error instead of a fleet-wide
  // rewrite.
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

  // Group by store now (was grouped by `site` text label before).
  const groups: Record<string, Camera[]> = {}
  for (const c of cams) {
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
          <Button variant="ghost" onClick={autoFailover} disabled={failingOver}>
            {failingOver ? 'Probing…' : 'Auto-fix offline cameras'}
          </Button>
          <Link to="/cameras/add"><Button>+ Add camera</Button></Link>
        </>}
      />
      {error && <div className="text-red-600 mb-2">{error}</div>}

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
    </div>
  )
}
