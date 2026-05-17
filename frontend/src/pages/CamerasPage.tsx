// Camera management page — list all cameras with status, quick actions,
// and an inline Store dropdown so operators attach/detach without curl.

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge, Button, Card, PageHeader, Select, useToast } from '@/components/ui/Primitives'
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
        actions={<Link to="/cameras/add"><Button>+ Add camera</Button></Link>}
      />
      {error && <div className="text-red-600 mb-2">{error}</div>}

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
