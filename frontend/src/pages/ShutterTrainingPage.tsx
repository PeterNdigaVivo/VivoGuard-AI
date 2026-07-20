// Shutter / door-status training-data collection.
//
// Operators pick a camera that has a 'shutter' zone, watch the live
// thumbnail, and capture frames tagged OPEN / CLOSED / PARTIAL. The
// frames feed the YOLOv8n-cls training pipeline (separate page). A
// review grid lets them delete bad frames. Auto-capture grabs one
// frame every 5 minutes for passive dataset building.

import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Button, Card, PageHeader } from '@/components/ui/Primitives'
import { api } from '@/api/client'

type Label = 'open' | 'closed' | 'partial'
const LABELS: Label[] = ['open', 'closed', 'partial']
const LABEL_STYLE: Record<Label, string> = {
  open:    'bg-emerald-600 hover:bg-emerald-700',
  closed:  'bg-red-600 hover:bg-red-700',
  partial: 'bg-amber-500 hover:bg-amber-600',
}
const MIN_PER_CLASS = 50

interface ShutterCamera {
  camera_id: number
  camera_name: string
  store_id: number | null
  store_name: string | null
  has_shutter_zone: boolean
  counts: { open: number; closed: number; partial: number }
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
    precision_macro?: number
    recall_macro?: number
    per_class?: Record<string, { precision: number; recall: number }>
    recommendation?: string
  }
}

