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
//
// `key` identifies the group itself (day|type|camera) and stays the same
// when a newer alert joins, so lists can key cards by it and a growing
// group updates in place instead of being re-created.

import type { Alert } from '@/api/alerts'

export interface AlertGroup {
  key: string
  head: Alert
  count: number
  last: string
  unresolvedCount: number
  siblings: Alert[]
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
  const closed = (alert: Alert) => ['resolved', 'confirmed', 'dismissed'].includes(alert.status)
  const out: AlertGroup[] = []
  for (const [key, arr] of groups) {
    arr.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))
    const last = arr[0].created_at
    const unresolved = arr.filter(alert => !closed(alert))
    // An unresolved occurrence must own the card even when a newer sibling
    // was resolved; otherwise grouping hides work from the operator.
    const head = unresolved[0] ?? arr[0]
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
