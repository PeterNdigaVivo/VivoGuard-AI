// Alerts page — chain-wide feed using the shared AlertCard.
// May-2026 redesign: same card component as the per-store dashboard
// feed so titles, severity colours, and action buttons match exactly.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Card, PageHeader } from '@/components/ui/Primitives'
import DateRangePicker, { rangeFor, type DateRange } from '@/components/DateRangePicker'
import { alerts as alertsApi, type Alert } from '@/api/alerts'
import { api } from '@/api/client'
import { AlertCard, groupAlerts } from '@/components/AlertCard'
import { stores as storesApi, type Store } from '@/api/stores'
import {
  appendOlderPage, applyLatestPage, createRefreshScheduler, detectGap, patchAlert,
  summarizeHeld, type AlertResolvedDetail, type RefreshScheduler,
} from '@/lib/alertFeed'

// Simple quick-filter buttons non-technical staff understand.
type Quick = 'store' | 'positive' | 'urgent' | 'attention' | 'calibration' | 'resolved' | 'all'

// store_intelligence has its own "Store Update" tab and is kept OUT of the
// actionable tabs (urgent / attention / resolved / all). system_health never
// arrives here at all — the API drops every subtype of it (see
// _operator_alert_filter); it lives on the System Health page instead.
const STORE_INTEL_TYPE = 'store_intelligence'
const POSITIVE_TYPE = 'positive_operational'
const _isStoreIntel = (a: Alert) => a.detection_type === STORE_INTEL_TYPE
const _isPositive = (a: Alert) => a.detection_type === POSITIVE_TYPE
const _isActionable = (a: Alert) => !_isStoreIntel(a) && !_isPositive(a)
const _isCalibration = (a: Alert) => a.review_only || a.notification_suppressed
const _isOperational = (a: Alert) => _isActionable(a) && !_isCalibration(a)
const _isOpen = (a: Alert) => !['resolved', 'confirmed', 'dismissed'].includes(a.status)
const PAGE_SIZE = 100

// Live feed tuning. Alerts arriving within REFRESH_BURST_MS share one
// background refresh; new cards open out over ~300 ms (CSS) and glow for
// FRESH_GLOW_MS. Past SCROLLED_DOWN_PX the operator is reading further
// down, so new alerts wait behind the "↑ N new alerts" button.
const REFRESH_BURST_MS = 1000
const FRESH_ENTER_MS = 400
const FRESH_GLOW_MS = 4000
const SCROLLED_DOWN_PX = 40

// Client-side quick-filter + search. Shared by the list and by the live
// feed's "would this new alert be visible?" check. The Actionable tabs
// exclude store intelligence, which has its own tab.
function matchesView(a: Alert, quick: Quick, search: string): boolean {
  if (quick === 'store') {
    if (!_isStoreIntel(a)) return false
  } else if (quick === 'positive') {
    if (!_isPositive(a)) return false
  } else {
    if (quick === 'calibration') {
      if (!(_isActionable(a) && _isCalibration(a))) return false
    } else if (!_isOperational(a)) {
      // Operational tabs exclude quarantined/review-only evidence. Those
      // records remain available in the dedicated Calibration tab.
      return false
    }
    if (quick === 'urgent' && !(a.severity_label === 'URGENT' && _isOpen(a))) return false
    if (quick === 'attention' && !(a.severity_label === 'ATTENTION' && _isOpen(a))) return false
    // 'confirmed' covers alerts the /resolve endpoint flipped (it re-uses
    // that bucket as a "handled" state). 'dismissed' covers "Not a problem".
    if (quick === 'resolved' && !['resolved', 'confirmed', 'dismissed'].includes(a.status)) return false
  }
  const q = search.trim().toLowerCase()
  if (q) {
    return (a.plain_title ?? a.title ?? '').toLowerCase().includes(q) ||
      (a.camera_name ?? '').toLowerCase().includes(q) ||
      (a.detection_type ?? '').toLowerCase().includes(q)
  }
  return true
}

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined'
    && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true
}

interface ProofOfLife {
  now: string
  state: 'active' | 'degraded' | 'offline'
  latest_detection_age_seconds: number | null
  latest_detection_type: string | null
  latest_detection_is_alert: boolean
  latest_detection_disposition: 'alert' | 'filtered' | 'metric_only' | null
  latest_alert_age_seconds: number | null
  pipeline_age_seconds: number | null
  cameras_total: number
  cameras_fresh: number
  cameras_actively_inferencing: number | null
  cameras_waiting_for_worker: number | null
  inference_queue_depth: number | null
  estimated_full_rotation_seconds: number | null
}

function ageLabel(seconds: number | null): string {
  if (seconds == null) return 'unknown'
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  return `${Math.floor(seconds / 3600)}h ago`
}

function detectionLabel(proof: ProofOfLife): string {
  const kind = proof.latest_detection_type?.replaceAll('_', ' ') ?? 'unknown'
  if (proof.latest_detection_is_alert) return `${kind} · alerted`
  if (proof.latest_detection_disposition === 'metric_only') return `${kind} · metric only`
  if (proof.latest_detection_disposition === 'filtered') return `${kind} · filtered`
  return `${kind} · not alerted`
}

