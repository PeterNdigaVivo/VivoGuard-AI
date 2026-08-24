// Training Studio landing — list datasets, create new ones, jump into
// the annotation page, and start training jobs.

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge, Button, Card, Input, PageHeader } from '@/components/ui/Primitives'
import { training, type Dataset, type SimulationEvidenceSummary } from '@/api/training'

function metric(value: number | null) {
  return value === null ? 'Not enough evidence' : `${(value * 100).toFixed(1)}%`
}

export default function TrainingStudioPage() {
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [name, setName] = useState('')
  const [classes, setClasses] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [evidence, setEvidence] = useState<SimulationEvidenceSummary | null>(null)

  const load = () => Promise.all([
    training.listDatasets(),
    training.simulationEvidenceSummary(),
  ]).then(([datasetRows, summary]) => {
    setDatasets(datasetRows)
    setEvidence(summary)
  }).catch(e => setError(String(e)))
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

      <Card className="p-4 mb-6">
        <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
          <div>
            <div className="font-medium">Live simulation evidence</div>
            <div className="text-sm text-slate-500 dark:text-slate-300 mt-1">
              Real camera person-presence probes only. Synthetic scenarios are excluded, and this is not fleet-wide alert accuracy.
            </div>
          </div>
          {evidence && (
            <Badge color={evidence.claimable_99 ? 'green' : 'amber'}>
              {evidence.claimable_99 ? '99% evidence gate proven' : '99% evidence gate not proven'}
            </Badge>
          )}
        </div>
        {evidence ? (
          <>
            <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3 text-sm">
              <div><div className="text-slate-500">Awaiting primary</div><div className="text-xl font-semibold">{evidence.pending_primary}</div></div>
              <div><div className="text-slate-500">Awaiting independent</div><div className="text-xl font-semibold">{evidence.pending_independent}</div></div>
              <div><div className="text-slate-500">Overdue</div><div className={`text-xl font-semibold ${evidence.overdue ? 'text-red-600' : ''}`}>{evidence.overdue}</div></div>
              <div><div className="text-slate-500">Approved</div><div className="text-xl font-semibold">{evidence.approved}</div></div>
              <div><div className="text-slate-500">Precision</div><div className="text-xl font-semibold">{metric(evidence.precision)}</div></div>
              <div><div className="text-slate-500">Recall</div><div className="text-xl font-semibold">{metric(evidence.recall)}</div></div>
            </div>
            <div className="mt-4 flex flex-wrap items-center gap-3 text-xs text-slate-500 dark:text-slate-300">
              <span>95% lower bounds: precision {metric(evidence.precision_lower_95)}, recall {metric(evidence.recall_lower_95)}</span>
              <span>Reviewed camera slices proven at 99%: {evidence.camera_slices_proven_99}/{evidence.camera_slices_total}</span>
              <span>Confusion matrix: TP {evidence.confusion_matrix.tp}, FP {evidence.confusion_matrix.fp}, FN {evidence.confusion_matrix.fn}, TN {evidence.confusion_matrix.tn}</span>
              {evidence.dataset_id && (
                <Link to={`/training/datasets/${evidence.dataset_id}`} className="text-sky-600 hover:underline font-medium">
                  Review evidence →
                </Link>
              )}
            </div>
          </>
        ) : (
          <div className="text-sm text-slate-500">Loading governed evidence queue…</div>
        )}
      </Card>

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
          <thead className="bg-slate-50 dark:bg-slate-800 text-slate-600 dark:text-slate-200">
            <tr>
              <th className="text-left p-3">Name</th>
              <th className="text-left p-3">Classes</th>
              <th className="text-left p-3">Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {datasets.map(d => (
              <tr key={d.id} className="border-t dark:border-slate-800 hover:bg-slate-50 dark:hover:bg-slate-800/60">
                <td className="p-3 font-medium">{d.name}</td>
                <td className="p-3">
                  {(d.classes_json || []).map(c => (
                    <Badge key={c} color="sky">{c}</Badge>
                  ))}
                  {!d.classes_json?.length && <span className="text-slate-400 dark:text-slate-400">—</span>}
                </td>
                <td className="p-3 text-slate-500 dark:text-slate-300">
                  {new Date(d.created_at).toLocaleString()}
                </td>
                <td className="p-3 text-right">
                  <Link to={`/training/datasets/${d.id}`}
                        className="text-sky-600 hover:underline">Open</Link>
                </td>
              </tr>
            ))}
            {!datasets.length && (
              <tr><td className="p-6 text-slate-500 dark:text-slate-300 text-center" colSpan={4}>
                No datasets yet — create one above.
              </td></tr>
            )}
          </tbody>
        </table>
      </Card>
    </div>
  )
}
