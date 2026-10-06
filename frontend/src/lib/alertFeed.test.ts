import { describe, expect, it, vi } from 'vitest'

import type { Alert } from '../api/alerts'
import { groupAlerts } from './alertGroups'
import {
  appendOlderPage, applyLatestPage, createRefreshScheduler, detectGap, mergeLatestPage,
  patchAlert, summarizeHeld,
} from './alertFeed'

// Minimal alert rows: only the fields the feed logic and grouping read.
// `t` = seconds after 06:00 UTC (09:00 Nairobi), so every test row falls
// on the same Nairobi day and larger t means newer.
function alert(id: number, t: number, extra: Partial<Alert> = {}): Alert {
  return {
    id, status: 'new', detection_type: 'person', camera_id: 1,
    created_at: new Date(Date.UTC(2026, 9, 6, 6, 0, 0) + t * 1000).toISOString(),
    ...extra,
  } as Alert
}

/** n alerts, newest first: ids `start`..`start-n+1` at times `t0`..`t0-n+1`. */
function page(start: number, n: number, t0 = 10_000): Alert[] {
  return Array.from({ length: n }, (_, i) => alert(start - i, t0 - i))
}

describe('mergeLatestPage', () => {
  it('adds a new alert at the top and reports it', () => {
    const current = [alert(2, 10), alert(1, 5)]
    const { items, addedIds, gap } = mergeLatestPage(current, [alert(3, 20), ...current], 100)
    expect(items.map(a => a.id)).toEqual([3, 2, 1])
    expect(addedIds).toEqual([3])
    expect(gap).toBe(false)
  })

  it('removes duplicates in the fetched page', () => {
    const a = alert(3, 20)
    const { items } = mergeLatestPage([], [a, { ...a }, alert(2, 10)], 100)
    expect(items.map(x => x.id)).toEqual([3, 2])
  })

  it('keeps the same object for unchanged alerts so their cards do not reset', () => {
    const kept = alert(1, 5)
    const { items } = mergeLatestPage([kept], [alert(2, 10), { ...kept }], 100)
    expect(items[1]).toBe(kept)
  })

  it('replaces an alert whose status changed, and only that one', () => {
    const a = alert(2, 10)
    const b = alert(1, 5)
    const { items } = mergeLatestPage([a, b], [{ ...a, status: 'dismissed' }, { ...b }], 100)
    expect(items[0].status).toBe('dismissed')
    expect(items[0]).not.toBe(a)
    expect(items[1]).toBe(b)
  })

  it('keeps "load more" pages older than the refreshed window', () => {
    const first = page(300, 100, 10_000)      // ids 300..201
    const older = page(200, 100, 9_000)       // ids 200..101, loaded with "Load more"
    const current = [...first, ...older]
    const latest = [alert(301, 10_001), ...first.slice(0, 99)]   // full page, 201 pushed out
    const { items } = mergeLatestPage(current, latest, 100)
    expect(items).toHaveLength(201)
    expect(items[0].id).toBe(301)
    expect(items.some(a => a.id === 201)).toBe(true)   // still on screen
    expect(items.some(a => a.id === 101)).toBe(true)
  })

  it('drops an alert inside the window that no longer matches the filters', () => {
    const current = [alert(3, 20), alert(2, 10), alert(1, 5)]
    const { items } = mergeLatestPage(current, [alert(3, 20), alert(1, 5)], 100)
    expect(items.map(a => a.id)).toEqual([3, 1])
  })

  it('detects a burst bigger than one page and shows the newest page', () => {
    const current = [alert(2, 1), alert(1, 0)]
    const latest = page(500, 100)
    expect(detectGap(current, latest, 100)).toBe(true)
    const { items, gap } = mergeLatestPage(current, latest, 100)
    expect(gap).toBe(true)
    expect(items.map(a => a.id)).toEqual(latest.map(a => a.id))
  })

  it('does not report a gap when the pages overlap', () => {
    const current2 = page(150, 100, 1_000)
    const latest = [alert(151, 1_001), ...current2.slice(0, 99)]
    expect(detectGap(current2, latest, 100)).toBe(false)
  })
})

describe('applyLatestPage (operator scrolled down)', () => {
  it('holds back new alerts that would move the list', () => {
    const current = [alert(2, 10), alert(1, 5)]
    const latest = [alert(4, 30), alert(3, 20), ...current]
    const items = applyLatestPage(current, latest, 100, row => row.id === 4)
    expect(items.map(a => a.id)).toEqual([3, 2, 1])   // 3 is not visible, inserted now
  })

  it('still applies status changes to alerts already shown', () => {
    const current = [alert(1, 5)]
    const latest = [alert(2, 10), { ...current[0], status: 'confirmed' }]
    const items = applyLatestPage(current, latest, 100, () => true)
    expect(items.map(a => [a.id, a.status])).toEqual([[1, 'confirmed']])
  })

  it('inserts held alerts once released', () => {
    const current = [alert(1, 5)]
    const latest = [alert(2, 10), alert(1, 5)]
    const held = applyLatestPage(current, latest, 100, () => true)
    expect(applyLatestPage(held, latest, 100).map(a => a.id)).toEqual([2, 1])
  })

  it('leaves the list alone on a gap until released', () => {
    const current = [alert(1, 0)]
    expect(applyLatestPage(current, page(500, 100), 100, () => true)).toBe(current)
  })
})

