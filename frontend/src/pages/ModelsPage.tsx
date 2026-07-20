// Model registry page. Lists trained models, lets you deploy to cameras,
// roll back, or export to ONNX/TorchScript/TensorRT.

import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge, Button, Card, PageHeader, Select } from '@/components/ui/Primitives'
import { training, type AIModel } from '@/api/training'
import { cameras as camsApi, type Camera } from '@/api/cameras'

export default function ModelsPage() {
  const [models, setModels] = useState<AIModel[]>([])
  const [cameras, setCameras] = useState<Camera[]>([])
  const [pendingDeploy, setPendingDeploy] = useState<Record<number, number[]>>({})

  const reload = () => Promise.all([
    training.listModels().then(setModels),
    camsApi.list().then(setCameras),
  ]).catch(console.error)
  useEffect(() => { reload() }, [])

  async function deploy(modelId: number) {
    const ids = pendingDeploy[modelId] ?? []
    if (!ids.length) return
    await training.deployModel(modelId, ids)
    setPendingDeploy(p => ({ ...p, [modelId]: [] }))
    reload()
  }
  async function rollback(modelId: number) {
    if (!confirm('Roll back to the previous model version?')) return
    await training.rollbackModel(modelId)
    reload()
  }
  async function exportFmt(modelId: number, format: 'torchscript' | 'onnx' | 'engine') {
    const r = await training.exportModel(modelId, format)
    alert(`Exported to: ${r.path}`)
  }

  const chainModels = useMemo(
    () => models.filter(m => m.is_chain_model),
    [models],
  )
  const otherModels = useMemo(
    () => models.filter(m => !m.is_chain_model),
    [models],
  )

  return (
    <div className="p-6">
      <PageHeader title="Models"
        actions={<Link to="/training/chain"><Button>Chain training →</Button></Link>} />

      {/* Chain models — surfaced first because they're the fleet-wide
          default. Legacy per-store models follow in the standard table. */}
      {chainModels.length > 0 && (
        <Card className="p-4 mb-4">
          <div className="font-medium mb-3">
            🌐 Chain models <span className="text-slate-500 dark:text-slate-300 text-sm font-normal">
              — pooled from every store, deploy fleet-wide</span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-slate-50 text-slate-600 dark:bg-slate-800 dark:text-slate-200">
                <tr>
                  <th className="text-left p-2">Detector</th>
                  <th className="text-left p-2">Version</th>
                  <th className="text-right p-2">Samples</th>
                  <th className="text-right p-2">Stores</th>
                  <th className="text-right p-2">Accuracy</th>
                  <th className="text-left p-2">Status</th>
                  <th className="text-left p-2">Trained</th>
                </tr>
              </thead>
              <tbody>
                {chainModels.map(m => (
                  <tr key={m.id} className="border-t dark:border-slate-800">
                    <td className="p-2"><Badge color="sky">{m.detector_type ?? '—'}</Badge></td>
                    <td className="p-2 font-medium">{m.version}</td>
                    <td className="p-2 text-right tabular-nums">{m.sample_count ?? '—'}</td>
                    <td className="p-2 text-right tabular-nums">{(m.trained_on_stores ?? []).length}</td>
                    <td className="p-2 text-right tabular-nums">
                      {m.map50 != null ? `${(m.map50 * 100).toFixed(1)}%` : '—'}
                    </td>
                    <td className="p-2">
                      {m.deployed
                        ? <Badge color="green">Live fleet-wide</Badge>
                        : <Badge color="slate">Archived</Badge>}
                    </td>
                    <td className="p-2 text-slate-600 dark:text-slate-300">
                      {new Date(m.created_at).toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="text-xs text-slate-500 dark:text-slate-300 mt-2">
            Manage chain models from <Link to="/training/chain" className="text-sky-600 hover:underline">
            Training → Chain</Link>.
          </div>
        </Card>
      )}

      <Card>
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600 dark:bg-slate-800 dark:text-slate-200">
            <tr>
              <th className="text-left p-3">Name / version</th>
              <th className="text-left p-3">Base</th>
              <th className="text-left p-3">Classes</th>
              <th className="text-left p-3">mAP@50</th>
              <th className="text-left p-3">Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {otherModels.map(m => {
              const targetIds = pendingDeploy[m.id] ?? []
              return (
                <tr key={m.id} className="border-t align-top dark:border-slate-800">
                  <td className="p-3">
                    <div className="font-medium">{m.name}</div>
                    <div className="text-slate-500 dark:text-slate-300 text-xs">{m.version}</div>
                  </td>
                  <td className="p-3">{m.base_model}</td>
                  <td className="p-3">
                    {(m.classes_json || []).slice(0, 4).map(c =>
                      <Badge key={c} color="sky">{c}</Badge>)}
                    {m.classes_json?.length > 4 && (
                      <span className="text-xs text-slate-500 dark:text-slate-300"> +{m.classes_json.length - 4}</span>
                    )}
                  </td>
                  <td className="p-3">{m.map50?.toFixed(3) ?? '—'}</td>
                  <td className="p-3">
                    {m.deployed
                      ? <Badge color="green">deployed</Badge>
                      : <Badge color="slate">idle</Badge>}
                  </td>
                  <td className="p-3 text-right space-y-2">
                    <div className="flex flex-wrap gap-1 justify-end">
                      <Select multiple size={Math.min(4, Math.max(1, cameras.length))}
                              value={targetIds.map(String)}
                              onChange={(e) => {
                                const opts = Array.from(e.target.selectedOptions).map(o => Number(o.value))
                                setPendingDeploy(p => ({ ...p, [m.id]: opts }))
                              }}
                              style={{ minWidth: 160, height: 'auto' }}>
                        {cameras.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
                      </Select>
                      <Button onClick={() => deploy(m.id)} disabled={!targetIds.length}>Deploy</Button>
                    </div>
                    <div className="flex justify-end gap-1">
                      <Button variant="ghost" onClick={() => exportFmt(m.id, 'torchscript')}>→ TS</Button>
                      <Button variant="ghost" onClick={() => exportFmt(m.id, 'onnx')}>→ ONNX</Button>
                      <Button variant="ghost" onClick={() => exportFmt(m.id, 'engine')}>→ TRT</Button>
                      <Button variant="danger" onClick={() => rollback(m.id)}>Rollback</Button>
                    </div>
                  </td>
                </tr>
              )
            })}
            {!otherModels.length && (
              <tr><td colSpan={6} className="p-6 text-center text-slate-500 dark:text-slate-300">
                No per-store models yet. Train one in the AI Studio,
                or use the chain trainer to build a fleet-wide model.
              </td></tr>
            )}
          </tbody>
        </table>
      </Card>
    </div>
  )
}
