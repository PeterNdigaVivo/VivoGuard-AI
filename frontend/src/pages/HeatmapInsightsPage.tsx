// Heatmap Insights — the floor-plan view, opened in its own tab from
// the Store Heatmaps page ("View more heatmap insights").
//
// v1 is composition, not projection: an operator uploads the store's
// floor-plan image once, then places each camera's coverage rectangle
// on it (select camera, drag on the plan). The page overlays each
// placed camera's existing heatmap PNG (same durable day/week grids
// the Store Heatmaps page shows) inside its rectangle. Approximate by
// design — good for "which part of the floor is hot", not for
// centimetre accuracy.
import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { Badge, Button, Card, PageHeader, Skeleton } from '@/components/ui/Primitives'
import { api } from '@/api/client'

type Layer = 'traffic' | 'engagement' | 'congestion'
type Win = '3h' | 'day' | 'week'

interface Placement { x: number; y: number; w: number; h: number; rot: number }
interface Cam {
  id: number
  name: string
  status: string
  floor_placement_json: Placement | null
}

async function fetchAuthedBlob(url: string): Promise<string | null> {
  const tok = localStorage.getItem('vg_access_token') ?? ''
  const res = await fetch(url, { headers: { Authorization: `Bearer ${tok}` } })
  if (!res.ok) return null
  return URL.createObjectURL(await res.blob())
}

