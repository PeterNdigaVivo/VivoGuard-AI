// Chain-Wide Training — pool every store's approved samples and train
// one shared model per detector. Operators see the live dataset
// composition, kick off a training run, watch progress, then deploy
// the resulting model to every camera with the matching zone tag.

import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Button, Card, PageHeader, Badge } from '@/components/ui/Primitives'
import { api } from '@/api/client'

type DetectorType = 'shutter' | 'uniform'

interface StoreBreakdown {
  store_id: number
  store_name: string | null
  total: number
  by_label: Record<string, number>
}

interface DetectorSummary {
  detector_type: DetectorType
  labels: string[]
  total_samples: number
  by_label: Record<string, number>
  by_store: StoreBreakdown[]
  stores_contributing: number[]
  min_per_label: number
  min_samples_met: boolean
  ready_to_train: boolean
  short_labels: Record<string, number>
}

interface ChainStatus {
  state: 'idle' | 'preparing' | 'filtering' | 'training' | 'done' | 'failed'
  message?: string
  epoch?: number
  total_epochs?: number
  model_id?: number
  weights?: string
  sample_count?: number
  trained_on_stores?: number[]
  report?: {
    accuracy?: number | null
    precision_macro?: number
    recall_macro?: number
    per_class?: Record<string, { precision: number; recall: number }>
    recommendation?: string
    warning?: string
  }
  stats?: {
    input?: Record<string, number>
    dropped_blurry?: number
    dropped_dup?: number
    dropped_capped?: number
    kept_by_label?: Record<string, number>
    kept_by_store?: Record<string, number>
  }
}

interface ChainModel {
  id: number
  name: string
  version: string
  detector_type: DetectorType
  sample_count: number | null
  trained_on_stores: number[]
  accuracy: number | null
  precision: number | null
  recall: number | null
  deployed: boolean
  weights_path: string
  created_at: string | null
}

const LABEL_TEXT: Record<string, string> = {
  // shutter
  open: 'Open', closed: 'Closed', partial: 'Partial',
  // uniform (P5 six-class + the seventh auto-harvest class)
  full_compliant:    '✅ Full Uniform',
  partial_compliant: '⚠️ Uniform, No Tag',
  color_only:        '🎨 Right Color Only',
  non_compliant:     '❌ Wrong/No Uniform',
  customer:          '👤 Customer',
  uncertain:         '❓ Unclear',
  staff:             '👔 Staff uniform',
}

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return `${(v * 100).toFixed(1)}%`
}