export default function AlertsPage() {
  const [items, setItems] = useState<Alert[]>([])
  const [range, setRange] = useState<DateRange>(() => rangeFor('today'))
  const [quick, setQuick] = useState<Quick>('all')
  const [storeId, setStoreId] = useState<string>('')
  // AI verdict filter (annotate-only): '' = All, never hides by default.
  const [aiVerdict, setAiVerdict] = useState<string>('')
  const [search, setSearch] = useState('')
  const [stores, setStores] = useState<Store[]>([])
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [summaryError, setSummaryError] = useState<string | null>(null)
  const [summaryLoading, setSummaryLoading] = useState(true)
  const [hasMore, setHasMore] = useState(false)
  const [proof, setProof] = useState<ProofOfLife | null>(null)
  const requestSequence = useRef(0)
  const [summary, setSummary] = useState({
    urgent: 0, attention: 0,
    resolved_today: 0, dismissed_today: 0,
    unread_urgent: 0,
    critical_today: 0, high_today: 0, medium_today: 0, low_today: 0,
    calibration_today: 0, operational_today_count: 0,
    ai_true_today: 0, ai_false_today: 0,
    ai_uncertain_today: 0, ai_pending_today: 0,
    ai_verifier_enabled: false,
    avg_response_seconds: null as number | null,
    today_count: 0, yesterday_count: 0,
    trend_vs_yesterday_pct: null as number | null,
    date_label: null as string | null,
  })
  // Bottom-right toast for the "resolved" confirmation. Auto-clears
  // after 4s. Use this for any user-facing success/error message
  // instead of window.alert() so the operator's flow isn't blocked.
  const [toast, setToast] = useState<string | null>(null)
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 4000)
    return () => clearTimeout(t)
  }, [toast])

  useEffect(() => { storesApi.list().then(setStores).catch(() => {}) }, [])

  useEffect(() => {
    const load = () => api<ProofOfLife>('/system/proof-of-life')
      .then(setProof).catch(() => setProof(null))
    load()
    const timer = setInterval(load, 60_000)
    return () => clearInterval(timer)
  }, [])

  // ---- Live feed state ---------------------------------------------
  // `epoch` changes on every first/filter load so a background refresh
  // started under old filters can never land in the new list.
  const rootRef = useRef<HTMLDivElement>(null)
  const epochRef = useRef(0)
  const loadingRef = useRef(true)
  const itemsRef = useRef<Alert[]>([])
  itemsRef.current = items
  // Newest page from the last background refresh, and whether its new
  // alerts are being held back because the operator scrolled down.
  const [latest, setLatest] = useState<Alert[] | null>(null)
  const [holding, setHolding] = useState(false)
  const holdingRef = useRef(false)
  holdingRef.current = holding
  const [refreshError, setRefreshError] = useState<string | null>(null)
  const [gapNotice, setGapNotice] = useState(false)
  // Ids of just-inserted alerts: opening animation, then a fading glow.
  const [enterIds, setEnterIds] = useState<ReadonlySet<number>>(new Set())
  const [glowIds, setGlowIds] = useState<ReadonlySet<number>>(new Set())
  const animateNextRef = useRef(false)
  const prevIdsRef = useRef<Set<number>>(new Set())
  const freshTimers = useRef<Set<ReturnType<typeof setTimeout>>>(new Set())
  const viewRef = useRef<(a: Alert) => boolean>(() => true)
  viewRef.current = (a: Alert) => matchesView(a, quick, search)

  const loadPage = useCallback(async (append = false, beforeId?: number) => {
    const sequence = ++requestSequence.current
    if (append) setLoadingMore(true)
    else {
      // First load or a filter change: the only time the list is emptied
      // and "Loading alerts…" shown. Reset everything the live feed holds.
      epochRef.current += 1
      loadingRef.current = true
      setLoading(true)
      setItems([])
      setLatest(null)
      setHolding(false)
      setRefreshError(null)
      setGapNotice(false)
      setEnterIds(new Set())
      setGlowIds(new Set())
    }
    setLoadError(null)
    try {
      const page = await alertsApi.list({
        store_id: storeId || undefined,
        ai_verdict: aiVerdict || undefined,
        since: range.since,
        until: range.until,
        limit: PAGE_SIZE,
        order: 'recent',
        before_id: beforeId,
      })
      if (sequence !== requestSequence.current) return
      setItems(previous => append ? appendOlderPage(previous, page) : page)
      setHasMore(page.length === PAGE_SIZE)
    } catch (error) {
      if (sequence !== requestSequence.current) return
      setLoadError(error instanceof Error ? error.message : String(error))
      if (!append) setHasMore(false)
    } finally {
      if (sequence === requestSequence.current) {
        loadingRef.current = false
        setLoading(false)
        setLoadingMore(false)
      }
    }
  }, [storeId, aiVerdict, range.since, range.until])

  const reload = useCallback(() => { void loadPage(false) }, [loadPage])

  useEffect(() => {
    reload()
    setSummaryError(null)
    setSummaryLoading(true)
    alertsApi.summary(storeId ? Number(storeId) : undefined)
      .then(setSummary)
      .catch(error => setSummaryError(error instanceof Error ? error.message : String(error)))
      .finally(() => setSummaryLoading(false))
  }, [reload, storeId])

  // ---- Background refresh -----------------------------------------
  // Never empties the list: fetches the newest page (same filters) and
  // merges it into what is on screen (lib/alertFeed.ts), so open
  // snapshots, half-typed notes and "Load more" pages survive. The header
  // summary is refreshed alongside, quietly (no "Refreshing…" card).
  const scrollContainer = useCallback((): HTMLElement | null => {
    return (rootRef.current?.closest('main') as HTMLElement | null)
      ?? (document.scrollingElement as HTMLElement | null)
  }, [])
  const isScrolledDown = useCallback(
    () => (scrollContainer()?.scrollTop ?? 0) > SCROLLED_DOWN_PX, [scrollContainer])

  const refreshQuietly = useCallback(async () => {
    // A first/filter load already in flight will bring fresh data itself.
    if (loadingRef.current) return
    const epoch = epochRef.current
    const summaryRequest = alertsApi.summary(storeId ? Number(storeId) : undefined)
      .then(next => {
        if (epoch !== epochRef.current) return
        setSummary(next)
        setSummaryError(null)
      })
      .catch(error => console.warn('[alerts] summary refresh failed; keeping the last counts', error))
    try {
      const page = await alertsApi.list({
        store_id: storeId || undefined,
        ai_verdict: aiVerdict || undefined,
        since: range.since,
        until: range.until,
        limit: PAGE_SIZE,
        order: 'recent',
      })
      if (epoch !== epochRef.current) return
      const hold = isScrolledDown()
      if (!hold && detectGap(itemsRef.current, page, PAGE_SIZE)) {
        setGapNotice(true)
        setHasMore(true)
      }
      animateNextRef.current = true
      setLatest(page)
      setHolding(hold)
      setItems(current => applyLatestPage(
        current, page, PAGE_SIZE, hold ? row => viewRef.current(row) : undefined))
      setRefreshError(null)
    } catch (error) {
      if (epoch !== epochRef.current) return
      console.warn('[alerts] background refresh failed; keeping the current list', error)
      setRefreshError(error instanceof Error ? error.message : String(error))
    }
    await summaryRequest
  }, [storeId, aiVerdict, range.since, range.until, isScrolledDown])

  // One live-feed connection for the page's lifetime. Bursts of alerts
  // collapse into one refresh; the scheduler always calls the CURRENT
  // refresh function (filters may have changed since it was created).
  const refreshRef = useRef(refreshQuietly)
  refreshRef.current = refreshQuietly
  const schedulerRef = useRef<RefreshScheduler | null>(null)
  useEffect(() => {
    const scheduler = createRefreshScheduler(() => refreshRef.current(), REFRESH_BURST_MS)
    schedulerRef.current = scheduler
    const unsubscribe = alertsApi.subscribe(() => scheduler.request())
    return () => {
      unsubscribe()
      scheduler.dispose()
      schedulerRef.current = null
    }
  }, [])
  // After an operator action (True/False, resolve, note): refresh now.
  const refreshNow = useCallback(() => { schedulerRef.current?.requestNow() }, [])

  // New alerts held while scrolled down, under the current tab + search.
  const held = useMemo(() => {
    if (!holding || !latest) return []
    const shown = new Set(items.map(a => a.id))
    return latest.filter(a => !shown.has(a.id) && matchesView(a, quick, search))
  }, [holding, latest, items, quick, search])

  // The "↑ N new alerts" button: what is waiting, a short pulse when the
  // number goes up, and a polite screen-reader announcement.
  const heldSummary = useMemo(() => summarizeHeld(held), [held])
  const [heldPulse, setHeldPulse] = useState(false)
  const [heldAnnouncement, setHeldAnnouncement] = useState('')
  const prevHeldCount = useRef(0)
  useEffect(() => {
    const prev = prevHeldCount.current
    prevHeldCount.current = heldSummary.count
    if (heldSummary.count === 0) { setHeldAnnouncement(''); return }
    if (heldSummary.count <= prev) return
    setHeldAnnouncement(heldSummary.announcement)
    if (prev === 0) return            // first appearance slides in instead
    setHeldPulse(true)
    const timer = setTimeout(() => setHeldPulse(false), 700)
    return () => clearTimeout(timer)
  }, [heldSummary])

  // Insert the held alerts (the button, or scrolling back to the top).
  const releaseHeld = useCallback(() => {
    if (!latest) return
    if (detectGap(itemsRef.current, latest, PAGE_SIZE)) {
      setGapNotice(true)
      setHasMore(true)
    }
    animateNextRef.current = true
    setHolding(false)
    setItems(current => applyLatestPage(current, latest, PAGE_SIZE))
  }, [latest])
  const releaseRef = useRef(releaseHeld)
  releaseRef.current = releaseHeld

  function showHeld() {
    scrollContainer()?.scrollTo({ top: 0, behavior: prefersReducedMotion() ? 'auto' : 'smooth' })
    releaseHeld()
  }

  // Scrolling back up to the top by hand releases held alerts too.
  useEffect(() => {
    const container = scrollContainer()
    if (!container) return
    const target: HTMLElement | Window =
      container === document.scrollingElement ? window : container
    const onScroll = () => {
      if (holdingRef.current && container.scrollTop <= SCROLLED_DOWN_PX) releaseRef.current()
    }
    target.addEventListener('scroll', onScroll, { passive: true })
    return () => target.removeEventListener('scroll', onScroll)
  }, [scrollContainer])

  // Mark alerts inserted by the live feed (not first loads or "Load
  // more") so their cards animate in and glow. A gap replacement brings
  // a whole page at once; animating that would be noise, so it is skipped.
  useEffect(() => {
    const ids = new Set(items.map(a => a.id))
    if (animateNextRef.current) {
      animateNextRef.current = false
      const added = items.filter(a => !prevIdsRef.current.has(a.id)).map(a => a.id)
      if (added.length > 0 && added.length <= PAGE_SIZE / 2) {
        setEnterIds(s => new Set([...s, ...added]))
        setGlowIds(s => new Set([...s, ...added]))
        const drop = (setter: typeof setEnterIds, ms: number) => {
          const timer = setTimeout(() => {
            freshTimers.current.delete(timer)
            setter(s => new Set([...s].filter(id => !added.includes(id))))
          }, ms)
          freshTimers.current.add(timer)
        }
        drop(setEnterIds, FRESH_ENTER_MS)
        drop(setGlowIds, FRESH_GLOW_MS)
      }
    }
    prevIdsRef.current = ids
  }, [items])
  useEffect(() => () => {
    for (const timer of freshTimers.current) clearTimeout(timer)
  }, [])

  // Instant-feedback: any time a card fires the resolve/dismiss
  // event, locally decrement the urgent/attention count so the header
  // tally + sidebar badge update without waiting for the next poll.
  // The /summary fetch fired by reload() then reconciles the truth.
  useEffect(() => {
    function onResolved(e: Event) {
      const detail: AlertResolvedDetail = (e as CustomEvent).detail || {}
      const id = detail.id
      // Update just this row (no list reload after a verdict); the next
      // live-feed refresh reconciles anything else the server changed.
      if (id != null && detail.patch) {
        const patch = detail.patch
        setItems(rows => patchAlert(rows, id, patch))
      }
      const action: 'resolve' | 'dismiss' = detail.action === 'dismiss' ? 'dismiss' : 'resolve'
      const closed = items.find(a => a.id === id)
      if (!closed) return
      if (_isCalibration(closed)) return
      setSummary(s => ({
        ...s,
        urgent:    closed.severity_label === 'URGENT'    ? Math.max(0, s.urgent - 1)    : s.urgent,
        attention: closed.severity_label === 'ATTENTION' ? Math.max(0, s.attention - 1) : s.attention,
        unread_urgent: closed.severity_label === 'URGENT' && closed.status === 'new'
          ? Math.max(0, s.unread_urgent - 1) : s.unread_urgent,
        // Split resolved vs dismissed so the header reflects what the
        // operator actually clicked.
        resolved_today:  action === 'resolve' ? s.resolved_today + 1  : s.resolved_today,
        dismissed_today: action === 'dismiss' ? s.dismissed_today + 1 : s.dismissed_today,
      }))
    }
    window.addEventListener('vg:alert-resolved', onResolved)
    return () => window.removeEventListener('vg:alert-resolved', onResolved)
  }, [items])

  // Client-side quick-filter + search over the loaded window.
  const filtered = useMemo(
    () => items.filter(a => matchesView(a, quick, search)), [items, quick, search])

  // Per-bucket counts derived from the loaded items, so each filter
  // button's badge equals what the user will actually see when they
  // click it. Store intelligence has its own Store Update tab.
  const counts = useMemo(() => {
    const operational = items.filter(_isOperational)
    return {
      // Store Update badge = unread (new) store_intelligence updates.
      store:     items.filter(a => _isStoreIntel(a) && _isOpen(a)).length,
      positive:  items.filter(_isPositive).length,
      urgent:    operational.filter(a => a.severity_label === 'URGENT' && _isOpen(a)).length,
      attention: operational.filter(a => a.severity_label === 'ATTENTION' && _isOpen(a)).length,
      calibration: items.filter(a => _isActionable(a) && _isCalibration(a)).length,
      resolved:  operational.filter(a => ['resolved', 'confirmed', 'dismissed'].includes(a.status)).length,
      all:       operational.length,
    }
  }, [items])

  const groups = groupAlerts(filtered)
  const rawTotal = range.key === 'today' ? summary.today_count : null
  const countsUnavailable = Boolean(loadError && items.length === 0)
  const shownCount = (count: number) => countsUnavailable ? '—' : String(count)

  // Excel export — fetch with the bearer header (an <a href> can't
  // carry it) and trigger a download of the returned .xlsx blob.
  async function resolveAll() {
    // Use whatever filter the user can see — store + date window —
    // so the bulk action mirrors the visible list, not the whole DB.
    const newCount = items.filter(a => a.status === 'new' && _isOperational(a)).length
    if (newCount === 0) { setToast('There are no unresolved alerts to clear.'); return }
    const label = storeId
      ? stores.find(s => String(s.id) === storeId)?.name ?? 'this store'
      : 'all stores'
    if (!confirm(`Mark ${newCount} unresolved alert${newCount === 1 ? '' : 's'} `
                 + `for ${label} (${range.label.toLowerCase()}) as resolved?`)) return
    try {
      const { resolved } = await alertsApi.resolveAll({
        store_id: storeId || undefined,
        since: range.since, until: range.until,
      })
      // Toast with the count from the server — never lies about what
      // actually flipped.
      setToast(`✅ ${resolved} alert${resolved === 1 ? '' : 's'} resolved successfully`)
      // Decrement summary immediately so the page header reflects it
      // before the network reload returns.
      setSummary(s => ({
        ...s,
        urgent: 0, attention: 0, unread_urgent: 0,
        resolved_today: s.resolved_today + resolved,
      }))
      window.dispatchEvent(new CustomEvent('vg:alert-resolved', { detail: { bulk: resolved } }))
      refreshNow()
    } catch (e) {
      setToast(`Could not resolve: ${e}`)
    }
  }

  async function exportExcel() {
    const q = new URLSearchParams()
    if (storeId) q.set('store_id', storeId)
    q.set('since', range.since); q.set('until', range.until)
    const tok = localStorage.getItem('vg_access_token') ?? ''
    const res = await fetch(`/api/alerts/export.xlsx?${q}`, {
      headers: { Authorization: `Bearer ${tok}` },
    })
    if (!res.ok) { alert('Export failed — rebuild the api container if this persists.'); return }
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `vivoguard_alerts_${range.key}.xlsx`
    document.body.appendChild(a); a.click(); document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  return (
    <div className="p-6" ref={rootRef}>
      <PageHeader title="Alerts" actions={
        <div className="flex items-center gap-2">
          <DateRangePicker value={range} onChange={setRange} />
          <button onClick={resolveAll}
                  className="px-3 py-1.5 rounded bg-slate-700 text-white text-sm hover:bg-slate-600">
            ✅ Resolve all
          </button>
          <button onClick={exportExcel}
                  className="px-3 py-1.5 rounded bg-emerald-600 text-white text-sm hover:bg-emerald-500">
            📊 Export to Excel
          </button>
        </div>
      } />

      {proof && <ProofOfLifeCard proof={proof} />}

      {/* Executive summary bar — spec Part 1 §3. Four-tier severity
          counts + resolved tally + avg-time-to-resolve + vs-yesterday
          trend in one glanceable strip. */}
      {summaryLoading ? (
        <Card className="p-3 mb-3 text-sm text-slate-500 dark:text-slate-300">
          Refreshing today’s alert counts…
        </Card>
      ) : summaryError ? (
        <Card className="p-3 mb-3 text-sm text-red-800 bg-red-50 border-red-200">
          <div role="alert">
            <strong>Alert counts are unavailable — this does not mean zero alerts.</strong>
            <div className="text-xs mt-1 break-words">{summaryError}</div>
          </div>
        </Card>
      ) : (
        <ExecutiveSummaryBar summary={summary} />
      )}

      {/* Legacy compact tally — kept beneath the bar for the operators
          who still scan for it. Drops once everyone's adopted the new
          card-style bar above. */}
      {!summaryLoading && !summaryError && (
        <div className="text-xs text-slate-500 dark:text-slate-300 mb-3">
          Today: <strong className="text-red-600">{summary.urgent} urgent</strong>
          {' · '}<strong className="text-amber-600">{summary.attention} need attention</strong>
          {' · '}<strong className="text-emerald-600">{summary.resolved_today} resolved</strong>
          {' · '}<strong className="text-slate-600 dark:text-slate-300">{summary.dismissed_today} dismissed</strong>
          {' · '}<strong className="text-violet-700">{summary.calibration_today} calibration-only</strong>
        </div>
      )}

      {/* Simple filter bar. Each button shows a live count so the
          operator sees at a glance how much is in each bucket. */}
      <Card className="p-3 mb-4 flex flex-wrap gap-2 items-center">
        <QuickBtn active={quick === 'store'}     onClick={() => setQuick('store')}
                  tone="teal">
          🏪 Store Update ({shownCount(counts.store)})
        </QuickBtn>
        <QuickBtn active={quick === 'positive'} onClick={() => setQuick('positive')}
                  tone="green">
          ✅ Positive ({shownCount(counts.positive)})
        </QuickBtn>
        <QuickBtn active={quick === 'urgent'}    onClick={() => setQuick('urgent')}>
          🔴 Urgent ({shownCount(counts.urgent)})
        </QuickBtn>
        <QuickBtn active={quick === 'attention'} onClick={() => setQuick('attention')}>
          🟡 Needs Attention ({shownCount(counts.attention)})
        </QuickBtn>
        <QuickBtn active={quick === 'calibration'} onClick={() => setQuick('calibration')}>
          🧪 Calibration ({shownCount(counts.calibration)})
        </QuickBtn>
        <QuickBtn active={quick === 'resolved'}  onClick={() => setQuick('resolved')}>
          ✅ Resolved ({shownCount(counts.resolved)})
        </QuickBtn>
        <QuickBtn active={quick === 'all'}       onClick={() => setQuick('all')}>
          📋 All ({shownCount(counts.all)})
        </QuickBtn>

        <select className="border rounded px-2 py-1 text-sm ml-2"
                value={storeId} onChange={e => setStoreId(e.target.value)}>
          <option value="">All Stores</option>
          {stores.map(s => <option key={s.id} value={String(s.id)}>{s.name}</option>)}
        </select>

        <select className="border rounded px-2 py-1 text-sm"
                title="AI verification filter (annotate-only; All shows every alert)"
                value={aiVerdict} onChange={e => setAiVerdict(e.target.value)}>
          <option value="">AI: All</option>
          <option value="true_alert">AI: Real</option>
          <option value="false_alert">AI: Likely false</option>
          <option value="uncertain">AI: Uncertain</option>
          <option value="pending">AI: Pending</option>
        </select>

        <input value={search} onChange={e => setSearch(e.target.value)}
               placeholder="Search alerts…"
               className="border rounded px-2 py-1 text-sm flex-1 min-w-[160px]" />

        <span className="text-xs text-slate-500 dark:text-slate-300">
          {range.label}
          {' · '}{rawTotal == null ? `${items.length}${hasMore ? '+' : ''} raw alerts loaded` : `${rawTotal} raw alerts · ${items.length}${hasMore ? '+' : ''} loaded`}
          {' · '}{groups.length} grouped incidents shown
        </span>
      </Card>

      {!loading && refreshError && (
        <Card className="p-2 mb-2 text-xs text-amber-900 bg-amber-50 border-amber-200
                         flex items-center gap-2">
          <span role="status">
            Couldn’t refresh just now — showing the alerts already loaded. ({refreshError})
          </span>
          <button type="button" onClick={refreshNow}
                  className="ml-auto px-2 py-0.5 rounded bg-amber-700 text-white">
            Retry
          </button>
        </Card>
      )}

      {!loading && gapNotice && (
        <Card className="p-2 mb-2 text-xs text-sky-900 bg-sky-50 border-sky-200
                         flex items-center gap-2">
          <span role="status">
            More than {PAGE_SIZE} alerts arrived at once — showing the newest {PAGE_SIZE}.
            Use “Load next” below for older ones.
          </span>
          <button type="button" onClick={() => setGapNotice(false)}
                  aria-label="Dismiss" className="ml-auto px-1 text-sky-700">×</button>
        </Card>
      )}

      {/* "↑ N new alerts" — a zero-height sticky row, so its appearance
          never moves the list being read. It sticks just below the urgent
          ribbon (--vg-sticky-top, published by Layout), at z-[45]: above
          the ribbon (z-40) and the cards, below pop-up viewers (z-50).
          Only the pill itself takes clicks. */}
      {!loading && heldSummary.count > 0 && (
        <div className="sticky z-[45] h-0 -mx-6 px-2 flex justify-center pointer-events-none"
             style={{ top: 'calc(var(--vg-sticky-top, 0px) + 0.75rem)' }}>
          <button type="button" onClick={showHeld} data-testid="new-alerts-button"
                  aria-label={`Show ${heldSummary.label}`}
                  className={'vg-newpill pointer-events-auto inline-flex items-center gap-2 '
                             // Narrow screens: may wrap to two lines rather than cut text.
                             + 'min-h-[44px] max-w-full px-3 sm:px-5 py-1 sm:py-0 rounded-2xl sm:rounded-full '
                             + 'whitespace-normal sm:whitespace-nowrap text-center leading-tight '
                             + 'text-sm sm:text-[15px] font-semibold text-white '
                             + 'shadow-lg shadow-slate-900/25 '
                             + 'ring-1 ring-white/30 focus-visible:outline focus-visible:outline-2 '
                             + 'focus-visible:outline-offset-2 '
                             + (heldSummary.tone === 'urgent'
                               ? 'bg-red-600 hover:bg-red-500 focus-visible:outline-red-400 '
                               : 'bg-sky-600 hover:bg-sky-500 focus-visible:outline-sky-400 ')
                             + (heldPulse ? 'vg-newpill-pulse' : '')}>
            <span aria-hidden="true" className="text-lg leading-none">↑</span>
            <span className="truncate hidden sm:inline">{heldSummary.label}</span>
            <span className="sm:hidden">{heldSummary.shortLabel}</span>
          </button>
        </div>
      )}
      {/* Announced politely when new alerts start waiting. */}
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {heldAnnouncement}
      </div>

      <div className="space-y-2 vg-feed-list">
        {loading ? (
          <Card className="p-8 text-center text-slate-500 dark:text-slate-300">
            <div role="status">Loading alerts…</div>
          </Card>
        ) : loadError && items.length === 0 ? (
          <Card className="p-8 text-center text-red-700 bg-red-50 border-red-200">
            <div className="font-medium">Alerts could not be loaded.</div>
            <div className="text-xs mt-1 break-words">{loadError}</div>
            <button type="button" onClick={reload}
                    className="mt-3 px-3 py-1.5 rounded bg-red-700 text-white text-sm">
              Try again
            </button>
          </Card>
        ) : groups.length === 0 ? (
          <Card className="p-8 text-center text-slate-500 dark:text-slate-300">No alerts to show.</Card>
        ) : (
          groups.map(g => {
            // Keyed by the GROUP, not its newest alert, so a group that
            // grows updates in place and keeps the card's open state.
            const rowIds = [g.head.id, ...g.siblings.map(s => s.id)]
            const freshCount = rowIds.filter(id => glowIds.has(id)).length
            const isNewGroup = freshCount > 0 && freshCount === rowIds.length
            const entering = isNewGroup && rowIds.some(id => enterIds.has(id))
            return (
              <div key={g.key}
                   className={(entering ? 'vg-feed-enter ' : '') + (isNewGroup ? 'vg-feed-glow' : '')}>
                <AlertCard alert={g.head}
                           groupCount={g.count} groupLast={g.last}
                           groupUnresolvedCount={g.unresolvedCount}
                           groupSiblings={g.siblings}
                           highlightCount={freshCount > 0 && !isNewGroup}
                           onChanged={refreshNow} />
              </div>
            )
          })
        )}
      </div>

      {!loading && loadError && items.length > 0 && (
        <Card className="p-3 mt-3 text-sm text-red-700 bg-red-50 border-red-200">
          More alerts could not be loaded: {loadError}
        </Card>
      )}

      {!loading && hasMore && (
        <div className="mt-4 text-center">
          <button type="button" disabled={loadingMore}
                  onClick={() => void loadPage(true, items[items.length - 1]?.id)}
                  className="px-4 py-2 rounded bg-slate-700 text-white text-sm
                             hover:bg-slate-600 disabled:opacity-50">
            {loadingMore ? 'Loading more…' : `Load next ${PAGE_SIZE} alerts`}
          </button>
        </div>
      )}

      {/* Toast — bottom-right, auto-dismiss after 4s. */}
      {toast && (
        <div className="fixed bottom-6 right-6 bg-slate-800 text-white text-sm
                        rounded shadow-lg px-4 py-2 z-50">
          {toast}
        </div>
      )}
    </div>
  )
}

