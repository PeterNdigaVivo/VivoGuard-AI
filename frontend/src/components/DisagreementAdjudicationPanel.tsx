import { useEffect, useMemo, useState } from 'react'
import { operations, type AssuranceCase } from '@/api/operations'
import { Button, Card, useToast } from '@/components/ui/Primitives'

type Verdict = 'confirm' | 'dismiss' | 'unclear'

export default function DisagreementAdjudicationPanel({ onClose }: {
  onClose: () => void
}) {
  const [cases, setCases] = useState<AssuranceCase[] | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [rationale, setRationale] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { push } = useToast()

  async function load() {
    setError(null)
    try {
      const rows = await operations.listReviewerDisagreements()
      const open = rows.filter(row => row.status !== 'resolved')
      setCases(open)
      setSelectedId(current => open.some(row => row.id === current)
        ? current : (open[0]?.id ?? null))
    } catch (err) {
      setError(String(err))
    }
  }

  useEffect(() => { void load() }, [])
  const selected = useMemo(
    () => cases?.find(row => row.id === selectedId) ?? null,
    [cases, selectedId],
  )

  async function adjudicate(verdict: Verdict) {
    if (!selected || rationale.trim().length < 8 || busy) return
    setBusy(true); setError(null)
    try {
      const result = await operations.adjudicateReviewerDisagreement(
        selected.id, verdict, rationale.trim(),
      )
      push(result.training_eligible
        ? 'Adjudicated; evidence is eligible for training.'
        : 'Adjudicated; evidence remains quarantined.')
      setRationale('')
      await load()
    } catch (err) {
      setError(String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card className="p-4 mb-4 border-violet-200">
      <div className="flex items-start justify-between gap-4 mb-3">
        <div>
          <h2 className="font-semibold">Independent disagreement adjudication</h2>
          <p className="text-sm text-slate-600 dark:text-slate-300">
            A third operator must review the evidence. Both earlier verdicts remain in the audit history.
          </p>
        </div>
        <Button variant="ghost" onClick={onClose}>Close</Button>
      </div>

      {error && <div className="mb-3 text-sm text-red-700">{error}</div>}
      {cases === null && <div className="text-sm text-slate-500">Loading disagreements…</div>}
      {cases?.length === 0 && (
        <div className="text-sm text-emerald-700">No disagreements require adjudication.</div>
      )}
      {cases && cases.length > 0 && (
        <div className="grid md:grid-cols-[17rem_1fr] gap-4">
          <div className="space-y-2 max-h-80 overflow-y-auto">
            {cases.map(row => (
              <button key={row.id} onClick={() => setSelectedId(row.id)}
                className={'w-full text-left rounded border p-2 text-sm ' +
                  (selectedId === row.id
                    ? 'border-violet-500 bg-violet-50 dark:bg-slate-800'
                    : 'border-slate-200 dark:border-slate-700')}>
                <div className="font-medium">Case #{row.id} · Alert #{row.alert_id ?? '—'}</div>
                <div className="text-slate-500">{row.title}</div>
              </button>
            ))}
          </div>
          {selected && (
            <div>
              <div className="text-sm mb-2">
                <span className="font-medium">Recorded disagreement:</span>{' '}
                {String(selected.evidence?.primary_verdict ?? 'unknown')} /{' '}
                {String(selected.evidence?.independent_verdict ?? 'unknown')}
              </div>
              {selected.alert_id && (
                <a className="text-sm text-sky-600 hover:underline"
                  href={`/alerts?search=${selected.alert_id}`} target="_blank" rel="noreferrer">
                  Open alert evidence ↗
                </a>
              )}
              <textarea value={rationale} onChange={event => setRationale(event.target.value)}
                placeholder="Explain the evidence and why this is the final classification (minimum 8 characters)."
                className="mt-3 w-full min-h-24 rounded border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 p-2 text-sm" />
              <div className="flex flex-wrap gap-2 mt-2">
                <Button disabled={busy || rationale.trim().length < 8}
                  onClick={() => adjudicate('confirm')}>Confirm true alert</Button>
                <Button variant="danger" disabled={busy || rationale.trim().length < 8}
                  onClick={() => adjudicate('dismiss')}>Confirm false alert</Button>
                <Button variant="ghost" disabled={busy || rationale.trim().length < 8}
                  onClick={() => adjudicate('unclear')}>Evidence unclear</Button>
              </div>
              <p className="mt-2 text-xs text-slate-500">
                If this camera-detector pair is quality-controlled, adjudication will not bypass that quarantine.
              </p>
            </div>
          )}
        </div>
      )}
    </Card>
  )
}