export default function ChainTrainingPage() {
  const [detector, setDetector] = useState<DetectorType>('uniform')
  const [summary, setSummary] = useState<Record<DetectorType, DetectorSummary> | null>(null)
  const [status, setStatus] = useState<ChainStatus>({ state: 'idle' })
  const [models, setModels] = useState<ChainModel[]>([])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [info, setInfo] = useState<string | null>(null)

  const loadSummary = () => {
    api<Record<DetectorType, DetectorSummary>>('/training/chain/dataset-summary')
      .then(setSummary)
      .catch(e => setErr(String(e)))
  }
  const loadStatus = (d: DetectorType) => {
    api<ChainStatus>(`/training/chain/status/${d}`)
      .then(setStatus)
      .catch(() => setStatus({ state: 'idle' }))
  }
  const loadModels = (d: DetectorType) => {
    api<ChainModel[]>(`/training/chain/models?detector_type=${d}`)
      .then(setModels)
      .catch(() => setModels([]))
  }

  useEffect(() => {
    loadSummary()
  }, [])

  useEffect(() => {
    loadStatus(detector)
    loadModels(detector)
    const t = setInterval(() => loadStatus(detector), 3000)
    return () => clearInterval(t)
  }, [detector])

  // When training finishes, refresh models so the new row shows up.
  useEffect(() => {
    if (status.state === 'done' || status.state === 'failed') {
      loadModels(detector)
      loadSummary()
    }
  }, [status.state, detector])

  const cur = summary?.[detector]

  async function startTraining() {
    if (!cur) return
    setErr(null); setInfo(null); setBusy(true)
    try {
      await api(`/training/chain/train?detector_type=${detector}`, { method: 'POST' })
      setInfo('Training started — this may take 20-40 minutes.')
      loadStatus(detector)
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function deployModel(modelId: number) {
    if (!confirm('Deploy this chain model to every matching camera across all stores?')) return
    setErr(null); setInfo(null); setBusy(true)
    try {
      const r = await api<{ camera_count: number }>(
        `/training/chain/deploy?detector_type=${detector}&model_id=${modelId}`,
        { method: 'POST' },
      )
      setInfo(`Deployed to ${r.camera_count} camera${r.camera_count === 1 ? '' : 's'}.`)
      loadModels(detector)
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  const training = status.state === 'preparing'
                  || status.state === 'filtering'
                  || status.state === 'training'

  const topStores = useMemo(
    () => (cur?.by_store ?? []).slice(0, 10),
    [cur],
  )
  const maxStoreCount = topStores.length ? Math.max(...topStores.map(s => s.total)) : 1

  return (
    <div className="p-6">
      <PageHeader title="Chain-Wide Training"
        actions={<div className="flex gap-2">
          <Link to="/training/shutter"><Button variant="ghost">Per-store shutter →</Button></Link>
          <Link to="/training/uniform"><Button variant="ghost">Per-store uniform →</Button></Link>
        </div>} />

      <p className="text-sm text-slate-600 mb-4 max-w-3xl">
        Pool approved samples from EVERY store into one shared dataset and train a
        single chain model. The trained model can be deployed to every camera
        in the fleet with one click — no per-store retraining needed.
      </p>

      {/* Detector switcher */}
      <div className="flex gap-2 mb-4">
        {(['uniform', 'shutter'] as DetectorType[]).map(d => (
          <Button
            key={d}
            variant={detector === d ? 'primary' : 'ghost'}
            onClick={() => setDetector(d)}>
            {d === 'uniform' ? 'Uniform Compliance' : 'Shutter / Door Status'}
          </Button>
        ))}
      </div>

      {err && <div className="text-red-600 text-sm mb-3">{err}</div>}
      {info && <div className="text-emerald-700 text-sm mb-3">{info}</div>}

      {/* Dataset overview */}
      <Card className="p-4 mb-4">
        <div className="font-medium mb-3">Dataset overview</div>
        {!cur && <div className="text-slate-400 text-sm">Loading…</div>}
        {cur && (
          <>
            <div className="flex flex-wrap gap-6 text-sm mb-4">
              <div>
                <div className="text-slate-500">Total samples</div>
                <div className="text-2xl font-semibold">{cur.total_samples}</div>
              </div>
              <div>
                <div className="text-slate-500">Stores contributing</div>
                <div className="text-2xl font-semibold">{cur.stores_contributing.length}</div>
              </div>
              <div>
                <div className="text-slate-500">Min per label</div>
                <div className="text-2xl font-semibold">{cur.min_per_label}</div>
              </div>
              <div>
                <div className="text-slate-500">Status</div>
                <div className="mt-1">
                  {cur.ready_to_train
                    ? <Badge color="green">Ready to train</Badge>
                    : <Badge color="amber">Need more samples</Badge>}
                </div>
              </div>
            </div>

            {/* By-label breakdown */}
            <div className="mb-4">
              <div className="text-sm text-slate-500 mb-2">Samples by label</div>
              <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-2">
                {cur.labels.map(l => {
                  const n = cur.by_label[l] ?? 0
                  const ok = n >= cur.min_per_label
                  return (
                    <div key={l} className={`p-3 rounded border ${
                      ok ? 'border-emerald-200 bg-emerald-50'
                         : 'border-amber-200 bg-amber-50'}`}>
                      <div className="text-xs text-slate-600">{LABEL_TEXT[l] ?? l}</div>
                      <div className="text-lg font-semibold">{n}</div>
                      {!ok && <div className="text-xs text-amber-700">
                        need {cur.min_per_label - n} more
                      </div>}
                    </div>
                  )
                })}
              </div>
            </div>

            {/* Top stores bar chart */}
            <div className="mb-4">
              <div className="text-sm text-slate-500 mb-2">
                Top contributing stores ({cur.stores_contributing.length} total)
              </div>
              {topStores.length === 0 && (
                <div className="text-slate-400 text-sm">No store data yet.</div>
              )}
              <div className="space-y-1.5">
                {topStores.map(s => (
                  <div key={s.store_id} className="flex items-center gap-2 text-sm">
                    <div className="w-40 truncate text-slate-700">
                      {s.store_name || `Store #${s.store_id}`}
                    </div>
                    <div className="flex-1 bg-slate-100 h-5 rounded overflow-hidden">
                      <div
                        className="h-full bg-sky-500"
                        style={{ width: `${(s.total / maxStoreCount) * 100}%` }} />
                    </div>
                    <div className="w-12 text-right tabular-nums text-slate-600">
                      {s.total}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="flex gap-2 items-center">
              <Button
                onClick={startTraining}
                disabled={busy || training || !cur.ready_to_train}>
                {training ? 'Training…' : 'Train chain model'}
              </Button>
              <Button variant="ghost" onClick={() => { loadSummary(); loadStatus(detector); loadModels(detector) }}>
                Refresh
              </Button>
              {!cur.ready_to_train && (
                <span className="text-xs text-amber-700">
                  Collect more frames for: {Object.keys(cur.short_labels).join(', ')}
                </span>
              )}
            </div>
          </>
        )}
      </Card>

      {/* Training status */}
      <Card className="p-4 mb-4">
        <div className="font-medium mb-2">Training status</div>
        <div className="text-sm">
          <div className="mb-1">
            <span className="text-slate-500">State: </span>
            <span className="font-medium">{status.state}</span>
          </div>
          {status.message && <div className="text-slate-700 mb-1">{status.message}</div>}
          {training && status.total_epochs && (
            <div className="mt-2">
              <div className="bg-slate-100 h-2 rounded overflow-hidden">
                <div
                  className="h-full bg-emerald-500 transition-all"
                  style={{ width: `${((status.epoch ?? 0) / status.total_epochs) * 100}%` }} />
              </div>
              <div className="text-xs text-slate-500 mt-1">
                Epoch {status.epoch ?? 0} / {status.total_epochs}
              </div>
            </div>
          )}
          {status.state === 'done' && status.report && (
            <div className="mt-3 grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
              <Stat label="Accuracy"  value={fmtPct(status.report.accuracy)} />
              <Stat label="Precision" value={fmtPct(status.report.precision_macro)} />
              <Stat label="Recall"    value={fmtPct(status.report.recall_macro)} />
              <Stat label="Samples"   value={String(status.sample_count ?? '—')} />
              {status.report.warning && (
                <div className="col-span-full text-xs text-amber-700">
                  ⚠️ {status.report.warning}
                </div>
              )}
              {status.report.recommendation && (
                <div className="col-span-full text-xs text-slate-600">
                  {status.report.recommendation}
                </div>
              )}
            </div>
          )}
          {status.state === 'done' && status.stats && (
            <div className="mt-3 text-xs text-slate-500">
              Filtered: dropped {status.stats.dropped_blurry ?? 0} blurry,
              {' '}{status.stats.dropped_dup ?? 0} duplicates,
              {' '}{status.stats.dropped_capped ?? 0} over per-store cap.
            </div>
          )}
        </div>
      </Card>

      {/* Chain models / deployment history */}
      <Card className="p-4">
        <div className="font-medium mb-3">Chain models — {detector}</div>
        {models.length === 0 && (
          <div className="text-slate-400 text-sm">
            No chain models yet. Train one above to deploy fleet-wide.
          </div>
        )}
        {models.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-slate-50 text-slate-600">
                <tr>
                  <th className="text-left p-2">Version</th>
                  <th className="text-left p-2">Trained</th>
                  <th className="text-right p-2">Samples</th>
                  <th className="text-right p-2">Stores</th>
                  <th className="text-right p-2">Accuracy</th>
                  <th className="text-left p-2">Status</th>
                  <th className="p-2"></th>
                </tr>
              </thead>
              <tbody>
                {models.map(m => (
                  <tr key={m.id} className="border-t">
                    <td className="p-2 font-medium">{m.version}</td>
                    <td className="p-2 text-slate-600">
                      {m.created_at ? new Date(m.created_at).toLocaleString() : '—'}
                    </td>
                    <td className="p-2 text-right tabular-nums">{m.sample_count ?? '—'}</td>
                    <td className="p-2 text-right tabular-nums">{m.trained_on_stores.length}</td>
                    <td className="p-2 text-right tabular-nums">{fmtPct(m.accuracy)}</td>
                    <td className="p-2">
                      {m.deployed
                        ? <Badge color="green">Live</Badge>
                        : <Badge color="slate">Archived</Badge>}
                    </td>
                    <td className="p-2 text-right">
                      {!m.deployed && (
                        <Button
                          variant="ghost"
                          disabled={busy}
                          onClick={() => deployModel(m.id)}>
                          Deploy fleet-wide
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="p-2 rounded bg-slate-50">
      <div className="text-xs text-slate-500">{label}</div>
      <div className="text-lg font-semibold tabular-nums">{value}</div>
    </div>
  )
}
