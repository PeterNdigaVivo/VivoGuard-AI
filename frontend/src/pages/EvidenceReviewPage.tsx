// Training review — operator verdicts are evidence, not labels. Nothing
// here trains a model; approval only makes an example eligible, and a job
// starts only once a class has enough approved, balanced examples.

import { useCallback, useEffect, useState } from 'react'
import { Badge, Card, PageHeader } from '@/components/ui/Primitives'
import { useSnapshot } from '@/components/AlertCard'
import {
  trainingEvidence, type Evidence, type ExampleKind, type Readiness, type ReviewState,
} from '@/api/models'

const STATES: { key: ReviewState; label: string }[] = [
  { key: 'pending_review', label: 'Pending review' },
  { key: 'approved', label: 'Approved' },
  { key: 'rejected', label: 'Rejected' },
  { key: 'excluded', label: 'Excluded' },
]
const KIND_TEXT: Record<ExampleKind, string> = {
  positive: 'Positive (object present)',
  negative: 'Negative (object absent)',
  uncertain: 'Uncertain',
  policy_error: 'Policy error (real, but rule was wrong)',
}

// <img src> cannot send the bearer token; reuse the alert card's loader.
function Snapshot({ alertId, url }: { alertId: number; url: string }) {
  const { src, failed } = useSnapshot(alertId, url)
  if (failed) return <div className="h-40 mb-2 rounded bg-slate-100 dark:bg-slate-800 text-xs
                                      text-slate-500 dark:text-slate-300 grid place-items-center">
    Snapshot unavailable</div>
  return src
    ? <img src={src} alt="" className="w-full h-40 object-cover rounded mb-2" />
    : <div className="h-40 mb-2 rounded bg-slate-100 dark:bg-slate-800 animate-pulse" />
}

export default function EvidenceReviewPage() {
  const [state, setState] = useState<ReviewState>('pending_review')
  const [items, setItems] = useState<Evidence[]>([])
  const [total, setTotal] = useState(0)
  const [ready, setReady] = useState<Readiness[]>([])
  const [message, setMessage] = useState<string | null>(null)

  const load = useCallback(() => {
    trainingEvidence.list(state).then(p => { setItems(p.items); setTotal(p.total) })
      .catch(e => setMessage(String(e)))
    trainingEvidence.readiness().then(setReady).catch(() => {})
  }, [state])
  useEffect(load, [load])

  async function decide(e: Evidence, decision: 'approve' | 'reject' | 'exclude', kind?: ExampleKind) {
    setMessage(null)
    try {
      await trainingEvidence.review(e.id, decision, kind)
    } catch (err) {
      setMessage(`Not saved: ${err}`)
    }
    load()
  }

  return (
    <div className="p-6 space-y-4">
      <PageHeader title="Training review" />
      <Card className="p-3 text-sm text-slate-700 dark:text-slate-200">
        A True/False click on an alert lands here. Approve only what you are sure of:
        approved examples are the only ones a model can learn from.
      </Card>

      {ready.length > 0 && (
        <Card className="p-3 text-sm">
          <div className="font-medium mb-1 text-slate-800 dark:text-slate-100">Ready to train?</div>
          {ready.map(r => (
            <div key={r.target_class} className="text-xs text-slate-600 dark:text-slate-300">
              <strong>{r.target_class}</strong>: {r.approved_positives} positives, {r.approved_negatives} negatives
              approved, {r.pending_review} pending — {r.ready ? '✓ ready' : r.reasons.join('; ')}
            </div>
          ))}
        </Card>
      )}

      <div className="flex gap-2">
        {STATES.map(s => (
          <button key={s.key} type="button" onClick={() => setState(s.key)}
                  className={'px-3 py-1.5 rounded text-sm font-medium '
                    + (state === s.key ? 'bg-slate-800 text-white dark:bg-slate-200 dark:text-slate-900'
                      : 'vg-on-light bg-slate-100 text-slate-700 hover:bg-slate-200')}>
            {s.label}{state === s.key ? ` (${total})` : ''}
          </button>
        ))}
      </div>
      {message && <Card className="p-3 text-sm text-red-700 dark:text-red-300">{message}</Card>}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {items.map(e => (
          <Card key={e.id} className="p-3 text-sm">
            {e.preview_url && e.source_alert_id != null && (
              <Snapshot alertId={e.source_alert_id} url={e.preview_url} />
            )}
            <div className="flex flex-wrap gap-1 mb-1">
              {e.example_kind && <Badge color={e.example_kind === 'positive' ? 'green'
                : e.example_kind === 'negative' ? 'sky' : 'amber'}>{KIND_TEXT[e.example_kind]}</Badge>}
              {e.target_class && <Badge>{e.target_class}</Badge>}
            </div>
            <div className="text-xs text-slate-600 dark:text-slate-300">
              From {e.detection_type ?? 'alert'} #{e.source_alert_id ?? '—'}
              {e.dismiss_reason ? ` · dismissed as “${e.dismiss_reason.replaceAll('_', ' ')}”` : ''}
              {' · '}{e.box_count} box(es)
            </div>
            {state === 'pending_review' && (
              <div className="mt-2 flex flex-wrap gap-1">
                {e.example_kind === 'uncertain' ? (<>
                  <button type="button" className="px-2 py-1 rounded bg-sky-700 text-white text-xs"
                          onClick={() => decide(e, 'approve', 'negative')}>Approve as negative</button>
                  <button type="button" className="px-2 py-1 rounded bg-slate-600 text-white text-xs"
                          onClick={() => decide(e, 'exclude', 'policy_error')}>Policy error</button>
                </>) : (
                  <button type="button" className="px-2 py-1 rounded bg-emerald-700 text-white text-xs"
                          onClick={() => decide(e, 'approve')}>Approve</button>
                )}
                <button type="button" className="px-2 py-1 rounded bg-red-700 text-white text-xs"
                        onClick={() => decide(e, 'reject')}>Reject</button>
                <button type="button" className="px-2 py-1 rounded bg-slate-500 text-white text-xs"
                        onClick={() => decide(e, 'exclude')}>Exclude</button>
              </div>
            )}
          </Card>
        ))}
        {items.length === 0 && (
          <Card className="p-6 text-sm text-slate-500 dark:text-slate-300">Nothing here.</Card>
        )}
      </div>
    </div>
  )
}
