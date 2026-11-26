// Training Review — the EXCEPTIONS queue.
//
// An operator's True/False on the Alerts page is the training label
// (when direct promotion is on). Only what needs a second look lands here:
// marks that disagree with the model, mass-marking bursts, a random audit
// sample of auto-promoted labels, and anything still ambiguous.

import { useCallback, useEffect, useState } from 'react'
import { Badge, Card, PageHeader } from '@/components/ui/Primitives'
import { useSnapshot } from '@/components/AlertCard'
import {
  trainingEvidence, type Evidence, type ExampleKind, type Readiness, type ReviewBucket,
} from '@/api/models'

const BUCKETS: { key: ReviewBucket; label: string; hint: string }[] = [
  { key: 'disagreement', label: 'Disagreements',
    hint: "The operator's mark and the AI disagree. Decide which is right." },
  { key: 'burst', label: 'Burst-held',
    hint: 'Many marks the same way in a few minutes (queue clearing). Check before training.' },
  { key: 'audit', label: 'Audit sample',
    hint: 'A random share of labels that trained automatically. Spot-check them.' },
  { key: 'ambiguous', label: 'Ambiguous',
    hint: 'Person alerts marked False without saying whether a person was there, and older labels.' },
  { key: 'approved', label: 'Approved', hint: 'Trainable labels.' },
  { key: 'rejected', label: 'Rejected', hint: 'Labels a reviewer rejected.' },
  { key: 'excluded', label: 'Excluded', hint: 'Unclear, bad image, policy errors, no learnable object.' },
]
const ACTIONABLE: ReviewBucket[] = ['disagreement', 'burst', 'audit', 'ambiguous']

const KIND_TEXT: Record<ExampleKind, string> = {
  positive: 'Positive (object present)',
  negative: 'Negative (object absent)',
  uncertain: 'Uncertain',
  policy_error: 'Policy error (real, but rule was wrong)',
}

const PATH_TEXT: Record<string, string> = {
  auto_agreement: 'Trained: operator and AI agreed',
  auto_trusted: 'Trained: trusted labeller',
  held_disagreement: 'Held: operator and AI disagree',
  held_burst: 'Held: part of a mass-marking burst',
  held_ambiguous: 'Held: person alert marked False without a choice',
  pending_manual: 'Waiting: direct promotion was off',
  legacy: 'Older label',
  reviewed: 'Reviewed',
  audited_ok: 'Audited and confirmed',
}

function modelSays(e: Evidence): string | null {
  const m = e.gate?.model
  if (!m) return null
  const who = m.source === 'ai_second_opinion' ? 'AI second opinion'
    : m.source === 'detector_confidence' ? 'Detector' : 'Model'
  const conf = typeof m.confidence === 'number' ? ` (${Math.round(m.confidence * 100)}%)` : ''
  return `${who} says ${m.says}${conf}`
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
  const [bucket, setBucket] = useState<ReviewBucket>('disagreement')
  const [items, setItems] = useState<Evidence[]>([])
  const [counts, setCounts] = useState<Record<string, number>>({})
  const [ready, setReady] = useState<Readiness[]>([])
  const [message, setMessage] = useState<string | null>(null)

  const load = useCallback(() => {
    trainingEvidence.list(bucket).then(p => { setItems(p.items); setCounts(p.counts) })
      .catch(e => setMessage(String(e)))
    trainingEvidence.readiness().then(setReady).catch(() => {})
  }, [bucket])
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

  const def = BUCKETS.find(b => b.key === bucket)!
  const actionable = ACTIONABLE.includes(bucket)
  return (
    <div className="p-6 space-y-4">
      <PageHeader title="Training review" />
      <Card className="p-3 text-sm text-slate-700 dark:text-slate-200">
        Operators' True/False marks on the Alerts page train the AI directly. This page only
        holds the exceptions that need a second look.
      </Card>

      {ready.length > 0 && (
        <Card className="p-3 text-sm">
          <div className="font-medium mb-1 text-slate-800 dark:text-slate-100">Ready to train?</div>
          {ready.map(r => (
            <div key={r.target_class} className="text-xs text-slate-600 dark:text-slate-300">
              <strong>{r.target_class}</strong>: {r.approved_positives} positives, {r.approved_negatives} negatives
              trainable, {r.pending_review} waiting — {r.ready ? '✓ ready' : r.reasons.join('; ')}
            </div>
          ))}
        </Card>
      )}

      <div className="flex flex-wrap gap-2">
        {BUCKETS.map(b => (
          <button key={b.key} type="button" title={b.hint} onClick={() => setBucket(b.key)}
                  className={'px-3 py-1.5 rounded text-sm font-medium '
                    + (bucket === b.key ? 'bg-slate-800 text-white dark:bg-slate-200 dark:text-slate-900'
                      : 'vg-on-light bg-slate-100 text-slate-700 hover:bg-slate-200')}>
            {b.label} ({counts[b.key] ?? '…'})
          </button>
        ))}
      </div>
      <div className="text-xs text-slate-500 dark:text-slate-300">{def.hint}</div>
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
              {e.audit_sample && <Badge color="slate">audit sample</Badge>}
            </div>
            <div className="text-xs text-slate-600 dark:text-slate-300 space-y-0.5">
              <div>From {e.detection_type ?? 'alert'} #{e.source_alert_id ?? '—'}
                {e.dismiss_reason ? ` · marked “${e.dismiss_reason.replaceAll('_', ' ')}”` : ''}
                {' · '}{e.box_count} box(es)</div>
              {e.promotion_path && <div>{PATH_TEXT[e.promotion_path] ?? e.promotion_path}</div>}
              {modelSays(e) && <div>{modelSays(e)}</div>}
              {e.gate?.burst && <div>{e.gate.burst.count} same-way marks in {e.gate.burst.window_minutes} min</div>}
            </div>
            {actionable && (
              <div className="mt-2 flex flex-wrap gap-1">
                {e.example_kind === 'uncertain' ? (<>
                  <button type="button" className="px-2 py-1 rounded bg-sky-700 text-white text-xs"
                          onClick={() => decide(e, 'approve', 'negative')}>No person there (negative)</button>
                  <button type="button" className="px-2 py-1 rounded bg-slate-600 text-white text-xs"
                          onClick={() => decide(e, 'exclude', 'policy_error')}>Policy error</button>
                </>) : (
                  <button type="button" className="px-2 py-1 rounded bg-emerald-700 text-white text-xs"
                          onClick={() => decide(e, 'approve')}>
                    {bucket === 'audit' ? 'Looks right' : 'Approve'}
                  </button>
                )}
                <button type="button" className="px-2 py-1 rounded bg-red-700 text-white text-xs"
                        onClick={() => decide(e, 'reject')}>Reject</button>
                <button type="button" className="px-2 py-1 rounded bg-slate-500 text-white text-xs"
                        onClick={() => decide(e, 'exclude')}>Exclude</button>
                {e.example_kind !== 'uncertain' && (
                  <button type="button" className="px-2 py-1 rounded bg-amber-700 text-white text-xs"
                          onClick={() => decide(e, 'exclude', 'policy_error')}>Policy error</button>
                )}
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