function ProofOfLifeCard({ proof }: { proof: ProofOfLife }) {
  const colour = proof.state === 'active'
    ? 'border-emerald-300 bg-emerald-50 text-emerald-950'
    : proof.state === 'degraded'
      ? 'border-amber-300 bg-amber-50 text-amber-950'
      : 'border-red-300 bg-red-50 text-red-950'
  const title = proof.state === 'active'
    ? 'Monitoring active'
    : proof.state === 'degraded'
      ? 'Monitoring active — coverage or capacity constrained'
      : 'Monitoring offline — investigate now'
  return (
    <Card className={`p-3 mb-4 border ${colour}`}>
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
        <div>
          <div className="font-semibold">{title}</div>
          <div className="text-xs opacity-75">Pipeline heartbeat {ageLabel(proof.pipeline_age_seconds)}</div>
        </div>
        <div>
          <span className="text-xs opacity-70">Latest AI activity</span><br />
          <strong>{ageLabel(proof.latest_detection_age_seconds)}</strong>
          <div className="text-[11px] opacity-70">{detectionLabel(proof)}</div>
        </div>
        <div><span className="text-xs opacity-70">Latest alert</span><br /><strong>{ageLabel(proof.latest_alert_age_seconds)}</strong></div>
        <div><span className="text-xs opacity-70">Fresh feeds</span><br /><strong>{proof.cameras_fresh}/{proof.cameras_total}</strong></div>
        <div><span className="text-xs opacity-70">Active / waiting</span><br /><strong>{proof.cameras_actively_inferencing ?? '—'} / {proof.cameras_waiting_for_worker ?? '—'}</strong></div>
        <div><span className="text-xs opacity-70">Full rotation</span><br /><strong>{proof.estimated_full_rotation_seconds == null ? '—' : `${Math.ceil(proof.estimated_full_rotation_seconds / 60)} min`}</strong></div>
      </div>
      <div className="mt-2 text-xs opacity-75">
        Routine metrics and filtered detections confirm AI activity but do not create incidents.
        Alert-worthy detections always appear in the alert feed.
      </div>
    </Card>
  )
}

