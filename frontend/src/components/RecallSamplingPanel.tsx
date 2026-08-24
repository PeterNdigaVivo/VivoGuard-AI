import { useEffect, useMemo, useState } from 'react'
import { operations, type AssuranceCase } from '@/api/operations'
import { Button, Card, Input, useToast } from '@/components/ui/Primitives'

const EVENT_LABELS = [
  'intrusion', 'trespass', 'staff_zone', 'stockroom_access', 'loitering',
  'crowd', 'fall', 'fight', 'weapon', 'fire', 'smoke', 'shop_open_close',
]

function useAuthenticatedClip(caseId: number | null) {
  const [src, setSrc] = useState<string | null>(null)
  const [state, setState] = useState<'idle' | 'loading' | 'failed'>('idle')
  useEffect(() => {
    if (!caseId) { setSrc(null); return }
    let cancelled = false
    let objectUrl: string | null = null
    setState('loading')
    const token = localStorage.getItem('vg_access_token') ?? ''
    fetch(`/api/operations/recall-samples/${caseId}/clip`, {
      headers: { Authorization: `Bearer ${token}` },
    }).then(response => response.ok ? response.blob() : null)
      .then(blob => {
        if (cancelled) return
        if (!blob) { setState('failed'); return }
        objectUrl = URL.createObjectURL(blob)
        setSrc(objectUrl); setState('idle')
      }).catch(() => { if (!cancelled) setState('failed') })
    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
      setSrc(null)
    }
  }, [caseId])
  return { src, state }
}

export default function RecallSamplingPanel({ onClose }: { onClose: () => void }) {
  const [cases, setCases] = useState<AssuranceCase[] | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [seed, setSeed] = useState(() => `validation-${new Date().toISOString().slice(0, 10)}`)
  const [label, setLabel] = useState('intrusion')
  const [rationale, setRationale] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { push } = useToast()

  async function load() {
    try {
      const rows = await operations.listRecallSamples()
      const reviewable = rows.filter(row => !['resolved', 'insufficient_evidence'].includes(row.status))
      setCases(reviewable)
      setSelectedId(current => reviewable.some(row => row.id === current)
        ? current : (reviewable.find(row => row.evidence?.extraction_status === 'ready')?.id ?? null))
    } catch (err) { setError(String(err)) }
  }
  useEffect(() => { void load() }, [])
  const selected = useMemo(() => cases?.find(row => row.id === selectedId) ?? null,
    [cases, selectedId])
  const clip = useAuthenticatedClip(
    selected?.evidence?.extraction_status === 'ready' ? selected.id : null,
  )

  async function generate() {
    setBusy(true); setError(null)
    try {
      const result = await operations.generateRecallSamples({
        sample_count: 10, duration_seconds: 30, seed,
      })
      push(`${result.created} blind recall samples queued${result.reused ? `; ${result.reused} reproducible samples reused` : ''}.`)
      await load()
    } catch (err) { setError(String(err)) }
    finally { setBusy(false) }
  }

  async function review(outcome: 'target_event' | 'no_target_event' | 'unclear') {
    if (!selected || rationale.trim().length < 8) return
    setBusy(true); setError(null)
    try {
      const result = await operations.reviewRecallSample(
        selected.id, outcome, outcome === 'target_event' ? label : null,
        rationale.trim(),
      )
      push(result.result === 'pending_independent_review'
        ? 'Primary review saved; a different operator must review this clip.'
        : `Recall review saved: ${result.result}.`)
      setRationale(''); await load()
    } catch (err) { setError(String(err)) }
    finally { setBusy(false) }
  }

  return (
    <Card className="p-4 mb-4 border-emerald-200">
      <div className="flex justify-between gap-3 mb-3">
        <div>
          <h2 className="font-semibold">Blind random-footage recall review</h2>
          <p className="text-sm text-slate-600 dark:text-slate-300">
            Review the clip before checking alert history. A different operator must independently agree.
          </p>
        </div>
        <Button variant="ghost" onClick={onClose}>Close</Button>
      </div>
      <div className="flex flex-wrap gap-2 mb-3">
        <Input value={seed} onChange={event => setSeed(event.target.value)}
          aria-label="Reproducible sampling seed" className="min-w-64" />
        <Button onClick={generate} disabled={busy || seed.trim().length < 4}>
          Generate 10 × 30-second samples
        </Button>
      </div>
      {error && <div className="text-sm text-red-700 mb-3">{error}</div>}
      {cases === null && <div className="text-sm text-slate-500">Loading samples…</div>}
      {cases?.length === 0 && <div className="text-sm text-slate-500">No samples await review.</div>}
      {cases && cases.length > 0 && (
        <div className="grid md:grid-cols-[17rem_1fr] gap-4">
          <div className="space-y-2 max-h-96 overflow-y-auto">
            {cases.map(row => (
              <button key={row.id} onClick={() => setSelectedId(row.id)}
                className={'w-full text-left rounded border p-2 text-sm ' +
                  (selectedId === row.id ? 'border-emerald-500 bg-emerald-50 dark:bg-slate-800'
                    : 'border-slate-200 dark:border-slate-700')}>
                <div className="font-medium">Sample #{row.id} · Camera #{row.camera_id ?? '—'}</div>
                <div className="text-slate-500">{row.status.replaceAll('_', ' ')}</div>
              </button>
            ))}
          </div>
          {selected && (
            <div>
              {clip.state === 'loading' && <div className="aspect-video bg-black text-white grid place-items-center">Loading clip…</div>}
              {clip.state === 'failed' && <div className="aspect-video bg-black text-white grid place-items-center">Clip is not ready or unavailable.</div>}
              {clip.src && <video key={clip.src} controls preload="metadata" src={clip.src}
                className="w-full aspect-video bg-black rounded" />}
              <div className="mt-3 flex gap-2 items-center">
                <label className="text-sm">Target event</label>
                <select value={label} onChange={event => setLabel(event.target.value)}
                  className="rounded border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 p-1 text-sm">
                  {EVENT_LABELS.map(value => <option key={value}>{value}</option>)}
                </select>
              </div>
              <textarea value={rationale} onChange={event => setRationale(event.target.value)}
                placeholder="Describe what the clip shows without names, biometrics or accusations."
                className="mt-2 w-full min-h-20 rounded border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 p-2 text-sm" />
              <div className="flex flex-wrap gap-2 mt-2">
                <Button disabled={busy || !clip.src || rationale.trim().length < 8}
                  onClick={() => review('target_event')}>Target event present</Button>
                <Button variant="ghost" disabled={busy || !clip.src || rationale.trim().length < 8}
                  onClick={() => review('no_target_event')}>No target event</Button>
                <Button variant="ghost" disabled={busy || !clip.src || rationale.trim().length < 8}
                  onClick={() => review('unclear')}>Unclear</Button>
              </div>
            </div>
          )}
        </div>
      )}
    </Card>
  )
}
