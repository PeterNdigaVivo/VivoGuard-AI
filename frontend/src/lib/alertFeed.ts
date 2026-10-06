// Live alert feed — pure list logic for the Alerts page.
//
// The /ws/alerts message only carries an alert id, not the full card
// payload, so the page re-fetches the newest page in the background and
// MERGES it into what is on screen instead of wiping and re-rendering.
// Everything here is side-effect free so it can be unit tested
// (alertFeed.test.ts); the page owns state, timers and scrolling.

import type { Alert } from '@/api/alerts'

/** Newest first: created_at desc, then id desc — the server's
 *  order=recent sort, with the same (created_at, id) tie-break its
 *  before_id cursor uses. */
export function compareNewestFirst(a: Alert, b: Alert): number {
  const byTime = (b.created_at || '').localeCompare(a.created_at || '')
  return byTime !== 0 ? byTime : b.id - a.id
}

/** True when two copies of an alert carry identical data. Unchanged
 *  rows keep their existing object so their cards do not re-render or
 *  reset (AlertCard re-syncs its local copy whenever the prop changes). */
export function sameAlert(a: Alert, b: Alert): boolean {
  return a === b || JSON.stringify(a) === JSON.stringify(b)
}

function dedupeById(rows: Alert[]): Alert[] {
  const seen = new Set<number>()
  return rows.filter(row => (seen.has(row.id) ? false : (seen.add(row.id), true)))
}

/** True when the latest page is full and shares no alert with the list
 *  on screen, and is entirely newer than it: more than one page of
 *  alerts arrived at once, so merging would leave a hole in the middle. */
export function detectGap(current: Alert[], latest: Alert[], pageSize: number): boolean {
  if (current.length === 0 || latest.length < pageSize) return false
  const currentIds = new Set(current.map(a => a.id))
  if (latest.some(a => currentIds.has(a.id))) return false
  const oldestLatest = [...latest].sort(compareNewestFirst)[latest.length - 1]
  const newestCurrent = [...current].sort(compareNewestFirst)[0]
  return compareNewestFirst(newestCurrent, oldestLatest) > 0
}

export interface MergeResult {
  items: Alert[]
  /** Ids in `latest` that were not on screen before. */
  addedIds: number[]
  /** See detectGap: `items` is then just the latest page. */
  gap: boolean
}

/**
 * Merge a freshly fetched first page (`latest`, order=recent) into the
 * list on screen (`current`, possibly extended by "Load more").
 *
 *  - new alerts are added, changed alerts replaced, unchanged alerts keep
 *    their exact object (no card reset);
 *  - an alert inside the latest page's time window that is missing from
 *    it no longer matches the filters (or was removed), so it is dropped;
 *  - alerts older than that window ("Load more" pages) are left alone;
 *  - when the latest page is not full it is the complete result set,
 *    so anything not in it is dropped;
 *  - duplicates are removed and the newest-first order is kept.
 */
export function mergeLatestPage(current: Alert[], latest: Alert[], pageSize: number): MergeResult {
  const fresh = dedupeById(latest).sort(compareNewestFirst)
  const currentById = new Map(current.map(a => [a.id, a]))
  const addedIds = fresh.filter(a => !currentById.has(a.id)).map(a => a.id)

  if (detectGap(current, fresh, pageSize)) {
    return { items: fresh, addedIds, gap: true }
  }

  const freshIds = new Set(fresh.map(a => a.id))
  const full = fresh.length >= pageSize
  const oldestFresh = fresh[fresh.length - 1]
  const merged: Alert[] = fresh.map(row => {
    const previous = currentById.get(row.id)
    return previous && sameAlert(previous, row) ? previous : row
  })
  for (const row of current) {
    if (freshIds.has(row.id)) continue
    // Keep only rows strictly older than the latest page's window.
    if (full && oldestFresh && compareNewestFirst(oldestFresh, row) < 0) merged.push(row)
  }
  return { items: dedupeById(merged).sort(compareNewestFirst), addedIds, gap: false }
}

/**
 * Background-refresh variant used by the page: like mergeLatestPage, but
 * new alerts for which `hold(row)` is true are NOT inserted yet (the
 * operator has scrolled down and would see the list jump). Held alerts
 * stay in the latest page and are inserted by a later call with no hold.
 * A gap while holding leaves the list unchanged until it is released.
 */
export function applyLatestPage(
  current: Alert[], latest: Alert[], pageSize: number,
  hold: (row: Alert) => boolean = () => false,
): Alert[] {
  const result = mergeLatestPage(current, latest, pageSize)
  const heldIds = new Set(
    result.addedIds.filter(id => hold(result.items.find(a => a.id === id)!)))
  if (heldIds.size === 0) return result.items
  if (result.gap) return current
  return result.items.filter(a => !heldIds.has(a.id))
}

/** "Load more": append an older page, dropping alerts already shown. */
export function appendOlderPage(current: Alert[], older: Alert[]): Alert[] {
  const ids = new Set(current.map(a => a.id))
  return [...current, ...dedupeById(older).filter(a => !ids.has(a.id))]
}

// ---- Burst coalescing ------------------------------------------------

export interface RefreshScheduler {
  /** Ask for a refresh soon; several requests within `delayMs` share one. */
  request(): void
  /** Refresh as soon as possible (after an operator action). */
  requestNow(): void
  dispose(): void
}

interface Timers {
  setTimeout: (fn: () => void, ms: number) => unknown
  clearTimeout: (handle: unknown) => void
}

const realTimers: Timers = {
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: handle => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
}

/**
 * Runs `run` at most once per burst: requests arriving within `delayMs`
 * collapse into one refresh, and requests arriving while a refresh is in
 * flight trigger exactly one follow-up refresh after it finishes.
 */
export function createRefreshScheduler(
  run: () => Promise<void>, delayMs: number, timers: Timers = realTimers,
): RefreshScheduler {
  let timer: unknown = null
  let running = false
  let again = false
  let disposed = false

  async function fire() {
    timer = null
    running = true
    try {
      await run()
    } catch (error) {
      // The page reports refresh failures itself; this only catches bugs.
      console.warn('[alerts] background refresh failed', error)
    } finally {
      running = false
      if (again && !disposed) {
        again = false
        request()
      }
    }
  }

  function request() {
    if (disposed) return
    if (running) { again = true; return }
    if (timer === null) timer = timers.setTimeout(() => void fire(), delayMs)
  }

  function requestNow() {
    if (disposed) return
    if (running) { again = true; return }
    if (timer !== null) timers.clearTimeout(timer)
    timer = null
    void fire()
  }

  function dispose() {
    disposed = true
    if (timer !== null) timers.clearTimeout(timer)
    timer = null
  }

  return { request, requestNow, dispose }
}

// ---- Verdicts without a reload ----------------------------------------

/** Detail of the window 'vg:alert-resolved' event a card fires after the
 *  operator marks / closes an alert. `patch` is what the server stored,
 *  so lists can update that one row instead of re-fetching everything. */
export interface AlertResolvedDetail {
  id?: number
  action?: 'resolve' | 'dismiss'
  bulk?: number
  patch?: Partial<Pick<Alert, 'status' | 'acknowledged_at' | 'resolved_at'>>
}

/** Apply a patch to one alert; returns `rows` itself when nothing changes. */
export function patchAlert(rows: Alert[], id: number, patch: Partial<Alert>): Alert[] {
  const index = rows.findIndex(a => a.id === id)
  if (index < 0) return rows
  const next = rows.slice()
  next[index] = { ...rows[index], ...patch }
  return next
}
