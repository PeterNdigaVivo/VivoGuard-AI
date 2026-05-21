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
const SEVERITY_LABEL: Record<'critical' | 'warning' | 'info' | 'default', string> = {
  critical: '🔴 Critical',
  warning:  '🟡 Warning',
  info:     '🔵 Info',
  default:  'Info',
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

export function AlertCard({ alert, groupCount, groupLast, groupSiblings, onChanged }: AlertCardProps) {
  const sev = sevKey(alert.severity)
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

  async function submitNote() {
    if (!noteText.trim()) return
    await act(() => alertsApi.addNote(alert.id, noteText.trim()))
    setNoteText('')
    setNoteOpen(false)
  }

  const { src: snapUrl, failed: snapAuthFailed } = useSnapshot(alert.id, alert.snapshot_url)

  return (
    <div className="relative bg-white rounded border border-slate-200 overflow-hidden">
      {/* Severity colour bar */}
      <div className={'absolute left-0 top-0 bottom-0 w-1 ' + SEVERITY_BAR[sev]} />

      <div className="pl-4 pr-3 py-3 flex flex-col sm:flex-row gap-3">
        {/* Left: title + body + actions */}
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-2 mb-1">
            <span className={'text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded ' +
              SEVERITY_BADGE[sev]}>
              {SEVERITY_LABEL[sev]}
            </span>
            <span className="text-xs text-slate-500">{formatTime(alert.created_at)}</span>
            {alert.status !== 'new' && (
              <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-slate-100 text-slate-600">
                {alert.status}
              </span>
            )}
            {groupCount && groupCount > 1 && (
              <button onClick={() => setGroupExpanded(g => !g)}
                      className="text-[11px] text-sky-700 hover:underline">
                ×{groupCount} today (last: {formatTime(groupLast ?? alert.created_at)}) {groupExpanded ? '▴' : '▾'}
              </button>
            )}
          </div>
          <div className="font-semibold text-slate-800">
            {alert.title ?? (alert.detection_type ?? 'Alert')}
          </div>
          {alert.body && (
            <div className="text-sm text-slate-600 mt-1">{alert.body}</div>
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

          {/* Action row */}
          <div className="mt-2 flex flex-wrap gap-1.5 text-xs">
            {alert.status === 'new' && (
              <>
                <ActionBtn onClick={() => act(() => alertsApi.resolve(alert.id))}
                           tone="emerald" disabled={busy}>
                  ✅ Resolved
                </ActionBtn>
                <ActionBtn onClick={() => act(() => alertsApi.confirm(alert.id))}
                           tone="slate" disabled={busy}>
                  Confirm (true)
                </ActionBtn>
                <ActionBtn onClick={() => act(() => alertsApi.dismiss(alert.id))}
                           tone="slate" disabled={busy}>
                  Dismiss
                </ActionBtn>
              </>
            )}
            {alert.camera_id && (
              <Link to={`/live`}
                    className="px-2 py-1 rounded bg-slate-100 text-slate-700 hover:bg-slate-200">
                👁️ View camera
              </Link>
            )}
            <ActionBtn onClick={() => setNoteOpen(o => !o)} tone="slate" disabled={busy}>
              📋 Add note
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

        {/* Right: thumbnail */}
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
  onClick: () => void; tone: 'emerald' | 'slate'; disabled?: boolean
  children: React.ReactNode
}) {
  const cls = tone === 'emerald'
    ? 'bg-emerald-100 text-emerald-800 hover:bg-emerald-200'
    : 'bg-slate-100 text-slate-700 hover:bg-slate-200'
  return (
    <button onClick={onClick} disabled={disabled}
            className={'px-2 py-1 rounded ' + cls + ' disabled:opacity-50'}>
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