export default function HeatmapInsightsPage() {
  const { id } = useParams()
  const storeId = Number(id)

  const [storeName, setStoreName] = useState('')
  const [cams, setCams] = useState<Cam[] | null>(null)
  const [planUrl, setPlanUrl] = useState<string | null>(null)
  const [planChecked, setPlanChecked] = useState(false)
  const [heatUrls, setHeatUrls] = useState<Record<number, string>>({})
  const [layer, setLayer] = useState<Layer>('traffic')
  const [win, setWin] = useState<Win>('day')
  const [opacity, setOpacity] = useState(0.75)
  const [editMode, setEditMode] = useState(false)
  const [selectedCam, setSelectedCam] = useState<number | null>(null)
  const [drag, setDrag] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const planRef = useRef<HTMLDivElement>(null)

  const loadCams = useCallback(() => {
    api<Cam[]>(`/stores/${storeId}/cameras`).then(setCams).catch(e => setError(String(e)))
  }, [storeId])

  useEffect(() => {
    api<{ name: string }>(`/stores/${storeId}`)
      .then(s => setStoreName(s.name)).catch(() => {})
    loadCams()
  }, [storeId, loadCams])

  // Floor plan (authed blob; 404 = none uploaded yet).
  useEffect(() => {
    let dead = false
    fetchAuthedBlob(`/api/stores/${storeId}/floor-plan`).then(u => {
      if (dead) { if (u) URL.revokeObjectURL(u); return }
      setPlanUrl(u); setPlanChecked(true)
    })
    return () => { dead = true }
  }, [storeId])

  // Heatmap PNGs for every placed camera, refetched on layer/window change.
  useEffect(() => {
    if (!cams) return
    let dead = false
    const created: string[] = []
    const placed = cams.filter(c => c.floor_placement_json)
    Promise.all(placed.map(async c => {
      const u = await fetchAuthedBlob(
        `/api/analytics/heatmap/${c.id}/image?layer=${layer}&window=${win}&alpha=1.0`)
      if (u) created.push(u)
      return [c.id, u] as const
    })).then(pairs => {
      if (dead) { created.forEach(URL.revokeObjectURL); return }
      const next: Record<number, string> = {}
      for (const [cid, u] of pairs) if (u) next[cid] = u
      setHeatUrls(next)
    })
    return () => { dead = true; created.forEach(URL.revokeObjectURL) }
  }, [cams, layer, win])

  async function generateAIPlan() {
    setSaving(true); setError(null)
    try {
      await api(`/stores/${storeId}/floor-plan/generate`, { method: 'POST' })
      const u = await fetchAuthedBlob(`/api/stores/${storeId}/floor-plan`)
      setPlanUrl(u)
      loadCams()
    } catch (e) { setError(String(e)) } finally { setSaving(false) }
  }

  async function uploadPlan(file: File) {
    setSaving(true); setError(null)
    try {
      const form = new FormData()
      form.append('file', file)
      await api(`/stores/${storeId}/floor-plan`, { method: 'POST', form })
      const u = await fetchAuthedBlob(`/api/stores/${storeId}/floor-plan`)
      setPlanUrl(u)
    } catch (e) { setError(String(e)) } finally { setSaving(false) }
  }

  function planCoords(e: React.PointerEvent): { x: number; y: number } | null {
    const el = planRef.current
    if (!el) return null
    const r = el.getBoundingClientRect()
    return {
      x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
      y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)),
    }
  }

  async function savePlacement(camId: number, p: Placement) {
    setSaving(true); setError(null)
    try {
      await api(`/cameras/${camId}/floor-placement`, { method: 'PUT', body: p })
      loadCams()
    } catch (e) { setError(String(e)) } finally { setSaving(false) }
  }

  async function removePlacement(camId: number) {
    setSaving(true); setError(null)
    try {
      await api(`/cameras/${camId}/floor-placement`, { method: 'DELETE' })
      loadCams()
    } catch (e) { setError(String(e)) } finally { setSaving(false) }
  }

  async function rotatePlacement(c: Cam) {
    const p = c.floor_placement_json
    if (!p) return
    await savePlacement(c.id, { ...p, rot: ((p.rot ?? 0) + 90) % 360 })
  }

  function onPointerDown(e: React.PointerEvent) {
    if (!editMode || selectedCam == null) return
    const c = planCoords(e)
    if (!c) return
    ;(e.target as Element).setPointerCapture?.(e.pointerId)
    setDrag({ x0: c.x, y0: c.y, x1: c.x, y1: c.y })
  }
  function onPointerMove(e: React.PointerEvent) {
    if (!drag) return
    const c = planCoords(e)
    if (c) setDrag({ ...drag, x1: c.x, y1: c.y })
  }
  function onPointerUp() {
    if (!drag || selectedCam == null) { setDrag(null); return }
    const x = Math.min(drag.x0, drag.x1)
    const y = Math.min(drag.y0, drag.y1)
    const w = Math.abs(drag.x1 - drag.x0)
    const h = Math.abs(drag.y1 - drag.y0)
    setDrag(null)
    if (w < 0.02 || h < 0.02) return   // accidental click, not a rectangle
    savePlacement(selectedCam, { x, y, w, h, rot: 0 })
    setSelectedCam(null)
  }

  if (!cams || !planChecked) {
    return (
      <div className="p-6">
        <PageHeader title="Heatmap insights" />
        <Skeleton className="h-96" />
      </div>
    )
  }

  const placed = cams.filter(c => c.floor_placement_json)
  const unplaced = cams.filter(c => !c.floor_placement_json)

  return (
    <div className="p-6">
      <PageHeader
        title={`Heatmap insights${storeName ? ` — ${storeName}` : ''}`}
        actions={
          <div className="flex items-center gap-2 flex-wrap">
            {(['traffic', 'engagement', 'congestion'] as Layer[]).map(l => (
              <Button key={l} variant={layer === l ? 'primary' : 'ghost'}
                      onClick={() => setLayer(l)}>{l}</Button>
            ))}
            <span className="w-2" />
            {(['3h', 'day', 'week'] as Win[]).map(w => (
              <Button key={w} variant={win === w ? 'primary' : 'ghost'}
                      onClick={() => setWin(w)}>{w}</Button>
            ))}
          </div>
        }
      />

      {error && (
        <Card className="p-3 mb-4 text-sm text-red-600 dark:text-red-400">{error}</Card>
      )}

      {!planUrl ? (
        <Card className="p-8 text-center">
          <p className="text-slate-600 dark:text-slate-300 mb-4">
            No floor plan uploaded for this store yet. Upload a top-down
            image of the floor (a photo of the architect's plan or a clean
            sketch both work) to unlock the floor heat view.
          </p>
          <div className="flex items-center justify-center gap-3 flex-wrap">
            <label className="inline-block">
              <span className="px-4 py-2 rounded bg-blue-600 text-white cursor-pointer">
                {saving ? 'Working…' : 'Upload floor plan'}
              </span>
              <input type="file" accept="image/png,image/jpeg,image/webp"
                     className="hidden" disabled={saving}
                     onChange={e => { const f = e.target.files?.[0]; if (f) uploadPlan(f) }} />
            </label>
            <span className="text-slate-400 text-sm">or</span>
            <Button onClick={generateAIPlan} disabled={saving}>
              {saving ? 'Drafting…' : '✨ Generate draft plan with AI'}
            </Button>
          </div>
          <p className="text-xs text-slate-500 dark:text-slate-400 mt-3">
            The AI draft looks at each camera&apos;s view, names and zones to
            sketch a schematic layout and pre-place every camera — adjust
            afterwards, and replace with a real drawing anytime without
            losing placements.
          </p>
        </Card>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-4 gap-4">
          <Card className="lg:col-span-3 p-3">
            <div
              ref={planRef}
              className="relative w-full select-none overflow-hidden rounded"
              style={{ touchAction: editMode ? 'none' : undefined,
                       cursor: editMode && selectedCam != null ? 'crosshair' : 'default' }}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={onPointerUp}
            >
              <img src={planUrl} alt="Floor plan" className="w-full block" draggable={false} />
              {placed.map(c => {
                const p = c.floor_placement_json as Placement
                const heat = heatUrls[c.id]
                return (
                  <div key={c.id}
                       className="absolute"
                       style={{ left: `${p.x * 100}%`, top: `${p.y * 100}%`,
                                width: `${p.w * 100}%`, height: `${p.h * 100}%` }}>
                    {heat ? (
                      <img src={heat} alt={`${c.name} heat`} draggable={false}
                           className="w-full h-full"
                           style={{ opacity,
                                    transform: `rotate(${p.rot ?? 0}deg)`,
                                    mixBlendMode: 'multiply' }} />
                    ) : (
                      <div className="w-full h-full border border-dashed border-slate-400/70" />
                    )}
                    {editMode && (
                      <div className="absolute -top-6 left-0 flex items-center gap-1 text-xs
                                      bg-slate-800/80 text-white rounded px-1.5 py-0.5 whitespace-nowrap">
                        {c.name}
                        <button className="px-1 hover:text-amber-300" title="Rotate 90°"
                                onClick={ev => { ev.stopPropagation(); rotatePlacement(c) }}>⟳</button>
                        <button className="px-1 hover:text-red-300" title="Remove from plan"
                                onClick={ev => { ev.stopPropagation(); removePlacement(c.id) }}>✕</button>
                      </div>
                    )}
                  </div>
                )
              })}
              {drag && (
                <div className="absolute border-2 border-blue-500 bg-blue-500/20 pointer-events-none"
                     style={{ left: `${Math.min(drag.x0, drag.x1) * 100}%`,
                              top: `${Math.min(drag.y0, drag.y1) * 100}%`,
                              width: `${Math.abs(drag.x1 - drag.x0) * 100}%`,
                              height: `${Math.abs(drag.y1 - drag.y0) * 100}%` }} />
              )}
            </div>
            <div className="flex items-center gap-4 mt-3 flex-wrap text-sm">
              <label className="flex items-center gap-2 text-slate-600 dark:text-slate-300">
                Heat opacity
                <input type="range" min={0.2} max={1} step={0.05} value={opacity}
                       onChange={e => setOpacity(Number(e.target.value))} />
              </label>
              <Button variant={editMode ? 'primary' : 'ghost'}
                      onClick={() => { setEditMode(m => !m); setSelectedCam(null) }}>
                {editMode ? 'Done placing' : 'Place cameras'}
              </Button>
              {editMode && (
                <span className="text-slate-500 dark:text-slate-400">
                  {selectedCam != null
                    ? 'Drag on the plan to draw the camera’s coverage area.'
                    : 'Pick a camera on the right, then drag on the plan.'}
                </span>
              )}
              <label className="ml-auto">
                <span className="text-blue-600 dark:text-blue-400 cursor-pointer hover:underline">
                  {saving ? 'Working…' : 'Replace floor plan'}
                </span>
                <input type="file" accept="image/png,image/jpeg,image/webp"
                       className="hidden" disabled={saving}
                       onChange={e => { const f = e.target.files?.[0]; if (f) uploadPlan(f) }} />
              </label>
            </div>
          </Card>

          <Card className="p-3">
            <h2 className="font-medium text-slate-800 dark:text-slate-100 mb-2">Cameras</h2>
            <ul className="space-y-1 text-sm">
              {cams.map(c => (
                <li key={c.id}>
                  <button
                    className={`w-full text-left px-2 py-1.5 rounded flex items-center gap-2
                      ${selectedCam === c.id ? 'bg-blue-100 dark:bg-blue-900/40' :
                        'hover:bg-slate-100 dark:hover:bg-slate-800'}`}
                    onClick={() => {
                      if (!editMode) setEditMode(true)
                      setSelectedCam(c.id)
                    }}>
                    <span className="flex-1 truncate text-slate-700 dark:text-slate-200">{c.name}</span>
                    {c.floor_placement_json
                      ? <Badge color="green">placed</Badge>
                      : <Badge color="slate">not placed</Badge>}
                  </button>
                </li>
              ))}
            </ul>
            {unplaced.length > 0 && (
              <p className="text-xs text-slate-500 dark:text-slate-400 mt-3">
                {unplaced.length} camera{unplaced.length === 1 ? '' : 's'} not on the
                plan yet — their heat shows only on the Store Heatmaps page.
              </p>
            )}
          </Card>
        </div>
      )}
    </div>
  )
}
