// Labelling Sprint — fast-review queue for unlabelled alerts.
// Keyboard: C = confirm, D = dismiss, ← = undo. Batch of 20.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge, Button, Card, PageHeader, Skeleton } from '@/components/ui/Primitives'
import { labels as labelsApi, type SprintAlert } from '@/api/labels'

const BATCH = 20
const UNDO_STACK_DEPTH = 5

export default function SprintPage() {
  const [queue, setQueue]     = useState<SprintAlert[] | null>(null)
  const [idx,   setIdx]       = useState(0)
  const [busy,  setBusy]      = useState(false)
  const [undoIds, setUndoIds] = useState<number[]>([])   // FIFO, head = most recent
  const [reviewed, setReviewed] = useState(0)            // session counter
  const [err, setErr] = useState<string | null>(null)
  const detectionTypeRef = useRef<string | undefined>(undefined)

  const loadBatch = useCallback(async (detection_type?: string) => {
    setBusy(true); setErr(null)
    try {
      const items = await labelsApi.queue({ detection_type, limit: BATCH })
      setQueue(items); setIdx(0)
    } catch (e) { setErr(String(e)) }
    finally { setBusy(false) }
  }, [])

  useEffect(() => { loadBatch() }, [loadBatch])

  const current = (queue && queue[idx]) || null
  const remaining = queue ? Math.max(0, queue.length - idx) : 0

  async function vote(verdict: 'confirm' | 'dismiss') {
    if (!current || busy) return
    setBusy(true)
    const id = current.id
    try {
      if (verdict === 'confirm') await labelsApi.confirm(id)
      else                       await labelsApi.dismiss(id)
      setUndoIds(prev => [id, ...prev].slice(0, UNDO_STACK_DEPTH))
      setReviewed(r => r + 1)
      // Advance — if we reach the end, transparently load the next batch.
      if (idx + 1 >= (queue?.length ?? 0)) {
        await loadBatch(detectionTypeRef.current)
      } else {
        setIdx(i => i + 1)
      }
    } catch (e) { setErr(String(e)) }
    finally { setBusy(false) }
  }

  async function undo() {
    const lastId = undoIds[0]
    if (!lastId || busy) return
    setBusy(true)
    try {
      await labelsApi.undo(lastId)
      setUndoIds(prev => prev.slice(1))
      setReviewed(r => Math.max(0, r - 1))
      // Refetch so the undone alert reappears at the top.
      await loadBatch(detectionTypeRef.current)
    } catch (e) { setErr(String(e)) }
    finally { setBusy(false) }
  }

  // Keyboard shortcuts. Ignored when user is typing in an input.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.target instanceof HTMLInputElement
          || e.target instanceof HTMLTextAreaElement) return
      const k = e.key.toLowerCase()
      if (k === 'c') { e.preventDefault(); vote('confirm') }
      else if (k === 'd') { e.preventDefault(); vote('dismiss') }
      else if (k === 'arrowleft') { e.preventDefault(); undo() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current, busy, undoIds])

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <PageHeader title="🎯 Labelling Sprint" actions={
        <Link to="/ai-learning" className="text-sm text-sky-600 hover:underline">
          ← Back to AI Learning
        </Link>
      } />

      {/* Progress + shortcuts */}
      <div className="mb-3 flex items-center gap-4 text-sm text-slate-600 dark:text-slate-300">
        <span>{reviewed} reviewed this session</span>
        <span>·</span>
        <span>{remaining} / {queue?.length ?? 0} left in batch</span>
        <div className="ml-auto flex items-center gap-2 text-xs">
          <Kbd>C</Kbd> confirm
          <Kbd>D</Kbd> dismiss
          <Kbd>←</Kbd> undo
        </div>
      </div>

      {err && (
        <Card className="p-3 mb-3 text-rose-700 bg-rose-50 border-rose-200">
          {err}
        </Card>
      )}

      {queue === null && (
        <div className="space-y-3">
          <Skeleton className="h-48" />
          <Skeleton className="h-12" />
        </div>
      )}

      {queue && queue.length === 0 && (
        <Card className="p-12 text-center">
          <div className="text-5xl mb-3">🎉</div>
          <div className="text-lg font-medium mb-1">
            Queue is empty — nothing left to label
          </div>
          <div className="text-slate-500 dark:text-slate-300 mb-4">
            Operator feedback will appear here as new alerts fire.
          </div>
          <Button onClick={() => loadBatch()}>Refresh</Button>
        </Card>
      )}

      {current && (
        <SprintCard alert={current} onConfirm={() => vote('confirm')}
                                     onDismiss={() => vote('dismiss')}
                                     onUndo={undoIds.length ? undo : undefined}
                                     busy={busy} />
      )}
    </div>
  )
}


