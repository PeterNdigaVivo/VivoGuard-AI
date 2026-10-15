// Model evaluation — system admins only.
//
// Shows how big the evaluation test set is and lets admins label it:
//   • People: click once on every person visible in a busy-hour frame.
//     Missed people and false detections are scored from these clicks.
//   • Clips:  watch a short recording and count the different people in
//     it (used to measure tracker ID switches).
// Reviewed alerts need no labelling here — the operators' True/False
// verdicts are their labels. Scores come from `python -m app.eval.run`
// (docs/MODEL_EVALUATION.md); this page never runs a model.

import { useCallback, useEffect, useState, type MouseEvent } from 'react'
import { Card, PageHeader } from '@/components/ui/Primitives'
import { evaluation, type EvalSampleRef, type EvalSummary, type LabelKind } from '@/api/evaluation'
import { clickToPoint, progressText } from '@/lib/evalLabel'

const CATEGORY_LABEL: Record<string, string> = {
  counter_unattended: 'Counter Left Unattended',
  person: 'Person / intrusion',
  uniform: 'Uniform',
}

export default function EvaluationPage() {
  const [summary, setSummary] = useState<EvalSummary | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<LabelKind>('people')

  const loadSummary = useCallback(() => {
    evaluation.summary()
      .then(s => { setSummary(s); setError(null) })
      .catch(e => setError(e instanceof Error ? e.message : String(e)))
  }, [])
  useEffect(loadSummary, [loadSummary])

  return (
    <div className="p-6 space-y-4">
      <PageHeader title="Model evaluation" />
      {error && (
        <Card className="p-3 text-sm text-red-800 bg-red-50 border-red-200">
          Could not load the test set: {error}
        </Card>
      )}
      {summary && <SummaryCard summary={summary} />}
      <div className="flex gap-2">
        {(['people', 'clip'] as LabelKind[]).map(k => (
          <button key={k} type="button" onClick={() => setTab(k)}
                  className={'px-3 py-1.5 rounded text-sm font-medium '
                    + (tab === k ? 'bg-slate-800 text-white' : 'bg-slate-100 text-slate-700 hover:bg-slate-200')}>
            {k === 'people' ? 'Label people' : 'Count people in clips'}
          </button>
        ))}
      </div>
      <Labeller key={tab} kind={tab} onSaved={loadSummary} />
    </div>
  )
}