describe('appendOlderPage', () => {
  it('appends older alerts without duplicates', () => {
    const items = appendOlderPage([alert(3, 20), alert(2, 10)], [alert(2, 10), alert(1, 5)])
    expect(items.map(a => a.id)).toEqual([3, 2, 1])
  })
})

describe('groupAlerts keys', () => {
  it('keeps the group key when a new alert joins, and raises the count', () => {
    const before = groupAlerts([alert(2, 10), alert(1, 5)])
    const after = groupAlerts([alert(3, 20), alert(2, 10), alert(1, 5)])
    expect(before).toHaveLength(1)
    expect(after).toHaveLength(1)
    expect(after[0].key).toBe(before[0].key)
    expect(after[0].count).toBe(3)
    expect(after[0].head.id).toBe(3)
  })

  it('gives different cameras different groups', () => {
    const groups = groupAlerts([alert(2, 10, { camera_id: 2 }), alert(1, 5)])
    expect(new Set(groups.map(g => g.key)).size).toBe(2)
  })
})

describe('createRefreshScheduler', () => {
  it('collapses a burst into one refresh', async () => {
    vi.useFakeTimers()
    const run = vi.fn(async () => {})
    const s = createRefreshScheduler(run, 1000)
    s.request(); s.request(); s.request()
    expect(run).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1000)
    expect(run).toHaveBeenCalledTimes(1)
    s.dispose()
    vi.useRealTimers()
  })

  it('runs exactly one follow-up for events during a refresh', async () => {
    vi.useFakeTimers()
    let finish: () => void = () => {}
    const run = vi.fn(() => new Promise<void>(resolve => { finish = resolve }))
    const s = createRefreshScheduler(run, 1000)
    s.requestNow()
    expect(run).toHaveBeenCalledTimes(1)
    s.request(); s.request(); s.requestNow()       // all while in flight
    finish()
    await vi.advanceTimersByTimeAsync(1000)
    expect(run).toHaveBeenCalledTimes(2)
    finish()
    await vi.advanceTimersByTimeAsync(5000)
    expect(run).toHaveBeenCalledTimes(2)
    s.dispose()
    vi.useRealTimers()
  })

  it('logs and recovers when a refresh throws', async () => {
    vi.useFakeTimers()
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const run = vi.fn(async () => { throw new Error('boom') })
    const s = createRefreshScheduler(run, 10)
    s.requestNow()
    await vi.advanceTimersByTimeAsync(0)
    expect(warn).toHaveBeenCalled()
    s.request()
    await vi.advanceTimersByTimeAsync(10)
    expect(run).toHaveBeenCalledTimes(2)
    warn.mockRestore()
    s.dispose()
    vi.useRealTimers()
  })

  it('does nothing after dispose', async () => {
    vi.useFakeTimers()
    const run = vi.fn(async () => {})
    const s = createRefreshScheduler(run, 1000)
    s.request()
    s.dispose()
    await vi.advanceTimersByTimeAsync(2000)
    expect(run).not.toHaveBeenCalled()
    vi.useRealTimers()
  })
})

describe('patchAlert', () => {
  it('updates one row and leaves the others as the same objects', () => {
    const rows = [alert(2, 10), alert(1, 5)]
    const next = patchAlert(rows, 1, { status: 'dismissed' })
    expect(next[1].status).toBe('dismissed')
    expect(next[0]).toBe(rows[0])
    expect(rows[1].status).toBe('new')            // original untouched
  })

  it('returns the same array when the id is not shown', () => {
    const rows = [alert(1, 5)]
    expect(patchAlert(rows, 99, { status: 'dismissed' })).toBe(rows)
  })
})

describe('summarizeHeld (the "↑ N new alerts" button)', () => {
  it('counts waiting alerts in the normal colour', () => {
    const s = summarizeHeld([alert(2, 10), alert(1, 5)])
    expect(s).toMatchObject({ count: 2, urgent: 0, tone: 'normal', label: '2 new alerts' })
    expect(s.announcement).toBe('2 new alerts waiting. Use the new alerts button at the top to show them.')
  })

  it('turns urgent when any waiting alert is urgent or critical', () => {
    const s = summarizeHeld([
      alert(3, 20, { severity_label: 'URGENT' }), alert(2, 10, { severity: 'critical' }), alert(1, 5),
    ])
    expect(s).toMatchObject({ count: 3, urgent: 2, tone: 'urgent', label: '3 new alerts · 2 urgent',
                              shortLabel: '3 new · 2 urgent' })
    expect(s.announcement).toContain('3 new alerts waiting, 2 urgent.')
  })

  it('uses the singular for one alert and says nothing for none', () => {
    expect(summarizeHeld([alert(1, 5, { severity_label: 'URGENT' })]).label).toBe('1 new alert · 1 urgent')
    expect(summarizeHeld([]).announcement).toBe('')
  })
})