function SprintCard({ alert, onConfirm, onDismiss, onUndo, busy }: {
  alert: SprintAlert
  onConfirm: () => void
  onDismiss: () => void
  onUndo?:   () => void
  busy: boolean
}) {
  const dwell = useMemo(() => {
    if (alert.dwell_seconds == null) return 'N/A'
    const m = Math.floor(alert.dwell_seconds / 60)
    const s = alert.dwell_seconds % 60
    return m > 0 ? `${m}m ${s}s` : `${s}s`
  }, [alert.dwell_seconds])

  const sevColor: 'red' | 'amber' | 'slate' =
    alert.severity_4 === 'CRITICAL' ? 'red'
    : alert.severity_4 === 'HIGH'     ? 'amber'
    : alert.severity_4 === 'MEDIUM'   ? 'amber'
    :                                   'slate'

  const snapshotCount = alert.snapshot_count ?? 0

  return (
    <Card className="p-0 overflow-hidden">
      {/* Snapshot */}
      <div className="bg-black aspect-video flex items-center justify-center">
        {alert.snapshot_url
          ? <img src={alert.snapshot_url} alt={alert.title ?? 'alert'}
                 className="w-full h-full object-contain" />
          : <div className="text-slate-500 text-sm">No snapshot</div>}
      </div>

      {/* Checkout-dwell timeline filmstrip (one JPEG per 60s).
          Scrolls horizontally; click any thumbnail to lightbox. */}
      {snapshotCount > 0 && (
        <div className="bg-slate-100 dark:bg-slate-800 px-3 py-2 border-b border-slate-200 dark:border-slate-800">
          <div className="text-xs text-slate-500 dark:text-slate-300 mb-1.5">
            Timeline — {snapshotCount} snapshot{snapshotCount === 1 ? '' : 's'}
            <span className="text-slate-400"> · one every 60s from arrival to departure</span>
          </div>
          <div className="flex gap-1.5 overflow-x-auto pb-1">
            {Array.from({ length: snapshotCount }, (_, i) => (
              <a key={i}
                 href={`/api/alerts/${alert.id}/snapshot/${i}`}
                 target="_blank"
                 rel="noopener noreferrer"
                 className="shrink-0 block w-20 h-14 bg-black rounded overflow-hidden
                            border border-slate-300 hover:border-sky-500
                            transition-colors">
                <img src={`/api/alerts/${alert.id}/snapshot/${i}`}
                     alt={`Frame ${i + 1}`}
                     loading="lazy"
                     className="w-full h-full object-cover" />
              </a>
            ))}
          </div>
        </div>
      )}

      {/* Metadata strip */}
      <div className="p-4 border-b border-slate-200 dark:border-slate-800">
        <div className="flex items-start justify-between gap-3 mb-2">
          <div>
            <div className="text-lg font-semibold">
              {alert.plain_title ?? alert.title ?? 'Alert'}
            </div>
            <div className="text-sm text-slate-500 dark:text-slate-300">
              {alert.detection_type ?? '—'}
              {alert.confidence != null && ` · ${(alert.confidence * 100).toFixed(0)}%`}
              {' · Dwell '}{dwell}
            </div>
          </div>
          {alert.severity_4 && (
            <Badge color={sevColor}>{alert.severity_4}</Badge>
          )}
        </div>
        <div className="text-sm text-slate-600 dark:text-slate-300 space-y-0.5">
          <div>🏬 {alert.camera_name ?? 'Unknown camera'}</div>
          {alert.zone_name && <div>📐 {alert.zone_name}</div>}
          {alert.time_range && <div>🕒 {alert.time_range}</div>}
        </div>
      </div>

      {/* Verdict buttons */}
      <div className="p-4 flex items-center gap-2">
        <Button onClick={onConfirm} disabled={busy}>
          ✅ Confirm <span className="ml-2 opacity-60 text-xs">(C)</span>
        </Button>
        <Button onClick={onDismiss} disabled={busy} variant="ghost">
          ❌ Dismiss <span className="ml-2 opacity-60 text-xs">(D)</span>
        </Button>
        <div className="ml-auto">
          {onUndo
            ? <button onClick={onUndo} disabled={busy}
                      className="text-sm text-slate-600 dark:text-slate-300 hover:text-slate-900 dark:hover:text-slate-100">
                ← Undo last
              </button>
            : <span className="text-sm text-slate-400">← Undo last</span>}
        </div>
      </div>
    </Card>
  )
}


function Kbd({ children }: { children: React.ReactNode }) {
  return (
    <kbd className="px-1.5 py-0.5 text-xs font-mono rounded
                    border border-slate-300 dark:border-slate-800 bg-slate-50 dark:bg-slate-800 text-slate-700 dark:text-slate-200">
      {children}
    </kbd>
  )
}
