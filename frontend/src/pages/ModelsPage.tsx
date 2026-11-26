// Models — what every camera actually runs, and the only way to change it:
// canary on a few cameras -> evaluate -> manual promotion -> rollback.
// Nothing here deploys automatically or fleet-wide.

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Badge, Card, PageHeader } from '@/components/ui/Primitives'
import { training as trainingApi, type AIModel } from '@/api/training'
import { modelRollouts, type FleetModels, type Rollout } from '@/api/models'

const pct = (v: number | null | undefined) => (v == null ? '—' : `${Math.round(v * 100)}%`)

export default function ModelsPage() {
  const [fleet, setFleet] = useState<FleetModels | null>(null)
  const [rollouts, setRollouts] = useState<Rollout[]>([])
  const [models, setModels] = useState<AIModel[]>([])
  const [message, setMessage] = useState<string | null>(null)
  const [canaryModel, setCanaryModel] = useState<number | ''>('')
  const [canaryCams, setCanaryCams] = useState<number[]>([])

  const load = useCallback(() => {
    modelRollouts.fleet().then(setFleet).catch(e => setMessage(String(e)))
    modelRollouts.list().then(setRollouts).catch(() => {})
    trainingApi.listModels().then(setModels).catch(() => {})
  }, [])
  useEffect(load, [load])

  const candidates = useMemo(() => models.filter(m =>
    (m.classes_json ?? []).map(c => c.toLowerCase()).includes('person')
    && !/-cls|_cls/i.test(`${m.base_model} ${m.weights_path}`)), [models])

  async function run(label: string, fn: () => Promise<unknown>) {
    setMessage(null)
    try { await fn(); setMessage(`${label} — done.`) } catch (e) { setMessage(`${label} failed: ${e}`) }
    load()
  }

  if (!fleet) return <div className="p-6"><PageHeader title="Models" />
    <Card className="p-4 text-slate-600 dark:text-slate-300">{message ?? 'Loading…'}</Card></div>

  const fallbacks = fleet.cameras.filter(c => c.fallback_reason)
  return (
    <div className="p-6 space-y-4">
      <PageHeader title="Models" />
      {message && <Card className="p-3 text-sm text-slate-700 dark:text-slate-200">{message}</Card>}

      <Card className="p-4">
        <div className="text-sm text-slate-700 dark:text-slate-200">
          Base model (universal fallback): <strong className="font-mono">{fleet.base_model}</strong>.
          Any camera whose assigned model cannot run the person pipeline uses the base model instead.
        </div>
        {fallbacks.length > 0 && (
          <div className="mt-2 text-sm text-amber-700 dark:text-amber-300">
            {fallbacks.length} camera(s) have an assignment that is being ignored for safety —
            roll those assignments back below.
          </div>
        )}
      </Card>

      <Card className="p-4">
        <div className="font-medium mb-2 text-slate-800 dark:text-slate-100">Rollouts</div>
        {rollouts.length === 0 ? (
          <div className="text-sm text-slate-500 dark:text-slate-300">No rollouts yet.</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-xs text-slate-500 dark:text-slate-300 text-left">
              <tr><th className="py-1">Model</th><th>Stage</th><th>Cameras</th>
                <th>Canary vs baseline</th><th /></tr>
            </thead>
            <tbody>
              {rollouts.map(r => {
                const ev = r.latest_evaluation
                const base = ev?.metrics?.baseline
                return (
                  <tr key={r.id} className="border-t border-slate-100 dark:border-slate-800 align-top">
                    <td className="py-2">{r.model_label ?? `#${r.model_id}`}</td>
                    <td><Badge color={r.stage === 'promoted' ? 'green' : r.stage === 'canary' ? 'sky' : 'slate'}>
                      {r.stage === 'rolled_back' ? 'Rolled back' : r.stage === 'canary' ? 'Canary' : 'Promoted'}
                    </Badge></td>
                    <td className="text-xs">{r.camera_ids.join(', ')}</td>
                    <td className="text-xs text-slate-600 dark:text-slate-300">
                      {!ev ? 'Not evaluated' : <>
                        {pct(ev.precision)} precise on {ev.sample_count ?? 0} reviewed
                        {' '}(baseline {pct(base?.precision)}){' '}
                        {ev.passed ? '✓ passed' : `✗ ${ev.reasons.join('; ')}`}
                      </>}
                    </td>
                    <td className="text-right whitespace-nowrap space-x-2">
                      {r.stage === 'canary' && (
                        <button type="button" className="px-2 py-1 rounded bg-slate-700 text-white text-xs"
                                onClick={() => run('Evaluate', () => modelRollouts.evaluate(r.id))}>
                          Evaluate
                        </button>
                      )}
                      {r.stage === 'canary' && ev?.passed && (
                        <button type="button" className="px-2 py-1 rounded bg-emerald-700 text-white text-xs"
                                onClick={() => {
                                  const raw = window.prompt(
                                    'Promote to which extra camera IDs? (comma-separated; canary cameras are kept)', '')
                                  if (raw === null) return
                                  const ids = raw.split(',').map(s => Number(s.trim())).filter(Boolean)
                                  if (!window.confirm(`Promote ${r.model_label} to ${r.camera_ids.length + ids.length} camera(s)?`)) return
                                  void run('Promote', () => modelRollouts.promote(r.id, ids))
                                }}>
                          Promote…
                        </button>
                      )}
                      {r.stage !== 'rolled_back' && (
                        <button type="button" className="px-2 py-1 rounded bg-red-700 text-white text-xs"
                                onClick={() => window.confirm('Put these cameras back on their previous model?')
                                  && void run('Rollback', () => modelRollouts.rollback(r.id))}>
                          Rollback
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </Card>

      <Card className="p-4">
        <div className="font-medium mb-2 text-slate-800 dark:text-slate-100">Start a canary</div>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <select value={canaryModel} onChange={e => setCanaryModel(e.target.value ? Number(e.target.value) : '')}
                  className="px-2 py-1.5 rounded border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 text-slate-800 dark:text-slate-100">
            <option value="">Compatible model…</option>
            {candidates.map(m => <option key={m.id} value={m.id}>{m.name} {m.version}</option>)}
          </select>
          <span className="text-xs text-slate-500 dark:text-slate-300">
            Tick up to 3 cameras in the table below, then
          </span>
          <button type="button" disabled={!canaryModel || canaryCams.length === 0}
                  className="px-3 py-1.5 rounded bg-sky-700 text-white disabled:opacity-50"
                  onClick={() => run('Start canary',
                    () => modelRollouts.startCanary(Number(canaryModel), canaryCams))
                    .then(() => setCanaryCams([]))}>
            Start canary on {canaryCams.length} camera(s)
          </button>
        </div>
      </Card>

      <Card className="p-4 overflow-x-auto">
        <div className="font-medium mb-2 text-slate-800 dark:text-slate-100">What each camera runs</div>
        <table className="w-full text-sm">
          <thead className="text-xs text-slate-500 dark:text-slate-300 text-left">
            <tr><th className="py-1" /><th>Camera</th><th>Assigned model</th><th>Actually running</th><th>Canary</th></tr>
          </thead>
          <tbody>
            {fleet.cameras.map(c => (
              <tr key={c.camera_id} className="border-t border-slate-100 dark:border-slate-800">
                <td className="py-1">
                  <input type="checkbox" checked={canaryCams.includes(c.camera_id)}
                         disabled={c.canary_rollout_id != null}
                         onChange={e => setCanaryCams(prev => e.target.checked
                           ? [...prev, c.camera_id].slice(0, 3)
                           : prev.filter(id => id !== c.camera_id))} />
                </td>
                <td>{c.camera_name}</td>
                <td className="text-xs">{c.assigned_model ?? 'Base model'}</td>
                <td className="text-xs">
                  {c.effective_model}
                  {c.fallback_reason && (
                    <span className="ml-1 text-amber-700 dark:text-amber-300">(fallback: {c.fallback_reason})</span>
                  )}
                </td>
                <td className="text-xs">{c.canary_rollout_id ? `#${c.canary_rollout_id}` : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  )
}