function QuickBtn({ active, onClick, children, tone = 'default' }: {
  active: boolean; onClick: () => void; children: React.ReactNode
  tone?: 'default' | 'teal' | 'green'
}) {
  // Same padding / sizing across tones — only the colour swap differs.
  // The teal tone marks the Store Update tab so it stands apart from the
  // urgent / attention / resolved buckets.
  const palette = tone === 'teal'
    ? (active
        ? 'bg-teal-600 text-white font-bold hover:bg-teal-700'
        : 'bg-teal-500 text-white font-bold hover:bg-teal-600')
    : tone === 'green'
      ? (active
          ? 'bg-emerald-700 text-white font-bold hover:bg-emerald-800'
          : 'bg-emerald-100 text-emerald-800 font-bold hover:bg-emerald-200')
    : (active
        ? 'bg-slate-800 text-white font-medium'
        : 'bg-slate-100 text-slate-700 font-medium hover:bg-slate-200')
  return (
    <button onClick={onClick}
            className={'px-3 py-1.5 rounded text-sm ' + palette}>
      {children}
    </button>
  )
}


// Executive summary bar — spec Part 1 §3. One card at the top of the
// page with the day label, four severity counts, resolved tally, the
// average time-to-resolve, and the vs-yesterday trend.
function ExecutiveSummaryBar({ summary }: {
  summary: {
    critical_today: number; high_today: number
    medium_today:   number; low_today:  number
    calibration_today: number
    resolved_today: number
    ai_true_today?: number; ai_false_today?: number
    ai_uncertain_today?: number; ai_pending_today?: number
    avg_response_seconds: number | null
    today_count: number; yesterday_count: number
    trend_vs_yesterday_pct: number | null
    date_label: string | null
  }
}) {
  const today = summary.date_label || new Date().toLocaleDateString(
    'en-GB', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })
  const avg = summary.avg_response_seconds
  const avgText = avg == null
    ? '—'
    : avg < 60
      ? `${Math.round(avg)}s`
      : `${Math.floor(avg / 60)}m ${Math.round(avg % 60)}s`
  const trend = summary.trend_vs_yesterday_pct
  const trendText = trend == null
    ? null
    : trend > 0 ? `📈 Alerts up ${trend}% vs yesterday`
      : trend < 0 ? `📉 Alerts down ${Math.abs(trend)}% vs yesterday`
      : '➡️ Same volume as yesterday'
  const trendColor = trend == null ? 'text-slate-500'
    : trend > 0 ? 'text-red-600'
    : trend < 0 ? 'text-emerald-600'
    : 'text-slate-500'

  return (
    <div className="rounded-lg border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 shadow-sm
                    mb-3 px-4 py-3">
      <div className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-300 mb-1">
        Today's Store Health — {today}
      </div>
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-sm">
        <SevPill label="Critical" emoji="🔴" count={summary.critical_today}
                 tone="text-red-700 bg-red-50 border-red-200" />
        <SevPill label="High"     emoji="🟠" count={summary.high_today}
                 tone="text-orange-700 bg-orange-50 border-orange-200" />
        <SevPill label="Medium"   emoji="🟡" count={summary.medium_today}
                 tone="text-yellow-700 bg-yellow-50 border-yellow-200" />
        <SevPill label="Low"      emoji="🔵" count={summary.low_today}
                 tone="text-blue-700 bg-blue-50 border-blue-200" />
        <SevPill label="Calibration" emoji="🧪" count={summary.calibration_today}
                 tone="text-violet-700 bg-violet-50 border-violet-200" />
        <span className="text-xs text-slate-500 dark:text-slate-300 ml-2"
              title="AI verification counts (annotate-only)">
          AI: {summary.ai_true_today ?? 0} real / {summary.ai_false_today ?? 0} likely false / {summary.ai_uncertain_today ?? 0} uncertain / {summary.ai_pending_today ?? 0} pending
        </span>
        <span className="text-slate-300">|</span>
        <span className="text-emerald-700">
          ✅ <strong className="tabular-nums">{summary.resolved_today}</strong> Resolved
        </span>
        <span className="text-slate-600 dark:text-slate-300">
          ⏱ Avg Response: <strong className="tabular-nums">{avgText}</strong>
        </span>
        {trendText && (
          <span className={'ml-auto ' + trendColor}>{trendText}</span>
        )}
      </div>
    </div>
  )
}

function SevPill({ label, emoji, count, tone }: {
  label: string; emoji: string; count: number; tone: string
}) {
  return (
    <span className={'inline-flex items-center gap-1.5 px-2 py-0.5 rounded ' +
                     'border text-xs font-medium ' + tone}>
      <span>{emoji}</span>
      <span className="tabular-nums">{count}</span>
      <span className="opacity-80">{label}</span>
    </span>
  )
}
