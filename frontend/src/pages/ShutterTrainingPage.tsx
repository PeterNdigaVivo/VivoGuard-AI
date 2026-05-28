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
  counts: { open: number; closed: number; partial: number }
}
interface Sample {
  id: number
  label: Label
  captured_at: string | null
  file_url: string
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

  const current = cameras.find(c => c.camera_id === selected) || null

  function loadCameras() {
    api<ShutterCamera[]>('/training/shutter/cameras')
      .then(rows => {
        setCameras(rows)
        if (rows.length && selected === null) setSelected(rows[0].camera_id)
      })
      .catch(e => setMsg(String(e)))
  }
  useEffect(loadCameras, [])  // eslint-disable-line react-hooks/exhaustive-deps

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

  const counts = current?.counts ?? { open: 0, closed: 0, partial: 0 }
  const ready = counts.open >= MIN_PER_CLASS &&
                counts.closed >= MIN_PER_CLASS &&
                counts.partial >= MIN_PER_CLASS

  return (
    <div className="p-6 space-y-4">
      <PageHeader
        title="Shutter training data"
        actions={<Link to="/training"><Button variant="ghost">← Training Studio</Button></Link>}
      />

      {cameras.length === 0 ? (
        <Card className="p-6 text-sm text-slate-600">
          No cameras have a <code>shutter</code> zone yet. Draw a zone tagged
          “shutter” on a camera that watches the store entrance, then come back
          here to start collecting OPEN / CLOSED / PARTIAL frames.
        </Card>
      ) : (
        <>
          {/* Camera picker */}
          <Card className="p-3">
            <div className="flex flex-wrap items-center gap-3">
              <span className="text-xs uppercase tracking-wide text-slate-500">Camera</span>
              <select className="border rounded px-2 py-1 text-sm"
                      value={selected ?? ''}
                      onChange={e => setSelected(Number(e.target.value))}>
                {cameras.map(c => (
                  <option key={c.camera_id} value={c.camera_id}>
                    {c.store_name ? `${c.store_name} — ` : ''}{c.camera_name}
                  </option>
                ))}
              </select>
              <label className="ml-auto flex items-center gap-1.5 text-xs text-slate-600">
                <input type="checkbox" checked={autoCapture}
                       onChange={e => setAutoCapture(e.target.checked)} />
                Auto-capture every 5 min
              </label>
            </div>
          </Card>

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
              {msg && <div className="text-xs text-slate-500 mt-2">{msg}</div>}
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
                {ready
                  ? '✅ Enough frames to train — head to the training pipeline.'
                  : `Collect at least ${MIN_PER_CLASS} frames per class to enable training.`}
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
                              : 'bg-slate-100 text-slate-600')}>
                    {l.toUpperCase()}
                  </button>
                ))}
              </div>
              <span className="text-xs text-slate-400 ml-auto">Click a frame to delete it</span>
            </div>
            {samples.length === 0 ? (
              <div className="text-sm text-slate-500 py-6 text-center">
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