export default function ShutterTrainingPage() {
  const [cameras, setCameras] = useState<ShutterCamera[]>([])
  const [selected, setSelected] = useState<number | null>(null)
  const [snap, setSnap] = useState<string | null>(null)
  const [samples, setSamples] = useState<Sample[]>([])
  const [reviewLabel, setReviewLabel] = useState<Label>('open')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [autoCapture, setAutoCapture] = useState(false)
  const autoTimer = useRef<ReturnType<typeof setInterval> | null>(null)
  const [train, setTrain] = useState<TrainStatus>({ state: 'idle' })
  // "" = All stores; otherwise the store_id as a string.
  const [storeFilter, setStoreFilter] = useState<string>('')
  // Cameras the operator has added to this collection session
  // (for the multi-camera progress strip). The currently-selected
  // camera is always part of the session.
  const [session, setSession] = useState<number[]>([])

  const current = cameras.find(c => c.camera_id === selected) || null
  const storeId = current?.store_id ?? null

  // Stores present in the camera list, for the filter dropdown.
  const storeOptions = (() => {
    const seen = new Map<number, string>()
    for (const c of cameras) {
      if (c.store_id != null) seen.set(c.store_id, c.store_name || `Store ${c.store_id}`)
    }
    return [...seen.entries()].sort((a, b) => a[1].localeCompare(b[1]))
  })()

  // Cameras after the store filter, grouped by store for the <optgroup>s.
  const visibleCameras = storeFilter
    ? cameras.filter(c => String(c.store_id) === storeFilter)
    : cameras
  const grouped = (() => {
    const m = new Map<string, ShutterCamera[]>()
    for (const c of visibleCameras) {
      const key = c.store_name || 'Unassigned'
      if (!m.has(key)) m.set(key, [])
      m.get(key)!.push(c)
    }
    return [...m.entries()]
  })()

  function loadCameras() {
    api<ShutterCamera[]>('/training/shutter/cameras')
      .then(rows => {
        setCameras(rows)
        setSelected(prev => prev ?? (rows.length ? rows[0].camera_id : null))
      })
      .catch(e => setMsg(String(e)))
  }
  useEffect(loadCameras, [])  // eslint-disable-line react-hooks/exhaustive-deps

  // Keep the selected camera in the session, and ensure the selection
  // is valid for the current store filter.
  useEffect(() => {
    if (selected != null) {
      setSession(prev => prev.includes(selected) ? prev : [...prev, selected])
    }
  }, [selected])

  function selectCamera(id: number) {
    setSelected(id)
    setSnap(null)
  }
  function pickFirstInStore(filter: string) {
    setStoreFilter(filter)
    const list = filter ? cameras.filter(c => String(c.store_id) === filter) : cameras
    if (list.length && !list.some(c => c.camera_id === selected)) {
      selectCamera(list[0].camera_id)
    }
  }

  // Live thumbnail of the selected camera, refreshed every 5s.
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

  // Sample review grid for the selected camera + label.
  function loadSamples() {
    if (selected === null) return
    api<Sample[]>(`/training/shutter/samples?camera_id=${selected}&label=${reviewLabel}`)
      .then(setSamples).catch(() => setSamples([]))
  }
  useEffect(loadSamples, [selected, reviewLabel])  // eslint-disable-line react-hooks/exhaustive-deps

  async function capture(label: Label) {
    if (selected === null) return
    setBusy(true); setMsg(null)
    try {
      await api(`/training/shutter/capture?camera_id=${selected}&label=${label}`,
                { method: 'POST' })
      setMsg(`Captured a ${label.toUpperCase()} frame`)
      loadCameras()
      if (label === reviewLabel) loadSamples()
    } catch (e) {
      setMsg(`Capture failed: ${e}`)
    } finally {
      setBusy(false)
    }
  }

  async function uploadFiles(label: Label, files: FileList | File[]) {
    if (selected === null) { setMsg('Pick a camera first.'); return }
    setBusy(true); setMsg(null)
    const form = new FormData()
    form.append('label', label)
    form.append('camera_id', String(selected))
    if (current?.store_id != null) form.append('store_id', String(current.store_id))
    for (const f of Array.from(files)) form.append('files', f, f.name)
    try {
      const tok = localStorage.getItem('vg_access_token') ?? ''
      const res = await fetch('/api/training/shutter/upload', {
        method: 'POST',
        headers: { Authorization: `Bearer ${tok}` },
        body: form,
      })
      if (!res.ok) throw new Error(await res.text())
      const r = await res.json()
      const errs = (r.errors || []).length
      setMsg(`Uploaded ${r.saved} ${label.toUpperCase()} image${r.saved === 1 ? '' : 's'}`
             + (errs ? ` · ${errs} rejected (size/type)` : ''))
      loadCameras()
      if (label === reviewLabel) loadSamples()
    } catch (e) { setMsg(`Upload failed: ${e}`) } finally { setBusy(false) }
  }

  async function del(id: number) {
    try {
      await api(`/training/shutter/samples/${id}`, { method: 'DELETE' })
      setSamples(s => s.filter(x => x.id !== id))
      loadCameras()
    } catch { /* ignore */ }
  }

  // Auto-capture: grab one UNLABELLED-by-default frame every 5 min.
  // The operator still labels it — auto mode just grabs the frame so
  // dataset-building can happen passively. We default it to 'partial'
  // so it's never silently mislabelled as a clean open/closed; the
  // operator re-tags from the review grid.
  useEffect(() => {
    if (autoTimer.current) { clearInterval(autoTimer.current); autoTimer.current = null }
    if (!autoCapture || selected === null) return
    autoTimer.current = setInterval(() => { capture('partial') }, 5 * 60_000)
    return () => { if (autoTimer.current) clearInterval(autoTimer.current) }
  }, [autoCapture, selected])  // eslint-disable-line react-hooks/exhaustive-deps

  // Poll training status for the selected store while a job is live.
  useEffect(() => {
    if (storeId === null) return
    let alive = true
    const poll = () => {
      api<TrainStatus>(`/training/shutter/train/status?store_id=${storeId}`)
        .then(s => { if (alive) setTrain(s) })
        .catch(() => {})
    }
    poll()
    const t = setInterval(poll, 4_000)
    return () => { alive = false; clearInterval(t) }
  }, [storeId])

  async function startTraining() {
    if (storeId === null) return
    setMsg(null)
    try {
      await api(`/training/shutter/train?store_id=${storeId}`, { method: 'POST' })
      setTrain({ state: 'preparing', message: 'Queued…' })
    } catch (e) {
      setMsg(`Could not start training: ${e}`)
    }
  }

  async function deploy() {
    if (!train.model_id || !train.candidate_cameras?.length) return
    try {
      await api('/training/shutter/deploy', {
        method: 'POST',
        body: { model_id: train.model_id, camera_ids: train.candidate_cameras },
      })
      setMsg(`Deployed model #${train.model_id} to ${train.candidate_cameras.length} camera(s).`)
    } catch (e) {
      setMsg(`Deploy failed: ${e}`)
    }
  }

  const counts = current?.counts ?? { open: 0, closed: 0, partial: 0 }
  const ready = counts.open >= MIN_PER_CLASS &&
                counts.closed >= MIN_PER_CLASS &&
                counts.partial >= MIN_PER_CLASS
  const training = train.state === 'preparing' || train.state === 'training'

  return (
    <div className="p-6 space-y-4">
      <PageHeader
        title="Shutter training data"
        actions={<Link to="/training"><Button variant="ghost">← Training Studio</Button></Link>}
      />

      {cameras.length === 0 ? (
        <Card className="p-6 text-sm text-slate-600 dark:text-slate-300">
          No AI-enabled cameras found. Enable AI on a camera that watches a
          store entrance, then come back here to collect OPEN / CLOSED /
          PARTIAL frames.
        </Card>
      ) : (
        <>
          {/* Camera picker — store filter + grouped, all AI cameras. */}
          <Card className="p-3">
            <div className="flex flex-wrap items-center gap-3">
              <span className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-300">Store</span>
              <select className="border rounded px-2 py-1 text-sm"
                      value={storeFilter}
                      onChange={e => pickFirstInStore(e.target.value)}>
                <option value="">All stores</option>
                {storeOptions.map(([id, name]) => (
                  <option key={id} value={String(id)}>{name}</option>
                ))}
              </select>

              <span className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-300">Camera</span>
              <select className="border rounded px-2 py-1 text-sm min-w-[220px]"
                      value={selected ?? ''}
                      onChange={e => selectCamera(Number(e.target.value))}>
                {grouped.map(([storeName, cams]) => (
                  <optgroup key={storeName} label={storeName}>
                    {cams.map(c => (
                      <option key={c.camera_id} value={c.camera_id}>
                        {storeName} - {c.camera_name}
                        {c.has_shutter_zone ? ' ✅' : ' ⚠️'}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>

              <label className="ml-auto flex items-center gap-1.5 text-xs text-slate-600 dark:text-slate-300">
                <input type="checkbox" checked={autoCapture}
                       onChange={e => setAutoCapture(e.target.checked)} />
                Auto-capture every 5 min
              </label>
            </div>

            {/* Zone status reminder for the selected camera. */}
            {current && (
              <div className={'mt-2 text-xs rounded px-2 py-1 inline-block ' +
                (current.has_shutter_zone
                  ? 'bg-emerald-50 text-emerald-700'
                  : 'bg-amber-50 text-amber-700')}>
                {current.has_shutter_zone
                  ? '✅ Shutter zone configured — ready to detect once a model is deployed.'
                  : '⚠️ No shutter zone yet — remember to draw a “shutter” zone on this camera after training.'}
              </div>
            )}
          </Card>

          {/* Multi-camera session progress strip. */}
          {session.length > 1 && (
            <Card className="p-3">
              <div className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-300 mb-2">
                This session
              </div>
              <div className="flex flex-wrap gap-3 text-xs">
                {session.map(id => {
                  const cam = cameras.find(c => c.camera_id === id)
                  if (!cam) return null
                  const tot = Math.min(cam.counts.open, cam.counts.closed, cam.counts.partial)
                  const done = cam.counts.open >= MIN_PER_CLASS &&
                               cam.counts.closed >= MIN_PER_CLASS &&
                               cam.counts.partial >= MIN_PER_CLASS
                  return (
                    <button key={id} onClick={() => selectCamera(id)}
                            className={'rounded px-2 py-1 border ' +
                              (id === selected ? 'border-blue-400 bg-blue-50' : 'border-slate-200')}>
                      <span className="font-medium">{cam.camera_name}</span>
                      {': '}{tot}/{MIN_PER_CLASS} {done ? '✅' : '🔄'}
                    </button>
                  )
                })}
              </div>
            </Card>
          )}

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {/* Live + capture */}
            <Card className="p-3">
              <div className="relative w-full bg-slate-900 rounded overflow-hidden">
                {snap ? (
                  <img src={`data:image/jpeg;base64,${snap}`}
                       className="block w-full h-auto" alt="camera" />
                ) : (
                  <div className="aspect-video flex items-center justify-center text-slate-400 text-sm">
                    Loading live frame…
                  </div>
                )}
              </div>
              <div className="mt-3 grid grid-cols-3 gap-2">
                {LABELS.map(l => (
                  <button key={l} disabled={busy}
                          onClick={() => capture(l)}
                          className={'text-white rounded py-2 text-sm font-medium disabled:opacity-50 ' + LABEL_STYLE[l]}>
                    Capture {l.toUpperCase()}
                  </button>
                ))}
              </div>
              {msg && <div className="text-xs text-slate-500 dark:text-slate-300 mt-2">{msg}</div>}

              {/* Image upload — phone photos / screenshots / WhatsApp.
                  JPG/PNG, 5 MB cap per file. */}
              <div className="mt-3 border-t dark:border-slate-800 pt-3">
                <div className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-300 mb-1">
                  Upload training images
                </div>
                <div className="grid grid-cols-3 gap-2">
                  {LABELS.map(l => (
                    <label key={l}
                           className={'text-white rounded py-2 text-xs font-medium cursor-pointer text-center '
                                      + (busy ? 'opacity-50 cursor-not-allowed ' : '')
                                      + LABEL_STYLE[l].split(' ')[0]}>
                      📤 {l.toUpperCase()}
                      <input type="file" multiple accept="image/jpeg,image/jpg,image/png"
                             className="hidden" disabled={busy}
                             onChange={e => {
                               if (e.target.files && e.target.files.length) {
                                 uploadFiles(l, e.target.files)
                                 e.target.value = ''
                               }
                             }} />
                    </label>
                  ))}
                </div>
                <div className="text-[11px] text-slate-400 mt-1">
                  JPG / PNG · up to 5 MB each · phone photos welcome
                </div>
              </div>
            </Card>

            {/* Progress */}
            <Card className="p-3">
              <div className="text-sm font-medium mb-2">
                Collected for {current?.camera_name}
              </div>
              <div className="space-y-2">
                {LABELS.map(l => {
                  const n = counts[l]
                  const pct = Math.min(100, (n / MIN_PER_CLASS) * 100)
                  return (
                    <div key={l}>
                      <div className="flex justify-between text-xs mb-0.5">
                        <span className="capitalize">{l}</span>
                        <span className={n >= MIN_PER_CLASS ? 'text-emerald-600' : 'text-slate-500 dark:text-slate-300'}>
                          {n} / {MIN_PER_CLASS}
                        </span>
                      </div>
                      <div className="h-2 bg-slate-100 dark:bg-slate-800 rounded">
                        <div className={'h-2 rounded ' + LABEL_STYLE[l].split(' ')[0]}
                             style={{ width: `${Math.max(pct, 2)}%` }} />
                      </div>
                    </div>
                  )
                })}
              </div>
              <div className={'mt-3 text-sm rounded px-3 py-2 ' +
                (ready ? 'bg-emerald-50 text-emerald-800' : 'bg-slate-50 text-slate-600 dark:bg-slate-800 dark:text-slate-300')}>
                {ready
                  ? '✅ Enough frames to train.'
                  : `Collect at least ${MIN_PER_CLASS} frames per class to enable training.`}
              </div>

              {/* Training control + live progress + report. */}
              <div className="mt-3 border-t dark:border-slate-800 pt-3">
                <div className="flex items-center gap-3">
                  <Button onClick={startTraining} disabled={!ready || training}>
                    {training ? 'Training…' : 'Start training'}
                  </Button>
                  {train.state === 'training' && train.total_epochs ? (
                    <span className="text-xs text-slate-600 dark:text-slate-300">
                      Epoch {train.epoch ?? 0}/{train.total_epochs}
                    </span>
                  ) : train.state === 'preparing' ? (
                    <span className="text-xs text-slate-600 dark:text-slate-300">{train.message ?? 'Preparing…'}</span>
                  ) : null}
                </div>

                {train.state === 'training' && train.total_epochs ? (
                  <div className="h-2 bg-slate-100 dark:bg-slate-800 rounded mt-2">
                    <div className="h-2 rounded bg-blue-500"
                         style={{ width: `${Math.max(2, ((train.epoch ?? 0) / train.total_epochs) * 100)}%` }} />
                  </div>
                ) : null}

                {train.state === 'failed' && (
                  <div className="text-xs text-red-600 mt-2">{train.message}</div>
                )}

                {train.state === 'done' && train.report && (
                  <div className="mt-3 text-sm bg-slate-50 dark:bg-slate-800 rounded p-3">
                    <div className="font-medium mb-1">Training complete</div>
                    <div className="text-xs space-y-0.5 text-slate-700 dark:text-slate-200">
                      {train.report.accuracy !== null && (
                        <div>Accuracy: <strong>{Math.round((train.report.accuracy ?? 0) * 100)}%</strong></div>
                      )}
                      {train.report.per_class && Object.entries(train.report.per_class).map(([l, pc]) => (
                        <div key={l} className="capitalize">
                          {l}: {Math.round(pc.precision * 100)}% precision · {Math.round(pc.recall * 100)}% recall
                        </div>
                      ))}
                      <div className="mt-1 text-slate-600 dark:text-slate-300">{train.report.recommendation}</div>
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

          {/* Review grid */}
          <Card className="p-3">
            <div className="flex items-center gap-3 mb-3">
              <span className="text-sm font-medium">Review frames</span>
              <div className="flex gap-1">
                {LABELS.map(l => (
                  <button key={l}
                          onClick={() => setReviewLabel(l)}
                          className={'px-2.5 py-1 rounded text-xs font-medium ' +
                            (reviewLabel === l
                              ? 'text-white ' + LABEL_STYLE[l].split(' ')[0]
                              : 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300')}>
                    {l.toUpperCase()}
                  </button>
                ))}
              </div>
              <span className="text-xs text-slate-400 ml-auto">Click a frame to delete it</span>
            </div>
            {samples.length === 0 ? (
              <div className="text-sm text-slate-500 dark:text-slate-300 py-6 text-center">
                No {reviewLabel.toUpperCase()} frames captured yet.
              </div>
            ) : (
              <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 gap-2">
                {samples.map(s => (
                  <button key={s.id} onClick={() => del(s.id)}
                          title={`Captured ${s.captured_at ?? ''} — click to delete`}
                          className="group relative rounded overflow-hidden border hover:border-red-400">
                    <img src={s.file_url} className="block w-full h-20 object-cover" alt="" />
                    <span className="absolute inset-0 bg-red-600/0 group-hover:bg-red-600/40
                                     flex items-center justify-center text-white text-xs opacity-0
                                     group-hover:opacity-100">Delete</span>
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
