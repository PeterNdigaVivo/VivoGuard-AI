// Camera management page — list all cameras with status, quick actions.

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge, Button, Card, PageHeader } from '@/components/ui/Primitives'
import { cameras as camsApi, type Camera } from '@/api/cameras'

function statusColor(s: string): 'green' | 'red' | 'amber' | 'slate' {
  if (s === 'online')   return 'green'
  if (s === 'offline')  return 'red'
  if (s === 'degraded') return 'amber'
  return 'slate'
}

export default function CamerasPage() {
  const [cams, setCams] = useState<Camera[]>([])
  const [error, setError] = useState<string | null>(null)
  const reload = () => camsApi.list().then(setCams).catch(e => setError(String(e)))
  useEffect(() => { reload() }, [])

  async function remove(id: number) {
    if (!confirm('Remove this camera?')) return
    await camsApi.remove(id)
    reload()
  }

  // Group cameras by site so the table is easy to scan.
  const groups: Record<string, Camera[]> = {}
  for (const c of cams) (groups[c.site || '(unsorted)'] ??= []).push(c)

  return (
    <div className="p-6">
      <PageHeader
        title="Cameras"
        actions={<Link to="/cameras/add"><Button>+ Add camera</Button></Link>}
      />
      {error && <div className="text-red-600 mb-2">{error}</div>}

      {Object.entries(groups).map(([site, list]) => (
        <Card key={site} className="mb-4">
          <div className="px-4 py-2 bg-slate-50 text-slate-600 text-sm font-medium border-b">
            {site}
          </div>
          <table className="w-full text-sm">
            <thead className="text-slate-600">
              <tr>
                <th className="text-left p-3">Name</th>
                <th className="text-left p-3">Type</th>
                <th className="text-left p-3">Host</th>
                <th className="text-left p-3">Status</th>
                <th className="text-left p-3">AI</th>
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
                    {c.ai_enabled
                      ? <Badge color="sky">{c.ai_model_id ? `model #${c.ai_model_id}` : 'default'}</Badge>
                      : <Badge color="slate">off</Badge>}
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
    </div>
  )
}