function SummaryCard({ summary }: { summary: EvalSummary }) {
  const t = summary.targets
  return (
    <Card className="p-4 text-sm space-y-3">
      {!summary.collection_enabled && (
        <div className="rounded border border-amber-200 bg-amber-50 text-amber-900 px-3 py-2">
          Collection is switched off (EVAL_CAPTURE_RAW_FRAMES=false), so the test set is not growing.
        </div>
      )}
      <div>
        <div className="font-semibold text-slate-800 mb-1">Reviewed alerts (labelled by operators)</div>
        <table className="text-left">
          <thead className="text-xs text-slate-500">
            <tr><th className="pr-6">Alert type</th><th className="pr-6">Samples</th>
                <th className="pr-6">Marked True</th><th>Marked False</th></tr>
          </thead>
          <tbody>
            {Object.keys(CATEGORY_LABEL).map(c => {
              const row = summary.alerts[c] ?? { total: 0, true: 0, false: 0 }
              return (
                <tr key={c}>
                  <td className="pr-6">{CATEGORY_LABEL[c]}</td>
                  <td className="pr-6 tabular-nums">{row.total} / {t.per_alert_type}</td>
                  <td className="pr-6 tabular-nums">{row.true}</td>
                  <td className="tabular-nums">{row.false}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div className="text-slate-700">
        People frames: {progressText(summary.people, t.people)}<br />
        Clips: {progressText(summary.clips, t.clips)}
      </div>
      <div className="text-xs text-slate-500">
        Test images are kept on the server for {summary.retention_days} days, visible to system admins only.
      </div>
    </Card>
  )
}

function Labeller({ kind, onSaved }: { kind: LabelKind; onSaved: () => void }) {
  const [sample, setSample] = useState<EvalSampleRef | null | undefined>(undefined)
  const [url, setUrl] = useState<string | null>(null)
  const [points, setPoints] = useState<[number, number][]>([])
  const [count, setCount] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const loadNext = useCallback(async () => {
    setError(null); setPoints([]); setCount('')
    try {
      const next = await evaluation.next(kind)
      setSample(next)
      setUrl(next ? await evaluation.mediaUrl(next.id) : null)
    } catch (e) {
      console.warn('[evaluation] could not load the next sample', e)
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [kind])
  useEffect(() => { void loadNext() }, [loadNext])
  useEffect(() => () => { if (url) URL.revokeObjectURL(url) }, [url])

  function onImageClick(e: MouseEvent<HTMLImageElement>) {
    const p = clickToPoint(e.clientX, e.clientY, e.currentTarget.getBoundingClientRect())
    if (p) setPoints(ps => [...ps, p])
  }

  async function save(body: { points?: number[][]; count?: number; skip?: boolean }) {
    if (!sample || busy) return
    setBusy(true)
    try {
      await evaluation.label(sample.id, body)
      onSaved()
      await loadNext()
    } catch (e) {
      console.warn('[evaluation] could not save the label', e)
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (sample === undefined) return <Card className="p-6 text-sm text-slate-500">Loading…</Card>
  if (sample === null) {
    return (
      <Card className="p-6 text-sm text-slate-600">
        Nothing waiting to be labelled. New {kind === 'people' ? 'frames' : 'clips'} arrive
        automatically while collection is switched on.
      </Card>
    )
  }

  const countValue = Number(count)
  const countValid = count.trim() !== '' && Number.isInteger(countValue) && countValue >= 0 && countValue <= 200

  return (
    <Card className="p-4 space-y-3">
      <div className="text-sm text-slate-700">
        {kind === 'people'
          ? <>Click once on <strong>every person</strong> you can see — including people partly hidden or at the edge. Click nothing if the frame is empty.</>
          : <>Watch the clip and enter how many <strong>different people</strong> appear in it.</>}
        <span className="ml-2 text-xs text-slate-500">Camera {sample.camera_id ?? '—'}</span>
      </div>
      {error && <div className="text-sm text-red-700" role="alert">{error}</div>}

      {kind === 'people' && url && (
        <div className="relative inline-block max-w-full select-none">
          <img src={url} alt="Frame to label" onClick={onImageClick}
               className="max-w-full max-h-[70vh] cursor-crosshair rounded" draggable={false} />
          {points.map(([x, y], i) => (
            <span key={i} aria-hidden="true"
                  className="absolute w-4 h-4 -ml-2 -mt-2 rounded-full bg-emerald-500 ring-2 ring-white pointer-events-none"
                  style={{ left: `${x * 100}%`, top: `${y * 100}%` }} />
          ))}
        </div>
      )}
      {kind === 'clip' && url && (
        <video src={url} controls className="max-w-full max-h-[70vh] rounded bg-black" />
      )}

      <div className="flex flex-wrap items-center gap-2">
        {kind === 'people' ? (
          <>
            <span className="text-sm font-medium text-slate-800 tabular-nums">
              {points.length} {points.length === 1 ? 'person' : 'people'} marked
            </span>
            <button type="button" disabled={!points.length || busy} onClick={() => setPoints(ps => ps.slice(0, -1))}
                    className="px-3 py-1.5 rounded bg-slate-100 text-slate-700 text-sm hover:bg-slate-200 disabled:opacity-50">
              Undo last
            </button>
            <button type="button" disabled={busy} onClick={() => save({ points })}
                    className="px-3 py-1.5 rounded bg-emerald-600 text-white text-sm font-semibold hover:bg-emerald-500 disabled:opacity-50">
              Done — save
            </button>
          </>
        ) : (
          <>
            <label className="text-sm text-slate-800">
              People in clip{' '}
              <input type="number" min={0} max={200} value={count} onChange={e => setCount(e.target.value)}
                     className="ml-1 w-20 border rounded px-2 py-1 text-sm" />
            </label>
            <button type="button" disabled={!countValid || busy} onClick={() => save({ count: countValue })}
                    className="px-3 py-1.5 rounded bg-emerald-600 text-white text-sm font-semibold hover:bg-emerald-500 disabled:opacity-50">
              Save
            </button>
          </>
        )}
        <button type="button" disabled={busy} onClick={() => save({ skip: true })}
                className="px-3 py-1.5 rounded bg-slate-100 text-slate-700 text-sm hover:bg-slate-200 disabled:opacity-50"
                title="Unclear picture — leave it out of the test set">
          Skip
        </button>
      </div>
    </Card>
  )
}
