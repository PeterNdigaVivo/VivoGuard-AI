// Alerts — the operator incident queue.
//
// One continuing event is ONE incident (deduplicated on the server), and
// one action closes it. Every number on this page comes from the server
// for the same store + date range: the tab counts and the summary bar
// from /incidents/summary, the list total from /incidents, the export
// and "Resolve all" from the same query. Nothing is counted from the
// rows that happen to be loaded in the browser.
//
// Until INCIDENT_LIFECYCLE_ENABLED is switched on (after migration 0050),
// the previous alert feed is shown unchanged (AlertsLegacyPage).

import { useCallback, useEffect, useRef, useState } from 'react'
import { Card, PageHeader } from '@/components/ui/Primitives'
import DateRangePicker, { rangeFor, type DateRange } from '@/components/DateRangePicker'
import { AlertCard } from '@/components/AlertCard'
import { alerts as alertsApi } from '@/api/alerts'
import { stores as storesApi, type Store } from '@/api/stores'
import {
  fleet as fleetApi, incidents as incidentsApi,
  type FleetStatus, type Incident, type IncidentSummary, type IncidentView,
} from '@/api/incidents'
import {
  VIEWS, bulkConfirmText, countFor, policyReasonText, shortAge,
} from '@/lib/incidentViews'
import AlertsLegacyPage from './AlertsLegacyPage'

const PAGE_SIZE = 50
const REFRESH_DEBOUNCE_MS = 1500

export default function AlertsPage() {
  // null = still checking whether the incident lifecycle is on.
  const [enabled, setEnabled] = useState<boolean | null>(null)
  useEffect(() => {
    incidentsApi.summary()
      .then(s => setEnabled(s.lifecycle_enabled))
      .catch(() => setEnabled(false))
  }, [])
  if (enabled === null) {
    return <div className="p-6 text-sm text-slate-500 dark:text-slate-300">Loading…</div>
  }
  return enabled ? <IncidentQueue /> : <AlertsLegacyPage />
}

