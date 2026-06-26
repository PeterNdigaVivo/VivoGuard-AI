// Shared alert card — used by both the per-store dashboard feed and
// the chain /alerts page. Single component, single look. Server
// computes title/body/severity; the frontend just renders.
//
// Features:
//   • Severity colour bar on the left edge (red / amber / blue)
//   • Title with embedded camera name + emoji
//   • Plain-English body with operational context
//   • 160×90 snapshot thumbnail on the right (placeholder on miss)
//   • Action row: Resolved · Confirm · Dismiss · View camera · Add note · View clip
//   • Investigation notes accordion (visible when notes exist)
//   • Click thumbnail → lightbox modal (full snapshot)
//   • "View clip" → modal saying clip unavailable + link to live view
//
// Grouping is handled by the OUTER component (the feed) — this
// component just renders one card. The group helper is exported
// separately so callers control grouping policy.

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { alerts as alertsApi, type Alert } from '@/api/alerts'


// <img src=…> can't carry the bearer header, so we fetch the
// snapshot endpoint with credentials and turn the blob into an
// object URL. Cached at module scope so groups of alerts referring
// to the same camera don't refetch — sub-fetches dedupe naturally
// because the URL is keyed by alert id.
const _snapshotCache = new Map<number, string>()

function useSnapshot(alertId: number, url: string | null) {
  const [src, setSrc] = useState<string | null>(_snapshotCache.get(alertId) ?? null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    if (!url) { setFailed(true); return }
    if (_snapshotCache.has(alertId)) {
      setSrc(_snapshotCache.get(alertId)!); return
    }
    let cancelled = false
    let created: string | null = null
    const tok = localStorage.getItem('vg_access_token') ?? ''
    fetch(url, { headers: { Authorization: `Bearer ${tok}` } })
      .then(r => r.ok ? r.blob() : null)
      .then(blob => {
        if (cancelled || !blob) {
          if (!cancelled) setFailed(true)
          return
        }
        created = URL.createObjectURL(blob)
        _snapshotCache.set(alertId, created)
        setSrc(created)
      })
      .catch(() => { if (!cancelled) setFailed(true) })
    return () => {
      cancelled = true
      // Don't revoke `created` — we cache it for future renders.
    }
  }, [alertId, url])

  return { src, failed }
}


const SEVERITY_BAR: Record<'critical' | 'warning' | 'info' | 'default', string> = {
  critical: 'bg-red-500',
  warning:  'bg-amber-500',
  info:     'bg-sky-500',
  default:  'bg-slate-300',
}
const SEVERITY_BADGE: Record<'critical' | 'warning' | 'info' | 'default', string> = {
  critical: 'bg-red-100 text-red-700',
  warning:  'bg-amber-100 text-amber-800',
  info:     'bg-sky-100 text-sky-700',
  default:  'bg-slate-100 text-slate-600',
}
// Traffic-light labels non-technical managers understand. Prefer the
// server's `severity_label` (URGENT/ATTENTION/INFO); fall back to the
// technical severity for older API responses.
const SEVERITY_LABEL: Record<'critical' | 'warning' | 'info' | 'default', string> = {
  critical: '🔴 URGENT',
  warning:  '🟡 ATTENTION',
  info:     '🔵 INFO',
  default:  '🔵 INFO',
}
const LABEL_FROM_SERVER: Record<string, string> = {
  URGENT: '🔴 URGENT', ATTENTION: '🟡 ATTENTION', INFO: '🔵 INFO',
}

function sevKey(s: string | null): 'critical' | 'warning' | 'info' | 'default' {
  if (s === 'critical' || s === 'warning' || s === 'info') return s
  return 'default'
}


export interface AlertCardProps {
  alert: Alert
  // Group metadata — when this alert is the head of a group of
  // similar repeats, the feed passes count + last timestamp + the
  // sibling list so the user can expand to see them.
  groupCount?: number
  groupLast?: string
  groupSiblings?: Alert[]
  onChanged?: () => void
}

