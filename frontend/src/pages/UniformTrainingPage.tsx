// Uniform-compliance training-data collection + per-store model training.
//
// Operators pick any AI camera (filtered by store), watch the live
// thumbnail, and capture frames tagged uniform_ok / uniform_violation /
// no_lanyard / civilian. Both Vivo uniform colours (black + burgundy)
// count as uniform_ok. A review grid lets them delete bad frames; once
// >=30 per class are collected they can train a YOLOv8n-cls model.

import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Button, Card, PageHeader } from '@/components/ui/Primitives'
import { api } from '@/api/client'

type Label = 'uniform_ok' | 'uniform_violation' | 'no_lanyard' | 'civilian'
const LABELS: Label[] = ['uniform_ok', 'uniform_violation', 'no_lanyard', 'civilian']
const LABEL_TEXT: Record<Label, string> = {
  uniform_ok: '✅ Uniform OK',
  uniform_violation: '❌ Violation',
  no_lanyard: '⚠️ No lanyard',
  civilian: '👤 Customer',
}
const LABEL_STYLE: Record<Label, string> = {
  uniform_ok:        'bg-emerald-600 hover:bg-emerald-700',
  uniform_violation: 'bg-red-600 hover:bg-red-700',
  no_lanyard:        'bg-amber-500 hover:bg-amber-600',
  civilian:          'bg-slate-500 hover:bg-slate-600',
}
const MIN_PER_CLASS = 30

interface UniformCamera {
  camera_id: number
  camera_name: string
  store_id: number | null
  store_name: string | null
  has_counter_zone: boolean
  counts: Record<Label, number>
}
interface Sample {
  id: number
  label: Label
  captured_at: string | null
  file_url: string
}
interface TrainStatus {
  state: 'idle' | 'preparing' | 'training' | 'done' | 'failed'
  message?: string
  epoch?: number
  total_epochs?: number
  model_id?: number
  candidate_cameras?: number[]
  report?: {
    accuracy: number | null
    per_class?: Record<string, { precision: number; recall: number }>
    recommendation?: string
  }
}

