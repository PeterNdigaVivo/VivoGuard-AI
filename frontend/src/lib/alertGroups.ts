// ---- Grouping helper ------------------------------------------------
//
// Groups repeat-of-same-thing alerts within the same day so the feed
// reads as
//   "Counter Unstaffed ×7 today (last: 3:06 PM)"
// instead of seven separate rows for one ongoing condition.
//
// Rule: alerts share a group iff they have the same detection_type,
// the same camera_id, and the same calendar day. The "head" of each
// group is the alert the card shows (see pickGroupHead below); the
// others render in the expand accordion.
//
// `key` identifies the group itself (day|type|camera) and stays the same
// when a newer alert joins, so lists can key cards by it and a growing
// group updates in place instead of being re-created.
//
// Which alert a group card shows (pickGroupHead): the newest OPEN alert;
// once every alert is closed, the most recently MARKED one — so the
// verdict the operator just gave stays on the card instead of the card
// flipping to an older, opposite verdict.

import type { Alert } from '@/api/alerts'

export interface AlertGroup {
  key: string
  head: Alert
  count: number
  last: string
  unresolvedCount: number
  siblings: Alert[]
}

const CLOSED_STATUSES = ['resolved', 'confirmed', 'dismissed']

export function isClosedAlert(alert: Alert): boolean {
  return CLOSED_STATUSES.includes(alert.status)
}

/** What happened to one alert, in operator terms.
 *  /confirm (True) and /resolve both store status "confirmed"; only
 *  /resolve and "Resolve all" set resolved_at, so that tells them apart. */
export type AlertOutcome = 'open' | 'true' | 'false' | 'resolved'

export function alertOutcome(alert: Alert): AlertOutcome {
  if (alert.status === 'dismissed') return 'false'
  if (alert.status === 'resolved') return 'resolved'
  if (alert.status === 'confirmed') return alert.resolved_at ? 'resolved' : 'true'
  return 'open'
}

function timeOf(value: string | null | undefined): number {
  const t = Date.parse(value ?? '')
  return Number.isNaN(t) ? 0 : t
}

/** When the alert was last marked/closed (best available timestamp). */
function markedAt(alert: Alert): number {
  return Math.max(timeOf(alert.resolved_at), timeOf(alert.acknowledged_at),
                  timeOf(alert.created_at))
}

/** The alert a group card shows. `rows` in any order. */
export function pickGroupHead(rows: Alert[]): Alert {
  const newestFirst = [...rows].sort(
    (a, b) => (b.created_at || '').localeCompare(a.created_at || '') || b.id - a.id)
  const open = newestFirst.filter(a => !isClosedAlert(a))
  if (open.length > 0) return open[0]
  return [...newestFirst].sort((a, b) => markedAt(b) - markedAt(a))[0]
}

/** The newest open alert other than `shownId`, if any. */
export function nextOpenAlert(rows: Alert[], shownId: number): Alert | null {
  const open = rows
    .filter(a => a.id !== shownId && !isClosedAlert(a))
    .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || '') || b.id - a.id)
  return open[0] ?? null
}

export interface GroupSummary {
  total: number
  open: number
  markedTrue: number
  markedFalse: number
  resolved: number
}

export function summarizeGroup(rows: Alert[]): GroupSummary {
  const out: GroupSummary = { total: rows.length, open: 0, markedTrue: 0, markedFalse: 0, resolved: 0 }
  for (const a of rows) {
    const outcome = alertOutcome(a)
    if (outcome === 'open') out.open++
    else if (outcome === 'true') out.markedTrue++
    else if (outcome === 'false') out.markedFalse++
    else out.resolved++
  }
  return out
}

/** e.g. "2 alerts: 1 marked True, 1 marked False" (zero parts omitted). */
export function groupSummaryText(s: GroupSummary): string {
  const parts: string[] = []
  if (s.open) parts.push(`${s.open} open`)
  if (s.markedTrue) parts.push(`${s.markedTrue} marked True`)
  if (s.markedFalse) parts.push(`${s.markedFalse} marked False`)
  if (s.resolved) parts.push(`${s.resolved} resolved`)
  const head = `${s.total} alert${s.total === 1 ? '' : 's'}`
  return parts.length ? `${head}: ${parts.join(', ')}` : head
}

const eatDay = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Africa/Nairobi', year: 'numeric', month: '2-digit', day: '2-digit',
})

export function groupAlerts(rows: Alert[]): AlertGroup[] {
  const groups = new Map<string, Alert[]>()
  for (const a of rows) {
    const parsed = new Date(a.created_at || '')
    const day = Number.isNaN(parsed.getTime()) ? (a.created_at || '').slice(0, 10) : eatDay.format(parsed)
    const key = `${day}|${a.detection_type ?? ''}|${a.camera_id ?? 'na'}`
    const arr = groups.get(key) ?? []
    arr.push(a)
    groups.set(key, arr)
  }
  const out: AlertGroup[] = []
  for (const [key, arr] of groups) {
    arr.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))
    const last = arr[0].created_at
    const unresolved = arr.filter(alert => !isClosedAlert(alert))
    // An unresolved occurrence must own the card even when a newer sibling
    // was resolved; otherwise grouping hides work from the operator.
    const head = pickGroupHead(arr)
    out.push({
      key,
      head,
      count: arr.length,
      last,
      unresolvedCount: unresolved.length,
      siblings: arr.filter(alert => alert.id !== head.id),
    })
  }
  out.sort((a, b) => (b.last || '').localeCompare(a.last || ''))
  return out
}
