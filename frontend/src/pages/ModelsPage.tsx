// Model registry page. Lists trained models, lets you deploy to cameras,
// roll back, or export to ONNX/TorchScript/TensorRT.

import { useEffect, useState } from 'react'
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

  return (
    <div className="p-6">
      <PageHeader title="Models" />

      <Card>
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600">
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
            {models.map(m => {
              const targetIds = pendingDeploy[m.id] ?? []
              return (
                <tr key={m.id} className="border-t align-top">
                  <td className="p-3">
                    <div className="font-medium">{m.name}</div>
                    <div className="text-slate-500 text-xs">{m.version}</div>
                  </td>
                  <td className="p-3">{m.base_model}</td>
                  <td className="p-3">
                    {(m.classes_json || []).slice(0, 4).map(c =>
                      <Badge key={c} color="sky">{c}</Badge>)}
                    {m.classes_json?.length > 4 && (
                      <span className="text-xs text-slate-500"> +{m.classes_json.length - 4}</span>
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
            {!models.length && (
              <tr><td colSpan={6} className="p-6 text-center text-slate-500">
                No models yet. Train one in the AI Studio.
              </td></tr>
            )}
          </tbody>
        </table>
      </Card>
    </div>
  )
}
