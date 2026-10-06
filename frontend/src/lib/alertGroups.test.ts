import { describe, expect, it } from 'vitest'

import type { Alert } from '../api/alerts'
import {
  alertOutcome, groupAlerts, groupSummaryText, nextOpenAlert, pickGroupHead, summarizeGroup,
} from './alertGroups'

// Same camera + type + Nairobi day, so all rows land in one group.
function alert(id: number, hhmm: string, extra: Partial<Alert> = {}): Alert {
  return {
    id, status: 'new', detection_type: 'person', camera_id: 1,
    created_at: `2026-10-06T${hhmm}:00+03:00`,
    acknowledged_at: null, resolved_at: null,
    ...extra,
  } as Alert
}

describe('alertOutcome', () => {
  it('tells True, False, Resolved and open apart', () => {
    expect(alertOutcome(alert(1, '10:00'))).toBe('open')
    expect(alertOutcome(alert(1, '10:00', { status: 'dismissed' }))).toBe('false')
    // /confirm and /resolve share status "confirmed"; only /resolve sets resolved_at.
    expect(alertOutcome(alert(1, '10:00', { status: 'confirmed' }))).toBe('true')
    expect(alertOutcome(alert(1, '10:00', {
      status: 'confirmed', resolved_at: '2026-10-06T10:05:00+03:00' }))).toBe('resolved')
    expect(alertOutcome(alert(1, '10:00', { status: 'resolved' }))).toBe('resolved')
  })
})

describe('pickGroupHead', () => {
  it('shows the newest open alert', () => {
    const rows = [alert(1, '10:00'), alert(2, '10:05'), alert(3, '10:10', { status: 'confirmed' })]
    expect(pickGroupHead(rows).id).toBe(2)
  })

  it('once all are closed, shows the most recently marked alert', () => {
    // The acceptance case: the newer alert was already True; the operator
    // then marks the older one False. The card must keep showing the False.
    const newerTrue = alert(2, '10:05', { status: 'confirmed', acknowledged_at: '2026-10-06T10:06:00+03:00' })
    const olderFalse = alert(1, '10:00', { status: 'dismissed', acknowledged_at: '2026-10-06T10:20:00+03:00' })
    expect(pickGroupHead([newerTrue, olderFalse]).id).toBe(1)
  })

  it('falls back to the newest alert when there are no marking times', () => {
    const rows = [alert(1, '10:00', { status: 'dismissed' }), alert(2, '10:05', { status: 'confirmed' })]
    expect(pickGroupHead(rows).id).toBe(2)
  })

  it('is what groupAlerts uses for the card head', () => {
    const groups = groupAlerts([
      alert(2, '10:05', { status: 'confirmed', acknowledged_at: '2026-10-06T10:06:00+03:00' }),
      alert(1, '10:00', { status: 'dismissed', acknowledged_at: '2026-10-06T10:20:00+03:00' }),
    ])
    expect(groups).toHaveLength(1)
    expect(groups[0].head.id).toBe(1)
    expect(groups[0].siblings.map(a => a.id)).toEqual([2])
    expect(groups[0].unresolvedCount).toBe(0)
  })
})

describe('nextOpenAlert', () => {
  it('returns the newest other open alert', () => {
    const rows = [alert(1, '10:00'), alert(2, '10:05'), alert(3, '10:10', { status: 'dismissed' })]
    expect(nextOpenAlert(rows, 3)?.id).toBe(2)
    expect(nextOpenAlert(rows, 2)?.id).toBe(1)
  })

  it('returns null when nothing else is open', () => {
    const rows = [alert(1, '10:00', { status: 'confirmed' }), alert(2, '10:05')]
    expect(nextOpenAlert(rows, 2)).toBeNull()
  })
})

describe('group summary', () => {
  it('reads "2 alerts: 1 marked True, 1 marked False"', () => {
    const rows = [alert(2, '10:05', { status: 'confirmed' }), alert(1, '10:00', { status: 'dismissed' })]
    expect(groupSummaryText(summarizeGroup(rows))).toBe('2 alerts: 1 marked True, 1 marked False')
  })

  it('lists open, True, False and resolved, omitting zeros', () => {
    const rows = [
      alert(1, '10:00'), alert(2, '10:01', { status: 'dismissed' }),
      alert(3, '10:02', { status: 'dismissed' }),
      alert(4, '10:03', { status: 'confirmed', resolved_at: '2026-10-06T10:04:00+03:00' }),
    ]
    expect(summarizeGroup(rows)).toEqual(
      { total: 4, open: 1, markedTrue: 0, markedFalse: 2, resolved: 1 })
    expect(groupSummaryText(summarizeGroup(rows))).toBe('4 alerts: 1 open, 2 marked False, 1 resolved')
  })
})
