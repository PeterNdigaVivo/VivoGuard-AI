// Annotation canvas:
//  - upload images
//  - capture frames from a camera
//  - click & drag to draw bboxes, pick a class label, save
//  - auto-suggest pre-labels via the base model
//  - keyboard shortcuts: N=next, P=prev, D=delete-selected, S=save
//  - launch a training job once enough images are labeled

import { useEffect, useMemo, useRef, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { Badge, Button, Card, Input, PageHeader, Select } from '@/components/ui/Primitives'
import { training, type AnnotationIn, type Dataset, type TrainingImage } from '@/api/training'
import { cameras as camsApi, type Camera } from '@/api/cameras'

interface Box { class_label: string; cx: number; cy: number; w: number; h: number; auto?: boolean }

export default function AnnotationPage() {
  const { dsId } = useParams()
  const datasetId = Number(dsId)
  const nav = useNavigate()

  const [dataset, setDataset] = useState<Dataset | null>(null)
  const [images, setImages] = useState<TrainingImage[]>([])
  const [idx, setIdx] = useState(0)
  const [cameras, setCameras] = useState<Camera[]>([])

  // Drawing state.
  const [boxes, setBoxes] = useState<Box[]>([])
  const [drawing, setDrawing] = useState<{ x: number; y: number } | null>(null)
  const [tempEnd, setTempEnd] = useState<{ x: number; y: number } | null>(null)
  const [activeClass, setActiveClass] = useState<string>('')
  const [selectedBox, setSelectedBox] = useState<number | null>(null)
  const [reviewRationale, setReviewRationale] = useState('')
  const [actionMessage, setActionMessage] = useState('')

  const imgRef = useRef<HTMLImageElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  // Load dataset + image list + camera list on mount.
  useEffect(() => {
    Promise.all([
      training.getDataset(datasetId).then(setDataset),
      training.listImages(datasetId).then(setImages),
      camsApi.list().then(setCameras),
    ]).catch(console.error)
  }, [datasetId])

  // Load persisted labels (including quarantined model suggestions) whenever
  // the active image changes. Previously the canvas appeared empty after each
  // navigation, which made governed review impossible.
  useEffect(() => {
    const image = images[idx]
    setSelectedBox(null)
    setReviewRationale('')
    setActionMessage('')
    if (!image) { setBoxes([]); return }
    training.listAnnotations(image.id).then(annotations => {
      setBoxes(annotations.map(annotation => ({
        class_label: annotation.class_label,
        cx: annotation.bbox_json[0], cy: annotation.bbox_json[1],
        w: annotation.bbox_json[2], h: annotation.bbox_json[3],
        auto: !!annotation.auto_suggested && !annotation.verified,
      })))
    }).catch(() => setBoxes([]))
  }, [idx, images[idx]?.id])

  // Keyboard shortcuts.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement)?.tagName === 'INPUT') return
      if (e.key === 'n' || e.key === 'N') setIdx(i => Math.min(images.length - 1, i + 1))
      if (e.key === 'p' || e.key === 'P') setIdx(i => Math.max(0, i - 1))
      if (e.key === 'd' || e.key === 'D') {
        if (selectedBox !== null) setBoxes(bs => bs.filter((_, i) => i !== selectedBox))
      }
      if (e.key === 's' || e.key === 'S') save()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [images.length, selectedBox, boxes])

  const currentImage = images[idx]
  const classes = dataset?.classes_json ?? []
  const trainableCount = useMemo(() => images.filter(i =>
    i.labeled && i.eligible_for_training && i.review_state === 'approved').length, [images])
  const isLiveSimulationEvidence = !!currentImage &&
    currentImage.evidence_source === 'simulation_live_probe' &&
    currentImage.synthetic === false

  // ------- mouse handlers (canvas-relative) -------
  function pos(e: React.MouseEvent): { x: number; y: number } {
    const r = imgRef.current?.getBoundingClientRect()
    if (!r) return { x: 0, y: 0 }
    return { x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height }
  }
  function onMouseDown(e: React.MouseEvent) {
    if (!activeClass) return
    setDrawing(pos(e)); setTempEnd(pos(e))
  }
  function onMouseMove(e: React.MouseEvent) { if (drawing) setTempEnd(pos(e)) }
  function onMouseUp() {
    if (!drawing || !tempEnd) return
    const x1 = Math.min(drawing.x, tempEnd.x), x2 = Math.max(drawing.x, tempEnd.x)
    const y1 = Math.min(drawing.y, tempEnd.y), y2 = Math.max(drawing.y, tempEnd.y)
    const w = x2 - x1, h = y2 - y1
    if (w > 0.005 && h > 0.005 && activeClass) {
      setBoxes(bs => [...bs, { class_label: activeClass, cx: x1 + w / 2, cy: y1 + h / 2, w, h }])
    }
    setDrawing(null); setTempEnd(null)
  }

  // ------- API actions -------
  async function save() {
    if (!currentImage) return
    const payload: AnnotationIn[] = boxes.map(b => ({
      class_label: b.class_label,
      bbox_json: [b.cx, b.cy, b.w, b.h],
      verified: !b.auto,
      auto_suggested: !!b.auto,
    }))
    await training.saveAnnotations(currentImage.id, payload)
    // refresh image list to reflect new `labeled` flag
    const refreshed = await training.listImages(datasetId)
    setImages(refreshed)
    setActionMessage(isLiveSimulationEvidence
      ? 'Primary review saved. A different operator must independently approve it.'
      : 'Annotations saved.')
  }

  async function reviewSimulation(verdict: 'approve' | 'reject') {
    if (!currentImage || reviewRationale.trim().length < 8) {
      setActionMessage('Add a clear rationale of at least 8 characters.')
      return
    }
    await training.reviewSimulationEvidence(currentImage.id, verdict, reviewRationale.trim())
    setImages(await training.listImages(datasetId))
    setActionMessage(verdict === 'approve'
      ? 'Independent review approved. This real frame is now training-eligible.'
      : 'Evidence rejected and remains excluded from training.')
  }

  async function autoSuggest() {
    if (!currentImage) return
    const sugs = await training.autoSuggest(currentImage.id)
    setBoxes(sugs.map(s => ({
      class_label: s.class_label,
      cx: s.bbox_json[0], cy: s.bbox_json[1], w: s.bbox_json[2], h: s.bbox_json[3],
      auto: true,
    })))
  }

  async function uploadFiles(fl: FileList | null) {
    if (!fl || fl.length === 0) return
    await training.uploadImages(datasetId, fl)
    setImages(await training.listImages(datasetId))
  }

  async function captureFrom(camId: number) {
    if (!camId) return
    await training.captureFromCamera({ camera_id: camId, dataset_id: datasetId, count: 1 })
    setImages(await training.listImages(datasetId))
  }

  async function startTraining() {
    const job = await training.startJob({
      model_name: `${dataset?.name ?? 'model'}-${Date.now()}`,
      dataset_id: datasetId,
      base_model: 'yolov8n.pt',
      epochs: 50, batch: 16, imgsz: 640, augment: true,
      split_train: 0.7, split_val: 0.2, split_test: 0.1,
    })
    nav(`/training/jobs/${job.id}`)
  }

  return (
    <div className="p-6">
      <PageHeader
        title={`Annotate — ${dataset?.name ?? '…'}`}
        actions={
          <>
            <Button variant="ghost" onClick={() => nav('/training')}>Back</Button>
            <Button onClick={startTraining} disabled={trainableCount < 5}>
              Start training ({trainableCount} approved)
            </Button>
          </>
        }
      />

      {/* Toolbar: capture / upload / classes */}
      <Card className="p-3 mb-4 flex flex-wrap items-center gap-2">
        <label className="text-sm">
          <span className="mr-2">Class:</span>
          <Select value={activeClass} onChange={e => setActiveClass(e.target.value)}>
            <option value="">(pick a class)</option>
            {classes.map(c => <option key={c} value={c}>{c}</option>)}
          </Select>
        </label>
        <Button variant="ghost" onClick={autoSuggest}>Auto-suggest</Button>
        <Button onClick={save}>Save (S)</Button>

        <div className="ml-auto flex items-center gap-2">
          <label className="text-sm">
            Capture from:
            <Select className="ml-1" defaultValue="" onChange={e => captureFrom(Number(e.target.value))}>
              <option value="">— camera —</option>
              {cameras.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
            </Select>
          </label>
          <label className="text-sm bg-slate-200 hover:bg-slate-300 px-3 py-1.5 rounded cursor-pointer">
            Upload images
            <input type="file" multiple accept="image/*" hidden
                   onChange={e => uploadFiles(e.target.files)} />
          </label>
        </div>
      </Card>

      <div className="grid grid-cols-12 gap-4">
        {/* Canvas */}
        <div className="col-span-9">
          <Card className="p-3">
            <div className="text-xs text-slate-500 dark:text-slate-300 mb-2">
              Image {images.length ? idx + 1 : 0} / {images.length} —
              shortcuts: N next · P prev · D delete · S save
            </div>
            {currentImage && (
              <div className="mb-3 flex flex-wrap items-center gap-2 text-xs">
                <Badge color={isLiveSimulationEvidence ? 'sky' : 'slate'}>
                  {isLiveSimulationEvidence ? 'REAL CAMERA SIMULATION EVIDENCE' : currentImage.source_kind}
                </Badge>
                <Badge color={currentImage.eligible_for_training ? 'green' : 'amber'}>
                  {currentImage.review_state.replaceAll('_', ' ')}
                </Badge>
                {!currentImage.eligible_for_training &&
                  <span className="text-amber-700">Excluded from training until review is complete.</span>}
              </div>
            )}
            <div ref={containerRef} className="relative bg-slate-200 dark:bg-slate-800 select-none"
                 style={{ width: '100%', maxWidth: 960 }}
                 onMouseDown={onMouseDown} onMouseMove={onMouseMove} onMouseUp={onMouseUp}>
              {currentImage ? (
                <img ref={imgRef} src={training.imageFileUrl(datasetId, currentImage.id)}
                     className="block w-full h-auto" alt="" />
              ) : (
                <div className="aspect-video flex items-center justify-center text-slate-500 dark:text-slate-300">
                  No images. Upload or capture above.
                </div>
              )}

              {/* existing boxes */}
              {boxes.map((b, i) => {
                const left = (b.cx - b.w / 2) * 100, top = (b.cy - b.h / 2) * 100
                const w = b.w * 100, h = b.h * 100
                return (
                  <div key={i}
                       onClick={e => { e.stopPropagation(); setSelectedBox(i) }}
                       className={'absolute border-2 cursor-pointer ' +
                                  (selectedBox === i ? 'border-amber-400' : (b.auto ? 'border-sky-400' : 'border-emerald-400'))}
                       style={{ left: `${left}%`, top: `${top}%`, width: `${w}%`, height: `${h}%` }}>
                    <span className="absolute -top-5 left-0 text-xs bg-black/70 text-white px-1 rounded">
                      {b.class_label}{b.auto ? ' (auto)' : ''}
                    </span>
                  </div>
                )
              })}

              {/* in-flight box while dragging */}
              {drawing && tempEnd && (
                <div className="absolute border-2 border-amber-400 pointer-events-none"
                     style={{
                       left: `${Math.min(drawing.x, tempEnd.x) * 100}%`,
                       top:  `${Math.min(drawing.y, tempEnd.y) * 100}%`,
                       width:  `${Math.abs(tempEnd.x - drawing.x) * 100}%`,
                       height: `${Math.abs(tempEnd.y - drawing.y) * 100}%`,
                     }} />
              )}
            </div>

            <div className="flex justify-between mt-3">
              <Button variant="ghost" onClick={() => setIdx(i => Math.max(0, i - 1))}
                      disabled={idx === 0}>Prev (P)</Button>
              <Button variant="ghost" onClick={() => setIdx(i => Math.min(images.length - 1, i + 1))}
                      disabled={idx >= images.length - 1}>Next (N)</Button>
            </div>
            {isLiveSimulationEvidence && currentImage.review_state === 'pending_independent_review' && (
              <div className="mt-4 rounded border border-sky-200 bg-sky-50 p-3">
                <div className="font-medium text-sm text-sky-900">Independent review</div>
                <div className="text-xs text-sky-800 mt-1">
                  A different operator must compare the real frame with the boxes. Approval never applies to synthetic scenarios.
                </div>
                <Input className="mt-2" value={reviewRationale}
                       placeholder="Evidence-based rationale"
                       onChange={e => setReviewRationale(e.target.value)} />
                <div className="flex gap-2 mt-2">
                  <Button onClick={() => reviewSimulation('approve')}>Approve evidence</Button>
                  <Button variant="danger" onClick={() => reviewSimulation('reject')}>Reject evidence</Button>
                </div>
              </div>
            )}
            {actionMessage && <div className="mt-3 text-sm text-slate-700">{actionMessage}</div>}
          </Card>
        </div>

        {/* Image strip + box list */}
        <div className="col-span-3 space-y-3">
          <Card className="p-3">
            <div className="text-sm font-medium mb-2">Boxes on this image</div>
            {boxes.length === 0 && <div className="text-slate-500 dark:text-slate-300 text-sm">No boxes yet.</div>}
            {boxes.map((b, i) => (
              <div key={i}
                   onClick={() => setSelectedBox(i)}
                   className={'flex items-center justify-between text-sm py-1 px-2 rounded cursor-pointer ' +
                              (selectedBox === i ? 'bg-amber-50' : '')}>
                <span><Badge color={b.auto ? 'sky' : 'green'}>{b.class_label}</Badge></span>
                <button className="text-red-600 hover:underline"
                        onClick={(e) => { e.stopPropagation(); setBoxes(bs => bs.filter((_, j) => j !== i)) }}>×</button>
              </div>
            ))}
          </Card>

          <Card className="p-3 max-h-[320px] overflow-auto">
            <div className="text-sm font-medium mb-2">Image gallery</div>
            <div className="grid grid-cols-3 gap-1">
              {images.map((im, i) => (
                <img key={im.id}
                     onClick={() => setIdx(i)}
                     className={'w-full aspect-square object-cover cursor-pointer rounded ' +
                                (i === idx ? 'ring-2 ring-sky-500' : '')}
                     src={training.imageFileUrl(datasetId, im.id)}
                     alt="" />
              ))}
            </div>
          </Card>
        </div>
      </div>
    </div>
  )
}