function IncidentQueue() {
  const [view, setView] = useState<IncidentView>('open')
  const [range, setRange] = useState<DateRange>(() => rangeFor('today'))
  const [storeId, setStoreId] = useState('')
  const [stores, setStores] = useState<Store[]>([])
  const [items, setItems] = useState<Incident[]>([])
  const [total, setTotal] = useState(0)
  const [summary, setSummary] = useState<IncidentSummary | null>(null)
  const [fleet, setFleet] = useState<FleetStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const [newWaiting, setNewWaiting] = useState(false)
  const sequence = useRef(0)

  const filters = { store_id: storeId || undefined, since: range.since, until: range.until }
  const filterKey = `${view}|${storeId}|${range.since}|${range.until}`

  useEffect(() => { storesApi.list().then(setStores).catch(() => {}) }, [])
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 5000)
    return () => clearTimeout(t)
  }, [toast])

  // Fleet status card: its own slow poll.
  useEffect(() => {
    const load = () => fleetApi.status().then(setFleet).catch(() => setFleet(null))
    load()
    const t = setInterval(load, 60_000)
    return () => clearInterval(t)
  }, [])

  // List + summary always load together, with the same filters.
  const reload = useCallback(async (quiet = false) => {
    const seq = ++sequence.current
    if (!quiet) { setLoading(true); setItems([]) }
    setError(null)
    try {
      const [page, sum] = await Promise.all([
        incidentsApi.list(view, filters, PAGE_SIZE, 0),
        incidentsApi.summary(filters),
      ])
      if (seq !== sequence.current) return
      setItems(page.items)
      setTotal(page.total)
      setSummary(sum)
      setNewWaiting(false)
    } catch (e) {
      if (seq === sequence.current) setError(String(e))
    } finally {
      if (seq === sequence.current) setLoading(false)
    }
  }, [filterKey])

  useEffect(() => { void reload() }, [reload])

  // Live updates: refresh quietly at the top of the list; further down,
  // offer a button instead of moving the list under the operator.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null
    const unsub = alertsApi.subscribe(() => {
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => {
        if (window.scrollY < 40) void reload(true)
        else {
          setNewWaiting(true)
          incidentsApi.summary(filters).then(setSummary).catch(() => {})
        }
      }, REFRESH_DEBOUNCE_MS)
    })
    return () => { if (timer) clearTimeout(timer); unsub() }
  }, [reload])

  async function loadMore() {
    setLoadingMore(true)
    try {
      const page = await incidentsApi.list(view, filters, PAGE_SIZE, items.length)
      setItems(prev => [...prev, ...page.items.filter(i => !prev.some(p => p.id === i.id))])
      setTotal(page.total)
    } catch (e) {
      setToast(`Could not load more: ${e}`)
    } finally {
      setLoadingMore(false)
    }
  }

  const viewDef = VIEWS.find(v => v.key === view)!
  const storeLabel = storeId
    ? stores.find(s => String(s.id) === storeId)?.name ?? 'this store'
    : 'all stores'

  async function resolveAll() {
    const count = countFor(view, summary)
    if (!count) { setToast('Nothing to resolve in this view.'); return }
    if (!window.confirm(bulkConfirmText(count, viewDef.label, storeLabel,
                                        range.label.toLowerCase()))) return
    try {
      const { resolved } = await incidentsApi.bulkResolve(view, filters, count)
      setToast(`✅ ${resolved} incident${resolved === 1 ? '' : 's'} resolved`)
      window.dispatchEvent(new CustomEvent('vg:alert-resolved', { detail: { bulk: 0 } }))
    } catch (e) {
      setToast(`Nothing was resolved — the number of incidents changed. `
               + `Please check the refreshed list and try again. (${e})`)
    }
    void reload(true)
  }

  async function exportExcel() {
    const tok = localStorage.getItem('vg_access_token') ?? ''
    const res = await fetch(incidentsApi.exportUrl(view, filters), {
      headers: { Authorization: `Bearer ${tok}` },
    })
    if (!res.ok) { setToast('Export failed.'); return }
    const url = URL.createObjectURL(await res.blob())
    const a = document.createElement('a')
    a.href = url
    a.download = `vivoguard_incidents_${view}_${range.key}.xlsx`
    document.body.appendChild(a); a.click(); document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  async function act(inc: Incident, action: 'escalate' | 'acknowledge') {
    try {
      await incidentsApi.act(inc.id, action, { version: inc.version })
      setToast(action === 'escalate' ? 'Incident escalated' : 'Incident acknowledged')
    } catch (e) {
      setToast(`Could not ${action}: ${e}`)
    }
    void reload(true)
  }

  return (
    <div className="p-6">
      <PageHeader title="Alerts" actions={
        <div className="flex items-center gap-2">
          <DateRangePicker value={range} onChange={setRange} />
          {viewDef.bulkResolvable && (
            <button type="button" onClick={resolveAll}
                    className="px-3 py-1.5 rounded bg-slate-700 text-white text-sm hover:bg-slate-600">
              ✅ Resolve all ({countFor(view, summary) ?? '…'})
            </button>
          )}
          <button type="button" onClick={exportExcel}
                  className="px-3 py-1.5 rounded bg-emerald-600 text-white text-sm hover:bg-emerald-500">
            📊 Export
          </button>
        </div>
      } />

      <OperationalStatusCard fleet={fleet} />
      <IncidentSummaryBar summary={summary} />

      <div className="flex flex-wrap items-center gap-2 mb-3">
        {VIEWS.map(v => (
          <button key={v.key} type="button" title={v.hint} onClick={() => setView(v.key)}
                  className={'px-3 py-1.5 rounded text-sm font-medium '
                    + (view === v.key
                      ? 'bg-slate-800 text-white dark:bg-slate-200 dark:text-slate-900'
                      : 'vg-on-light bg-slate-100 text-slate-700 hover:bg-slate-200')}>
            {v.label} ({countFor(v.key, summary) ?? '…'})
          </button>
        ))}
        <select value={storeId} onChange={e => setStoreId(e.target.value)}
                className="ml-auto px-2 py-1.5 rounded border border-slate-300 dark:border-slate-700
                           bg-white dark:bg-slate-900 text-sm text-slate-800 dark:text-slate-100">
          <option value="">All stores</option>
          {stores.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
      </div>

      {newWaiting && (
        <button type="button" onClick={() => { window.scrollTo({ top: 0 }); void reload(true) }}
                className="sticky top-2 z-10 mb-3 w-full px-3 py-2 rounded bg-sky-600 text-white text-sm shadow">
          ↑ New activity — show the latest
        </button>
      )}

      <div className="text-xs text-slate-500 dark:text-slate-300 mb-2">
        {loading ? 'Loading…' : `Showing ${items.length} of ${total} ${viewDef.label.toLowerCase()}`}
      </div>

      <div className="space-y-3">
        {error ? (
          <Card className="p-6 text-center text-red-700 bg-red-50 border-red-200 dark:bg-red-950 dark:border-red-900 dark:text-red-200">
            <div className="font-medium">Incidents could not be loaded — this does not mean there are none.</div>
            <div className="text-xs mt-1 break-words">{error}</div>
            <button type="button" onClick={() => void reload()}
                    className="mt-3 px-3 py-1.5 rounded bg-red-700 text-white text-sm">Try again</button>
          </Card>
        ) : !loading && items.length === 0 ? (
          <Card className="p-8 text-center text-slate-500 dark:text-slate-300">
            Nothing in {viewDef.label.toLowerCase()} for {storeLabel} ({range.label.toLowerCase()}).
          </Card>
        ) : items.map(inc => (
          <IncidentRow key={inc.id} incident={inc}
                       onAct={act} onChanged={() => void reload(true)} />
        ))}
      </div>

      {!loading && items.length < total && (
        <div className="mt-4 text-center">
          <button type="button" disabled={loadingMore} onClick={loadMore}
                  className="px-4 py-2 rounded bg-slate-700 text-white text-sm hover:bg-slate-600 disabled:opacity-50">
            {loadingMore ? 'Loading…' : `Load ${Math.min(PAGE_SIZE, total - items.length)} more`}
          </button>
        </div>
      )}

      {toast && (
        <div role="status"
             className="fixed bottom-6 right-6 max-w-md bg-slate-800 text-white text-sm rounded shadow-lg px-4 py-2 z-50">
          {toast}
        </div>
      )}
    </div>
  )
}

const PRIORITY_STYLE: Record<Incident['priority'], string> = {
  urgent: 'vg-on-light bg-red-100 text-red-800 border-red-200',
  attention: 'vg-on-light bg-amber-100 text-amber-800 border-amber-200',
  low: 'vg-on-light bg-slate-100 text-slate-700 border-slate-200',
}
const PRIORITY_LABEL: Record<Incident['priority'], string> = {
  urgent: 'URGENT', attention: 'ATTENTION', low: 'REVIEW',
}

function fmtTime(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString('en-GB', {
    hour: '2-digit', minute: '2-digit', timeZone: 'Africa/Nairobi',
  })
}

function IncidentRow({ incident, onAct, onChanged }: {
  incident: Incident
  onAct: (i: Incident, a: 'escalate' | 'acknowledge') => void
  onChanged: () => void
}) {
  const open = ['open', 'acknowledged', 'escalated'].includes(incident.status)
  const reason = policyReasonText(incident.policy_reason)
  return (
    <div className="rounded-lg border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-xs
                      text-slate-600 dark:text-slate-300 border-b border-slate-100 dark:border-slate-800">
        <span className={'px-1.5 py-0.5 rounded border font-semibold ' + PRIORITY_STYLE[incident.priority]}>
          {incident.queue === 'review' ? 'REVIEW' : PRIORITY_LABEL[incident.priority]}
        </span>
        <span className="font-medium text-slate-800 dark:text-slate-100">
          {incident.status === 'open' ? 'Open' : incident.status[0].toUpperCase() + incident.status.slice(1)}
        </span>
        <span title="Repeated detections of the same continuing event are grouped here">
          Seen {incident.event_count}× · {fmtTime(incident.first_seen_at)}–{fmtTime(incident.last_seen_at)}
        </span>
        <span>{[incident.store_name, incident.camera_name, incident.zone_name].filter(Boolean).join(' · ')}</span>
        {reason && <span className="italic">{reason}</span>}
        {open && (
          <span className="ml-auto flex gap-2">
            {incident.status === 'open' && (
              <button type="button" onClick={() => onAct(incident, 'acknowledge')}
                      className="px-2 py-1 rounded bg-slate-700 text-white hover:bg-slate-600">
                👁 I’m on it
              </button>
            )}
            {incident.status !== 'escalated' && incident.queue === 'incident' && (
              <button type="button" onClick={() => onAct(incident, 'escalate')}
                      className="px-2 py-1 rounded bg-red-700 text-white hover:bg-red-600">
                ⬆ Escalate
              </button>
            )}
          </span>
        )}
        {!open && incident.close_reason && (
          <span className="ml-auto">Closed: {incident.close_reason.replaceAll('_', ' ')}</span>
        )}
      </div>
      {incident.head
        ? <AlertCard alert={incident.head} onChanged={onChanged} />
        : <div className="p-3 text-sm text-slate-600 dark:text-slate-300">
            {incident.detection_type.replaceAll('_', ' ')}
          </div>}
    </div>
  )
}

function OperationalStatusCard({ fleet }: { fleet: FleetStatus | null }) {
  const [showGaps, setShowGaps] = useState(false)
  if (!fleet) {
    return (
      <Card className="p-3 mb-3 text-sm text-slate-600 dark:text-slate-300">
        Monitoring status unavailable.
      </Card>
    )
  }
  const tone = fleet.health === 'healthy'
    ? 'border-emerald-300 bg-emerald-50 text-emerald-950 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-100'
    : fleet.health === 'degraded'
      ? 'border-amber-300 bg-amber-50 text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100'
      : 'border-red-300 bg-red-50 text-red-950 dark:border-red-800 dark:bg-red-950 dark:text-red-100'
  const title = fleet.monitoring === 'offline' ? 'Monitoring offline — investigate now'
    : fleet.health === 'healthy' ? 'Monitoring active'
      : fleet.health === 'degraded' ? 'Monitoring active — coverage reduced'
        : 'Monitoring critical'
  const t = fleet.totals
  const q = fleet.queue
  const gaps = fleet.coverage_gaps.filter(g => g.state !== 'unconfigured')
  return (
    <Card className={`p-3 mb-3 border ${tone}`}>
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
        <div>
          <div className="font-semibold">{title}</div>
          {fleet.health_reasons[0] && <div className="text-xs opacity-80">{fleet.health_reasons[0]}</div>}
        </div>
        <Stat label="Fresh cameras" value={`${t.fresh}/${t.total}`} />
        <Stat label="AI-active" value={String(t.ai_active)} />
        <Stat label="Queue / rotation"
              value={`${q.waiting_for_worker} waiting · ${q.full_rotation_seconds == null
                ? '—' : `${Math.ceil(q.full_rotation_seconds / 60)} min`}`} />
        <Stat label="Latest detection" value={`${shortAge(fleet.latest_detection_age_seconds)} ago`} />
        <Stat label="Latest incident" value={`${shortAge(fleet.latest_incident_age_seconds)} ago`} />
        {gaps.length > 0 && (
          <button type="button" onClick={() => setShowGaps(s => !s)}
                  className="ml-auto text-xs underline">
            {showGaps ? 'Hide' : 'Show'} {gaps.length} coverage gap{gaps.length === 1 ? '' : 's'}
          </button>
        )}
      </div>
      {showGaps && (
        <ul className="mt-2 text-xs grid sm:grid-cols-2 gap-x-6 gap-y-1">
          {gaps.map(g => (
            <li key={g.camera_id}>
              <strong>{g.state === 'offline' ? 'Offline' : 'Stale'}</strong>
              {' · '}{g.store_name ?? 'no store'} · {g.camera_name} — {g.reason}
            </li>
          ))}
        </ul>
      )}
      <div className="mt-2 text-xs opacity-75">
        Camera coverage problems are listed here and on System Health; they are never added to the incident queue.
        {t.unconfigured > 0 && ` ${t.unconfigured} camera(s) are not set up for AI.`}
      </div>
    </Card>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span className="text-xs opacity-70">{label}</span><br />
      <strong className="tabular-nums">{value}</strong>
    </div>
  )
}

function IncidentSummaryBar({ summary }: { summary: IncidentSummary | null }) {
  if (!summary) {
    return (
      <Card className="p-3 mb-3 text-sm text-slate-500 dark:text-slate-300">
        Loading incident counts…
      </Card>
    )
  }
  const avg = summary.avg_response_seconds
  const avgText = avg == null ? '—' : avg < 60 ? `${avg}s` : `${Math.floor(avg / 60)}m ${avg % 60}s`
  return (
    <div className="rounded-lg border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900
                    shadow-sm mb-3 px-4 py-2 flex flex-wrap items-center gap-x-5 gap-y-1 text-sm
                    text-slate-700 dark:text-slate-200">
      <span><strong className="tabular-nums text-red-700 dark:text-red-300">{summary.urgent}</strong> urgent</span>
      <span><strong className="tabular-nums text-amber-700 dark:text-amber-300">{summary.attention}</strong> needs attention</span>
      <span><strong className="tabular-nums text-violet-700 dark:text-violet-300">{summary.review}</strong> in review queue</span>
      <span><strong className="tabular-nums text-emerald-700 dark:text-emerald-300">{summary.resolved}</strong> resolved</span>
      <span className="ml-auto">⏱ Avg response <strong className="tabular-nums">{avgText}</strong></span>
    </div>
  )
}