export function AlertCard({ alert: incoming, groupCount, groupLast, groupSiblings, onChanged }: AlertCardProps) {
  // Local copy of the alert so we can reflect a resolve / dismiss
  // immediately without waiting for the parent's reload — feels
  // instantaneous and never flashes back to "new".
  const [alert, setLocal] = useState(incoming)
  useEffect(() => { setLocal(incoming) }, [incoming])
  const sev = sevKey(alert.severity)
  // "Resolved" covers the three terminal statuses the API can set:
  // resolved (everyday "I handled it") + confirmed (legacy alias the
  // /resolve endpoint still uses) + dismissed ("not a problem").
  const isResolved  = ['resolved', 'confirmed'].includes(alert.status)
  const isDismissed = alert.status === 'dismissed'
  const isClosed    = isResolved || isDismissed
  const [lightbox, setLightbox] = useState(false)
  const [snapFailed, setSnapFailed] = useState(false)
  const [clipModal, setClipModal] = useState(false)
  const [noteOpen, setNoteOpen] = useState(false)
  const [noteText, setNoteText] = useState('')
  const [groupExpanded, setGroupExpanded] = useState(false)
  const [busy, setBusy] = useState(false)

  async function act(fn: () => Promise<unknown>) {
    setBusy(true)
    try { await fn(); onChanged?.() }
    finally { setBusy(false) }
  }

  async function acknowledgeOne() {
    // 👁 I'm on it — operational acknowledgement. Stamps
    // acknowledged_at so the lifecycle pip moves to the middle step
    // while keeping the alert in the active list (status stays 'new'
    // so the True/False feedback buttons remain available).
    setLocal({ ...alert, acknowledged_at: new Date().toISOString() })
    try {
      await alertsApi.acknowledge(alert.id)
      onChanged?.()
    } catch (e) {
      setLocal(incoming)
      window.alert('Could not acknowledge. ' + e)
    }
  }

  async function markTrue() {
    // ✅ True Alert — the AI was right. Positive training sample via
    // /alerts/{id}/confirm → absorb_confirmed.
    if (!window.confirm('Mark this alert as TRUE? It will be added to AI training as a positive example.')) return
    setLocal({ ...alert, status: 'confirmed',
               resolved_at: new Date().toISOString() })
    try {
      await alertsApi.confirm(alert.id)
      window.dispatchEvent(new CustomEvent('vg:alert-resolved',
        { detail: { id: alert.id, action: 'resolve' } }))
      onChanged?.()
    } catch (e) {
      setLocal(incoming)   // rollback
      window.alert('Could not mark True. ' + e)
    }
  }

  async function markFalse() {
    // ❌ False Alert — the AI was wrong. Hard-negative training
    // sample via /alerts/{id}/dismiss → absorb_dismissed.
    if (!window.confirm('Mark this alert as FALSE? It will be added to AI training as a negative example.')) return
    setLocal({ ...alert, status: 'dismissed',
               resolved_at: new Date().toISOString() })
    try {
      await alertsApi.dismiss(alert.id)
      window.dispatchEvent(new CustomEvent('vg:alert-resolved',
        { detail: { id: alert.id, action: 'dismiss' } }))
      onChanged?.()
    } catch (e) {
      setLocal(incoming)
      window.alert('Could not mark False. ' + e)
    }
  }

  async function submitNote() {
    if (!noteText.trim()) return
    await act(() => alertsApi.addNote(alert.id, noteText.trim()))
    setNoteText('')
    setNoteOpen(false)
  }

  const { src: snapUrl, failed: snapAuthFailed } = useSnapshot(alert.id, alert.snapshot_url)

  return (
    <div className={'relative bg-white rounded border border-slate-200 overflow-hidden transition-opacity '
                    + (isClosed ? 'opacity-60' : '')}>
      {/* Severity colour bar — greyed when resolved/dismissed. */}
      <div className={'absolute left-0 top-0 bottom-0 w-1 '
                      + (isClosed ? 'bg-slate-300' : SEVERITY_BAR[sev])} />

      <div className="pl-4 pr-3 py-3 flex flex-col sm:flex-row gap-3">
        {/* Left: title + body + actions */}
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-2 mb-1">
            {/* When still new, show the traffic-light severity badge.
                When resolved/dismissed, show the closure badge instead
                so the operator sees its state at a glance. */}
            {isResolved ? (
              <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded font-semibold bg-emerald-100 text-emerald-800">
                ✅ RESOLVED
              </span>
            ) : isDismissed ? (
              <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded font-semibold bg-slate-200 text-slate-700">
                ◯ DISMISSED
              </span>
            ) : (
              <span className={'text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded font-semibold ' +
                SEVERITY_BADGE[sev]}>
                {alert.severity_label
                  ? (LABEL_FROM_SERVER[alert.severity_label] ?? alert.severity_label)
                  : SEVERITY_LABEL[sev]}
              </span>
            )}
            <span className="text-xs text-slate-500">{formatTime(alert.created_at)}</span>
            {alert.camera_name && (
              <span className="text-xs text-slate-500">· {alert.camera_name}</span>
            )}
            {groupCount && groupCount > 1 && (
              <button onClick={() => setGroupExpanded(g => !g)}
                      className="text-[11px] text-sky-700 hover:underline">
                ×{groupCount} today (last: {formatTime(groupLast ?? alert.created_at)}) {groupExpanded ? '▴' : '▾'}
              </button>
            )}
          </div>
          <div className="font-semibold text-slate-800 text-base">
            {alert.plain_title ?? alert.title ?? (alert.detection_type ?? 'Alert')}
          </div>
          {alert.body && (
            <div className="text-sm text-slate-600 mt-1">{alert.body}</div>
          )}

          {/* What to do — plain-English steps for non-technical staff. */}
          {alert.what_to_do && alert.what_to_do.length > 0 && (
            <div className="mt-2 bg-slate-50 rounded p-2">
              <div className="text-xs font-semibold text-slate-700 mb-1">What to do:</div>
              <ol className="list-decimal ml-5 text-sm text-slate-700 space-y-0.5">
                {alert.what_to_do.map((s, i) => <li key={i}>{s}</li>)}
              </ol>
            </div>
          )}
          {/* When-it-happened line. Server-rendered in the camera's
              store-local timezone so it always reads as wall-clock
              EAT (or whatever the store is set to). Shown directly
              under the two-sentence body so the operator's eye picks
              up the time as part of the same paragraph block. */}
          {alert.time_range && (
            <div className="text-xs text-slate-600 mt-1 font-medium">{alert.time_range}</div>
          )}

          {/* Group siblings */}
          {groupExpanded && groupSiblings && groupSiblings.length > 0 && (
            <div className="mt-2 pl-2 border-l-2 border-slate-200 space-y-0.5 text-xs text-slate-500">
              {groupSiblings.map(s => (
                <div key={s.id}>{formatTime(s.created_at)} — {s.title ?? s.detection_type}</div>
              ))}
            </div>
          )}

          {/* Notes */}
          {alert.notes && (
            <details className="mt-2">
              <summary className="text-xs text-slate-500 cursor-pointer hover:text-slate-700">
                📋 Investigation notes
              </summary>
              <pre className="text-xs text-slate-700 mt-1 whitespace-pre-wrap font-sans bg-slate-50 rounded p-2">
                {alert.notes}
              </pre>
            </details>
          )}

          {/* Lifecycle progress pip — Generated → Acknowledged →
              Resolved. Filled steps are dark; the active step pulses;
              future steps are pale. Hides when the alert is dismissed
              (acknowledge / resolve aren't the relevant flow). */}
          {!isDismissed && (
            <LifecyclePip
              createdAt={alert.created_at}
              acknowledgedAt={alert.acknowledged_at}
              resolvedAt={alert.resolved_at}
              isResolved={isResolved}
            />
          )}

          {/* Closure footer — shown only when already resolved or dismissed. */}
          {isClosed && (
            <div className="mt-2 text-xs text-slate-500">
              {isResolved ? 'Resolved' : 'Dismissed'}
              {alert.resolved_at ? ` at ${formatTime(alert.resolved_at)}` : ''}
            </div>
          )}

          {/* Action row.
              The two prominent training-feedback buttons sit first
              and visually dominate (bold, solid colour, white text)
              so operators can't miss them. The auxiliary buttons
              (acknowledge, see live, note, clip) keep their muted
              tone. Once the operator has cast a verdict, both
              True/False buttons collapse into a single disabled
              "✓ Marked True/False" chip so the choice is obvious
              and can't be double-submitted. */}
          <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
            {alert.status === 'new' ? (
              <>
                <FeedbackBtn onClick={markTrue} tone="green" disabled={busy}>
                  ✅ True Alert
                </FeedbackBtn>
                <FeedbackBtn onClick={markFalse} tone="red" disabled={busy}>
                  ❌ False Alert
                </FeedbackBtn>
              </>
            ) : (
              <span className={'px-3 py-1.5 rounded font-bold text-white '
                                + (isDismissed ? 'bg-red-600' : 'bg-green-600')}>
                ✓ Marked {isDismissed ? 'False' : 'True'}
              </span>
            )}
            {alert.status === 'new' && !alert.acknowledged_at && (
              <ActionBtn onClick={acknowledgeOne} tone="amber" disabled={busy}>
                👁 I'm on it
              </ActionBtn>
            )}
            {alert.camera_id && (
              <Link to={`/live`}
                    className="px-2 py-1 rounded bg-slate-100 text-slate-700 hover:bg-slate-200">
                📹 See Live Camera
              </Link>
            )}
            <ActionBtn onClick={() => setNoteOpen(o => !o)} tone="slate" disabled={busy}>
              📋 Write a note
            </ActionBtn>
            <ActionBtn onClick={() => setClipModal(true)} tone="slate" disabled={busy}>
              📹 View clip
            </ActionBtn>
          </div>

          {/* Note composer */}
          {noteOpen && (
            <div className="mt-2 flex gap-2">
              <input value={noteText}
                     onChange={e => setNoteText(e.target.value)}
                     placeholder="Add an investigation note…"
                     className="flex-1 text-sm border border-slate-200 rounded px-2 py-1"
                     onKeyDown={(e) => { if (e.key === 'Enter') submitNote() }} />
              <button onClick={submitNote} disabled={busy || !noteText.trim()}
                      className="text-xs px-2 py-1 rounded bg-sky-600 text-white hover:bg-sky-500 disabled:opacity-50">
                Save
              </button>
            </div>
          )}
        </div>

        {/* Right: thumbnail (+ checkout-dwell filmstrip when present) */}
        <div className="sm:w-40 sm:flex-shrink-0">
          {snapUrl && !snapFailed && !snapAuthFailed ? (
            <img src={snapUrl}
                 alt=""
                 onError={() => setSnapFailed(true)}
                 onClick={() => setLightbox(true)}
                 className="w-full sm:w-40 aspect-video object-cover rounded bg-slate-100 cursor-zoom-in" />
          ) : (
            <div className="w-full sm:w-40 aspect-video flex flex-col items-center justify-center bg-slate-100 rounded text-slate-400">
              <div className="text-2xl">📷</div>
              <div className="text-[10px] mt-1">
                {alert.camera_name ?? 'Snapshot'}
              </div>
            </div>
          )}
          {/* Checkout-dwell timeline — one JPEG per 60s from arrival
              to departure. NULL for all other alert types. */}
          {(alert.snapshot_count ?? 0) > 0 && (
            <div className="mt-2">
              <div className="text-[10px] text-slate-500 mb-1">
                Timeline · {alert.snapshot_count} snapshot{alert.snapshot_count === 1 ? '' : 's'}
              </div>
              <div className="flex gap-1 overflow-x-auto">
                {Array.from({ length: alert.snapshot_count ?? 0 }, (_, i) => (
                  <a key={i}
                     href={`/api/alerts/${alert.id}/snapshot/${i}`}
                     target="_blank"
                     rel="noopener noreferrer"
                     className="shrink-0 block w-12 h-9 rounded overflow-hidden
                                bg-black border border-slate-300 hover:border-sky-500">
                    <img src={`/api/alerts/${alert.id}/snapshot/${i}`}
                         alt={`Frame ${i + 1}`}
                         loading="lazy"
                         className="w-full h-full object-cover" />
                  </a>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Lightbox modal */}
      {lightbox && snapUrl && !snapFailed && !snapAuthFailed && (
        <div className="fixed inset-0 bg-black/85 flex items-center justify-center z-50 p-6"
             onClick={() => setLightbox(false)}>
          <img src={snapUrl} alt="" className="max-w-full max-h-full rounded shadow-xl" />
        </div>
      )}

      {/* Clip modal — clip recording isn't implemented yet so we
          surface a clear "unavailable" state with the live-view
          fallback, instead of silently doing nothing. */}
      {clipModal && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-6"
             onClick={() => setClipModal(false)}>
          <div className="bg-white rounded p-6 max-w-md w-full" onClick={e => e.stopPropagation()}>
            <div className="text-lg font-semibold mb-2">Clip unavailable</div>
            <div className="text-sm text-slate-600 mb-4">
              Recording isn't enabled for this camera yet. You can watch
              the live feed instead.
            </div>
            <div className="flex justify-end gap-2">
              <button onClick={() => setClipModal(false)}
                      className="px-3 py-1.5 rounded bg-slate-100 hover:bg-slate-200 text-sm">
                Close
              </button>
              {alert.camera_id && (
                <Link to="/live" onClick={() => setClipModal(false)}
                      className="px-3 py-1.5 rounded bg-sky-600 text-white hover:bg-sky-500 text-sm">
                  Open live view →
                </Link>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function ActionBtn({ onClick, tone, disabled, children }: {
  onClick: () => void
  tone: 'emerald' | 'slate' | 'amber' | 'rose'
  disabled?: boolean
  children: React.ReactNode
}) {
  const cls = tone === 'emerald'
    ? 'bg-emerald-100 text-emerald-800 hover:bg-emerald-200'
    : tone === 'amber'
      ? 'bg-amber-100 text-amber-800 hover:bg-amber-200'
      : tone === 'rose'
        ? 'bg-rose-100 text-rose-800 hover:bg-rose-200'
        : 'bg-slate-100 text-slate-700 hover:bg-slate-200'
  return (
    <button onClick={onClick} disabled={disabled}
            className={'px-2 py-1 rounded ' + cls + ' disabled:opacity-50'}>
      {children}
    </button>
  )
}

// Bold solid-colour variant for the two AI training-feedback buttons.
// Visually dominates the auxiliary `ActionBtn` chips so operators
// can't miss the primary call to action.
function FeedbackBtn({ onClick, tone, disabled, children }: {
  onClick: () => void
  tone: 'green' | 'red'
  disabled?: boolean
  children: React.ReactNode
}) {
  const cls = tone === 'green'
    ? 'bg-green-600 hover:bg-green-700'
    : 'bg-red-600 hover:bg-red-700'
  return (
    <button onClick={onClick} disabled={disabled}
            className={'px-3 py-1.5 rounded text-white font-bold shadow-sm '
                       + cls + ' disabled:opacity-50'}>
      {children}
    </button>
  )
}


function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      hour: '2-digit', minute: '2-digit',
      month: 'short', day: 'numeric',
    })
  } catch {
    return iso
  }
}


// ---- Grouping helper ------------------------------------------------
//
// Groups repeat-of-same-thing alerts within the same day so the feed
// reads as
//   "Counter Unstaffed ×7 today (last: 3:06 PM)"
// instead of seven separate rows for one ongoing condition.
//
// Rule: alerts share a group iff they have the same detection_type,
// the same camera_id, and the same calendar day. The "head" of each
// group is the most recent alert; siblings render in the expand
// accordion.

export function groupAlerts(rows: Alert[]): {
  head: Alert; count: number; last: string; siblings: Alert[]
}[] {
  const groups = new Map<string, Alert[]>()
  for (const a of rows) {
    const day = (a.created_at || '').slice(0, 10)
    const key = `${day}|${a.detection_type ?? ''}|${a.camera_id ?? 'na'}`
    const arr = groups.get(key) ?? []
    arr.push(a)
    groups.set(key, arr)
  }
  const out: { head: Alert; count: number; last: string; siblings: Alert[] }[] = []
  for (const arr of groups.values()) {
    arr.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))
    out.push({
      head: arr[0],
      count: arr.length,
      last: arr[0].created_at,
      siblings: arr.slice(1),
    })
  }
  out.sort((a, b) => (b.last || '').localeCompare(a.last || ''))
  return out
}


// Lifecycle pip — three-dot timeline showing how far the alert is
// through the Generated → Acknowledged → Resolved flow. Each step
// renders the timestamp inline when reached. The active (current)
// step pulses so the operator's eye lands on what's next.
function LifecyclePip({ createdAt, acknowledgedAt, resolvedAt, isResolved }: {
  createdAt: string | null
  acknowledgedAt: string | null
  resolvedAt: string | null
  isResolved: boolean
}) {
  // Steps: generated (always), acknowledged (optional), resolved.
  const steps = [
    { label: 'Generated',    ts: createdAt,      done: !!createdAt },
    { label: 'Acknowledged', ts: acknowledgedAt, done: !!acknowledgedAt || isResolved },
    { label: 'Resolved',     ts: resolvedAt,     done: isResolved },
  ]
  const activeIdx = steps.findIndex(s => !s.done)
  return (
    <div className="mt-2 flex items-center gap-1 text-[11px] text-slate-500"
         aria-label="Alert lifecycle">
      {steps.map((s, i) => {
        const active = i === activeIdx
        const dotCls = s.done
          ? 'bg-emerald-500 border-emerald-500'
          : active
            ? 'bg-amber-400 border-amber-400 animate-pulse'
            : 'bg-slate-200 border-slate-300'
        return (
          <span key={s.label} className="inline-flex items-center gap-1">
            <span className={'inline-block w-2 h-2 rounded-full border ' + dotCls} />
            <span className={s.done ? 'text-slate-700' : ''}>
              {s.label}
              {s.ts && <span className="text-slate-400"> ({formatTime(s.ts)})</span>}
            </span>
            {i < steps.length - 1 && (
              <span className="inline-block w-4 h-px bg-slate-200 mx-0.5" />
            )}
          </span>
        )
      })}
    </div>
  )
}
