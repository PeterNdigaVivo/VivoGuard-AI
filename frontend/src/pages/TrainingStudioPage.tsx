// Training Studio landing — list datasets, create new ones, jump into
// the annotation page, and start training jobs.

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge, Button, Card, Input, PageHeader } from '@/components/ui/Primitives'
import { training, type Dataset } from '@/api/training'

export default function TrainingStudioPage() {
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [name, setName] = useState('')
  const [classes, setClasses] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = () => training.listDatasets().then(setDatasets).catch(e => setError(String(e)))
  useEffect(() => { load() }, [])

  async function create() {
    if (!name) return
    setBusy(true); setError(null)
    try {
      await training.createDataset({
        name,
        classes: classes.split(',').map(s => s.trim()).filter(Boolean),
      })
      setName(''); setClasses('')
      await load()
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }

  return (
    <div className="p-6">
      <PageHeader title="AI Training Studio"
        actions={<div className="flex gap-2">
          <Link to="/training/chain"><Button>Chain-wide training →</Button></Link>
          <Link to="/training/shutter"><Button variant="ghost">Shutter training →</Button></Link>
          <Link to="/training/uniform"><Button variant="ghost">Uniform training →</Button></Link>
        </div>} />

      {/* New dataset form */}
      <Card className="p-4 mb-6">
        <div className="font-medium mb-3">New dataset</div>
        <div className="flex flex-wrap gap-2 items-center">
          <Input placeholder="Dataset name" value={name} onChange={e => setName(e.target.value)} />
          <Input placeholder="Classes (comma-separated)" className="flex-1 min-w-[300px]"
                 value={classes} onChange={e => setClasses(e.target.value)} />
          <Button onClick={create} disabled={busy || !name}>
            {busy ? 'Creating…' : 'Create dataset'}
          </Button>
        </div>
        {error && <div className="text-red-600 text-sm mt-2">{error}</div>}
      </Card>

      {/* Dataset list */}
      <Card>
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600">
            <tr>
              <th className="text-left p-3">Name</th>
              <th className="text-left p-3">Classes</th>
              <th className="text-left p-3">Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {datasets.map(d => (
              <tr key={d.id} className="border-t hover:bg-slate-50">
                <td className="p-3 font-medium">{d.name}</td>
                <td className="p-3">
                  {(d.classes_json || []).map(c => (
                    <Badge key={c} color="sky">{c}</Badge>
                  ))}
                  {!d.classes_json?.length && <span className="text-slate-400">—</span>}
                </td>
                <td className="p-3 text-slate-500">
                  {new Date(d.created_at).toLocaleString()}
                </td>
                <td className="p-3 text-right">
                  <Link to={`/training/datasets/${d.id}`}
                        className="text-sky-600 hover:underline">Open</Link>
                </td>
              </tr>
            ))}
            {!datasets.length && (
              <tr><td className="p-6 text-slate-500 text-center" colSpan={4}>
                No datasets yet — create one above.
              </td></tr>
            )}
          </tbody>
        </table>
      </Card>
    </div>
  )
}