export default function UniformTrainingPage() {
  const [cameras, setCameras] = useState<UniformCamera[]>([])
  const [selected, setSelected] = useState<number | null>(null)
  const [snap, setSnap] = useState<string | null>(null)
  const [samples, setSamples] = useState<Sample[]>([])
  const [reviewLabel, setReviewLabel] = useState<Label>('uniform_ok')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [autoCapture, setAutoCapture] = useState(false)
  const autoTimer = useRef<ReturnType<typeof setInterval> | null>(null)
  const [train, setTrain] = useState<TrainStatus>({ state: 'idle' })
  const [storeFilter, setStoreFilter] = useState<string>('')
  const [session, setSession] = useState<number[]>([])

  const current = cameras.find(c => c.camera_id === selected) || null
  const storeId = current?.store_id ?? null

  const storeOptions = (() => {
    const seen = new Map<number, string>()
    for (const c of cameras) if (c.store_id != null) seen.set(c.store_id, c.store_name || `Store ${c.store_id}`)
    return [...seen.entries()].sort((a, b) => a[1].localeCompare(b[1]))
  })()
  const visibleCameras = storeFilter
    ? cameras.filter(c => String(c.store_id) === storeFilter)
    : cameras
  const grouped = (() => {
    const m = new Map<string, UniformCamera[]>()
    for (const c of visibleCameras) {
      const key = c.store_name || 'Unassigned'
      if (!m.has(key)) m.set(key, [])
      m.get(key)!.push(c)
    }
    return [...m.entries()]
  })()

  function loadCameras() {
    api<UniformCamera[]>('/training/uniform/cameras')
      .then(rows => {
        setCameras(rows)
        setSelected(prev => prev ?? (rows.length ? rows[0].camera_id : null))
      })
      .catch(e => setMsg(String(e)))
  }
  useEffect(loadCameras, [])  // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (selected != null) setSession(prev => prev.includes(selected) ? prev : [...prev, selected])
  }, [selected])

  function selectCamera(id: number) { setSelected(id); setSnap(null) }
  function pickFirstInStore(filter: string) {
    setStoreFilter(filter)
    const list = filter ? cameras.filter(c => String(c.store_id) === filter) : cameras
    if (list.length && !list.some(c => c.camera_id === selected)) selectCamera(list[0].camera_id)
  }

  useEffect(() => {
    if (selected === null) return
    let alive = true
    const grab = () => {
      api<{ jpeg_b64: string }>(`/cameras/${selected}/snapshot`)
        .then(s => { if (alive) setSnap(s.jpeg_b64) })
        .catch(() => { if (alive) setSnap(null) })
    }
    grab()
    const t = setInterval(grab, 5_000)
    return () => { alive = false; clearInterval(t) }
  }, [selected])

  function loadSamples() {
    if (selected === null) return
    api<Sample[]>(`/training/uniform/samples?camera_id=${selected}&label=${reviewLabel}`)
      .then(setSamples).catch(() => setSamples([]))
  }
  useEffect(loadSamples, [selected, reviewLabel])  // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (storeId === null) return
    let alive = true
    const poll = () => {
      api<TrainStatus>(`/training/uniform/train/status?store_id=${storeId}`)
        .then(s => { if (alive) setTrain(s) }).catch(() => {})
    }
    poll()
    const t = setInterval(poll, 4_000)
    return () => { alive = false; clearInterval(t) }
  }, [storeId])

  async function capture(label: Label) {
    if (selected === null) return
    setBusy(true); setMsg(null)
    try {
      await api(`/training/uniform/capture?camera_id=${selected}&label=${label}`, { method: 'POST' })
      setMsg(`Captured a ${LABEL_TEXT[label]} frame`)
      loadCameras()
      if (label === reviewLabel) loadSamples()
    } catch (e) { setMsg(`Capture failed: ${e}`) } finally { setBusy(false) }
  }

  async function del(id: number) {
    try {
      await api(`/training/uniform/samples/${id}`, { method: 'DELETE' })
      setSamples(s => s.filter(x => x.id !== id))
      loadCameras()
    } catch { /* ignore */ }
  }

  useEffect(() => {
    if (autoTimer.current) { clearInterval(autoTimer.current); autoTimer.current = null }
    if (!autoCapture || selected === null) return
    // Passive build: grab a frame every 10 min tagged civilian; the
    // operator re-tags from the review grid.
    autoTimer.current = setInterval(() => { capture('civilian') }, 10 * 60_000)
    return () => { if (autoTimer.current) clearInterval(autoTimer.current) }
  }, [autoCapture, selected])  // eslint-disable-line react-hooks/exhaustive-deps

  async function startTraining() {
    if (storeId === null) return
    setMsg(null)
    try {
      await api(`/training/uniform/train?store_id=${storeId}`, { method: 'POST' })
      setTrain({ state: 'preparing', message: 'Queued…' })
    } catch (e) { setMsg(`Could not start training: ${e}`) }
  }

  async function deploy() {
    if (!train.model_id || !train.candidate_cameras?.length) return
    try {
      await api('/training/uniform/deploy', {
        method: 'POST',
        body: { model_id: train.model_id, camera_ids: train.candidate_cameras },
      })
      setMsg(`Deployed model #${train.model_id} to ${train.candidate_cameras.length} camera(s).`)
    } catch (e) { setMsg(`Deploy failed: ${e}`) }
  }

  const counts = current?.counts ?? { uniform_ok: 0, uniform_violation: 0, no_lanyard: 0, civilian: 0 }
  const ready = LABELS.every(l => counts[l] >= MIN_PER_CLASS)
  const training = train.state === 'preparing' || train.state === 'training'

  return (
    <div className="p-6 space-y-4">
      <PageHeader
        title="Uniform compliance training"
        actions={<Link to="/training"><Button variant="ghost">← Training Studio</Button></Link>}
      />

      {/* Capture tips */}
      <Card className="p-3 bg-sky-50 border-sky-200 text-sm text-sky-900">
        📸 <strong>Tips for good training data:</strong>
        <ul className="list-disc ml-5 mt-1 text-xs space-y-0.5">
          <li>Capture staff at the counter from the camera's angle.</li>
          <li>Capture in different lighting (morning, midday, evening).</li>
          <li>Capture BOTH black AND burgundy uniforms as “Uniform OK”.</li>
          <li>At least {MIN_PER_CLASS} examples per class (50 recommended).</li>
          <li>For violations, ask staff to briefly remove the lanyard for the shot.</li>
        </ul>
      </Card>

      {/* How staff identification works — so operators know what this unlocks. */}
      <Card className="p-3 bg-slate-50 text-sm text-slate-700">
        👔 <strong>How staff identification works</strong>
        <ul className="list-disc ml-5 mt-1 text-xs space-y-0.5">
          <li>A person in a <strong>counter / staff zone</strong> wearing a Vivo
              uniform (black or burgundy) + lanyard + name tag is treated as
              <strong> staff</strong> and excluded from the customer count.</li>
          <li>In uniform but missing the lanyard / name tag → still staff, but an
              <strong> INFO</strong> "missing name tag" reminder after 5 minutes.</li>
          <li>At the counter with no uniform → <strong>ATTENTION</strong>
              "unidentified person at counter" after 2 minutes.</li>
          <li>Works out of the box on colour rules. Collecting frames here and
              training a model makes it far more accurate for your cameras.</li>
          <li>Remember to draw a <strong>counter</strong> zone on the camera —
              that's how the system knows where staff stand.</li>
        </ul>
      </Card>

      {cameras.length === 0 ? (
        <Card className="p-6 text-sm text-slate-600">
          No AI-enabled cameras found. Enable AI on a camera that watches the
          counter, then collect uniform frames here.
        </Card>
      ) : (
        <>
          <Card className="p-3">
            <div className="flex flex-wrap items-center gap-3">
              <span className="text-xs uppercase tracking-wide text-slate-500">Store</span>
              <select className="border rounded px-2 py-1 text-sm"
                      value={storeFilter} onChange={e => pickFirstInStore(e.target.value)}>
                <option value="">All stores</option>
                {storeOptions.map(([id, name]) => <option key={id} value={String(id)}>{name}</option>)}
              </select>
              <span className="text-xs uppercase tracking-wide text-slate-500">Camera</span>
              <select className="border rounded px-2 py-1 text-sm min-w-[220px]"
                      value={selected ?? ''} onChange={e => selectCamera(Number(e.target.value))}>
                {grouped.map(([storeName, cams]) => (
                  <optgroup key={storeName} label={storeName}>
                    {cams.map(c => (
                      <option key={c.camera_id} value={c.camera_id}>
                        {storeName} - {c.camera_name}{c.has_counter_zone ? ' ✅' : ' ⚠️'}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>
              <label className="ml-auto flex items-center gap-1.5 text-xs text-slate-600">
                <input type="checkbox" checked={autoCapture}
                       onChange={e => setAutoCapture(e.target.checked)} />
                Auto-capture every 10 min
              </label>
            </div>
            {current && (
              <div className={'mt-2 text-xs rounded px-2 py-1 inline-block ' +
                (current.has_counter_zone ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-700')}>
                {current.has_counter_zone
                  ? '✅ Counter zone configured — compliance will be scored here.'
                  : '⚠️ No counter/staff zone — draw one so the detector knows where staff stand.'}
              </div>
            )}
          </Card>

          {session.length > 1 && (
            <Card className="p-3">
              <div className="text-xs uppercase tracking-wide text-slate-500 mb-2">This session</div>
              <div className="flex flex-wrap gap-3 text-xs">
                {session.map(id => {
                  const cam = cameras.find(c => c.camera_id === id)
                  if (!cam) return null
                  const tot = Math.min(...LABELS.map(l => cam.counts[l]))
                  const done = LABELS.every(l => cam.counts[l] >= MIN_PER_CLASS)
                  return (
                    <button key={id} onClick={() => selectCamera(id)}
                            className={'rounded px-2 py-1 border ' +
                              (id === selected ? 'border-blue-400 bg-blue-50' : 'border-slate-200')}>
                      <span className="font-medium">{cam.camera_name}</span>{': '}{tot}/{MIN_PER_CLASS} {done ? '✅' : '🔄'}
                    </button>
                  )
                })}
              </div>
            </Card>
          )}

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <Card className="p-3">
              <div className="relative w-full bg-slate-900 rounded overflow-hidden">
                {snap ? (
                  <img src={`data:image/jpeg;base64,${snap}`} className="block w-full h-auto" alt="camera" />
                ) : (
                  <div className="aspect-video flex items-center justify-center text-slate-400 text-sm">
                    Loading live frame…
                  </div>
                )}
              </div>
              <div className="mt-3 grid grid-cols-2 gap-2">
                {LABELS.map(l => (
                  <button key={l} disabled={busy} onClick={() => capture(l)}
                          className={'text-white rounded py-2 text-sm font-medium disabled:opacity-50 ' + LABEL_STYLE[l]}>
                    {LABEL_TEXT[l]}
                  </button>
                ))}
              </div>
              {msg && <div className="text-xs text-slate-500 mt-2">{msg}</div>}
            </Card>

            <Card className="p-3">
              <div className="text-sm font-medium mb-2">Collected for {current?.camera_name}</div>
              <div className="space-y-2">
                {LABELS.map(l => {
                  const n = counts[l]
                  const pct = Math.min(100, (n / MIN_PER_CLASS) * 100)
                  return (
                    <div key={l}>
                      <div className="flex justify-between text-xs mb-0.5">
                        <span>{LABEL_TEXT[l]}</span>
                        <span className={n >= MIN_PER_CLASS ? 'text-emerald-600' : 'text-slate-500'}>
                          {n} / {MIN_PER_CLASS}
                        </span>
                      </div>
                      <div className="h-2 bg-slate-100 rounded">
                        <div className={'h-2 rounded ' + LABEL_STYLE[l].split(' ')[0]}
                             style={{ width: `${Math.max(pct, 2)}%` }} />
                      </div>
                    </div>
                  )
                })}
              </div>
              <div className={'mt-3 text-sm rounded px-3 py-2 ' +
                (ready ? 'bg-emerald-50 text-emerald-800' : 'bg-slate-50 text-slate-600')}>
                {ready ? '✅ Enough frames to train.'
                       : `Collect at least ${MIN_PER_CLASS} frames per class to enable training.`}
              </div>

              <div className="mt-3 border-t pt-3">
                <div className="flex items-center gap-3">
                  <Button onClick={startTraining} disabled={!ready || training}>
                    {training ? 'Training…' : 'Start training'}
                  </Button>
                  {train.state === 'training' && train.total_epochs ? (
                    <span className="text-xs text-slate-600">Epoch {train.epoch ?? 0}/{train.total_epochs}</span>
                  ) : train.state === 'preparing' ? (
                    <span className="text-xs text-slate-600">{train.message ?? 'Preparing…'}</span>
                  ) : null}
                </div>
                {train.state === 'training' && train.total_epochs ? (
                  <div className="h-2 bg-slate-100 rounded mt-2">
                    <div className="h-2 rounded bg-blue-500"
                         style={{ width: `${Math.max(2, ((train.epoch ?? 0) / train.total_epochs) * 100)}%` }} />
                  </div>
                ) : null}
                {train.state === 'failed' && <div className="text-xs text-red-600 mt-2">{train.message}</div>}
                {train.state === 'done' && train.report && (
                  <div className="mt-3 text-sm bg-slate-50 rounded p-3">
                    <div className="font-medium mb-1">Training complete</div>
                    <div className="text-xs space-y-0.5 text-slate-700">
                      {train.report.accuracy !== null && (
                        <div>Accuracy: <strong>{Math.round((train.report.accuracy ?? 0) * 100)}%</strong></div>
                      )}
                      {train.report.per_class && Object.entries(train.report.per_class).map(([l, pc]) => (
                        <div key={l}>{l}: {Math.round(pc.precision * 100)}% precision · {Math.round(pc.recall * 100)}% recall</div>
                      ))}
                      <div className="mt-1 text-slate-600">{train.report.recommendation}</div>
                    </div>
                    {train.model_id && train.candidate_cameras?.length ? (
                      <Button className="mt-2" onClick={deploy}>
                        Deploy to {train.candidate_cameras.length} camera(s)
                      </Button>
                    ) : null}
                  </div>
                )}
              </div>
            </Card>
          </div>

          <Card className="p-3">
            <div className="flex items-center gap-3 mb-3">
              <span className="text-sm font-medium">Review frames</span>
              <div className="flex gap-1 flex-wrap">
                {LABELS.map(l => (
                  <button key={l} onClick={() => setReviewLabel(l)}
                          className={'px-2.5 py-1 rounded text-xs font-medium ' +
                            (reviewLabel === l ? 'text-white ' + LABEL_STYLE[l].split(' ')[0] : 'bg-slate-100 text-slate-600')}>
                    {LABEL_TEXT[l]}
                  </button>
                ))}
              </div>
              <span className="text-xs text-slate-400 ml-auto">Click a frame to delete it</span>
            </div>
            {samples.length === 0 ? (
              <div className="text-sm text-slate-500 py-6 text-center">No {LABEL_TEXT[reviewLabel]} frames yet.</div>
            ) : (
              <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 gap-2">
                {samples.map(s => (
                  <button key={s.id} onClick={() => del(s.id)}
                          title={`Captured ${s.captured_at ?? ''} — click to delete`}
                          className="group relative rounded overflow-hidden border hover:border-red-400">
                    <img src={s.file_url} className="block w-full h-20 object-cover" alt="" />
                    <span className="absolute inset-0 bg-red-600/0 group-hover:bg-red-600/40 flex items-center justify-center text-white text-xs opacity-0 group-hover:opacity-100">Delete</span>
                  </button>
                ))}
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  )
}
