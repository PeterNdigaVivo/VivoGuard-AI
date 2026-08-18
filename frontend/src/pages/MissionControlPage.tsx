// Mission Control — restricted system-health dashboard at /system-health,
// SYSTEM ADMINS ONLY (allowlist in lib/systemAdmins.ts; the API enforces
// the same list server-side, this page just avoids a guaranteed 403).
// Distinct from pages/SystemHealthPage.tsx, the all-users /system page.

import { useCallback, useEffect, useState } from 'react'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'
import { api } from '@/api/client'
import { useAuth } from '@/auth/AuthContext'
import { Card } from '@/components/ui/Primitives'
import { isSystemAdmin } from '@/lib/systemAdmins'

interface Snapshot {
  generated_at: string
  overall: { emoji: string; label: string }
  containers: { name: string; status: string; uptime: string | null; healthy: boolean }[]
  cameras: {
    total: number; streaming: number; offline: number
    offline_names: string[]; ai_enabled_active: number
  }
  detection: {
    events_last_30min_by_type: Record<string, number>
    total_events_today: number
    events_by_hour_24h: { hour: string; count: number }[]
  }
  model: {
    version: string | null; map50: number | null
    precision: number | null; recall: number | null
    deployed_since: string | null
  }
  training: {
    jobs_queued: number; jobs_running: number
    jobs_completed_today: number; latest_map50: number | null
  }
  storage: {
    total_gb: number | null; used_gb: number | null; free_gb: number | null
    percent_used: number | null; docker_images_gb: number | null
    recordings_gb: number | null; alert_clips_gb: number | null
  }
  database: { total_alerts: number; alerts_today: number; detection_events_count: number }
  alerts: { urgent_today: number; resolved_today: number; pending_today: number }
  integrations: {
    bytetrack_active: boolean; supervision_active: boolean
    mannequin_filter_active: boolean
  }
}

const num = (v: number | null | undefined, digits = 0) =>
  v == null ? '—' : v.toFixed(digits)

export default function MissionControlPage() {
  const { user } = useAuth()
  const [data, setData] = useState<Snapshot | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [lastLoaded, setLastLoaded] = useState<Date | null>(null)

  const refresh = useCallback(() => {
    setLoading(true)
    api<Snapshot>('/system-health')
      .then(d => { setData(d); setError(null); setLastLoaded(new Date()) })
      .catch(e => setError(e?.message ?? 'failed to load'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    if (!isSystemAdmin(user?.email)) return
    refresh()
    const t = setInterval(refresh, 60_000)   // auto-refresh every 60s
    return () => clearInterval(t)
  }, [refresh, user?.email])

  if (!isSystemAdmin(user?.email)) {
    return (
      <div className="p-6">
        <Card className="p-10 text-center text-slate-500 dark:text-slate-300">
          🔒 This page is restricted to platform system administrators.
        </Card>
      </div>
    )
  }

  const pct = data?.storage.percent_used ?? null
  const barColor = pct == null ? 'bg-slate-400'
    : pct > 90 ? 'bg-red-600' : pct > 80 ? 'bg-amber-500' : 'bg-emerald-600'

  return (
    <div className="p-6 space-y-4 print:p-2">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-2xl font-semibold">
            💓 System Health
            {data && ` — ${data.overall.emoji} ${data.overall.label}`}
          </h1>
          <div className="text-sm text-slate-500 dark:text-slate-300">
            {lastLoaded
              ? `Last refreshed ${lastLoaded.toLocaleTimeString()} · auto-refresh 60s`
              : 'Loading…'}
          </div>
        </div>
        <div className="flex gap-2 print:hidden">
          <button onClick={refresh} disabled={loading}
                  className="px-3 py-1.5 rounded bg-sky-600 text-white text-sm hover:bg-sky-700 disabled:opacity-50">
            {loading ? 'Refreshing…' : '↻ Refresh'}
          </button>
          {/* Browser print-to-PDF — every desktop browser ships it and
              it needs no backend PDF dependency. */}
          <button onClick={() => window.print()}
                  className="px-3 py-1.5 rounded bg-slate-700 text-white text-sm hover:bg-slate-800">
            ⬇ Export PDF
          </button>
        </div>
      </div>

      {error && (
        <Card className="p-4 text-red-600">Failed to load: {error}</Card>
      )}
      {!data && !error && <Card className="p-10 text-center">Loading…</Card>}

      {data && (
        <>
          {/* Containers */}
          <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-6 gap-3">
            {data.containers.map(c => (
              <Card key={c.name}
                    className={'p-3 border-l-4 ' +
                      (c.healthy ? 'border-l-emerald-500' : 'border-l-red-500')}>
                <div className="font-mono text-sm font-semibold truncate" title={c.name}>
                  {c.healthy ? '🟢' : '🔴'} {c.name}
                </div>
                <div className="text-xs text-slate-500 dark:text-slate-300 truncate"
                     title={c.status}>{c.status}</div>
                {c.uptime && (
                  <div className="text-xs text-slate-400">up {c.uptime}</div>
                )}
              </Card>
            ))}
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {/* Cameras */}
            <Card className="p-4">
              <div className="font-semibold mb-2">📷 Cameras</div>
              <div className="text-3xl font-bold">
                {data.cameras.streaming}
                <span className="text-lg text-slate-400">/{data.cameras.total} streaming</span>
              </div>
              <div className="text-sm text-slate-500 dark:text-slate-300 mt-1">
                {data.cameras.offline} offline · {data.cameras.ai_enabled_active} AI-active
              </div>
              {data.cameras.offline_names.length > 0 && (
                <div className="mt-2 text-xs text-amber-600 dark:text-amber-400">
                  Offline: {data.cameras.offline_names.join(', ')}
                </div>
              )}
            </Card>

            {/* Storage */}
            <Card className="p-4">
              <div className="font-semibold mb-2">💾 Storage</div>
              <div className="h-4 rounded bg-slate-200 dark:bg-slate-700 overflow-hidden">
                <div className={`h-4 ${barColor}`}
                     style={{ width: `${Math.min(pct ?? 0, 100)}%` }} />
              </div>
              <div className="text-sm mt-2">
                {num(data.storage.used_gb, 1)} / {num(data.storage.total_gb, 1)} GB
                ({num(pct, 1)}%) · {num(data.storage.free_gb, 1)} GB free
              </div>
              <div className="text-xs text-slate-500 dark:text-slate-300 mt-1">
                Recordings {num(data.storage.recordings_gb, 1)} GB ·
                alert clips {num(data.storage.alert_clips_gb, 1)} GB
              </div>
              {pct != null && pct > 80 && (
                <div className="mt-1 text-xs font-semibold text-red-600">
                  ⚠ Above 80% — prune recordings or expand the volume.
                </div>
              )}
            </Card>
          </div>

          {/* Detection activity — last 24h */}
          <Card className="p-4">
            <div className="font-semibold mb-2">
              📊 Detection activity (last 24h) —
              {' '}{data.detection.total_events_today.toLocaleString()} events today
            </div>
            <div className="h-48">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={data.detection.events_by_hour_24h}>
                  <XAxis dataKey="hour" tick={{ fontSize: 10 }} interval={2} />
                  <YAxis tick={{ fontSize: 10 }} width={40} />
                  <Tooltip />
                  <Bar dataKey="count" fill="#0284c7" radius={[2, 2, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
            <div className="text-xs text-slate-500 dark:text-slate-300 mt-2">
              Last 30 min:{' '}
              {Object.entries(data.detection.events_last_30min_by_type)
                .sort((a, b) => b[1] - a[1])
                .map(([k, v]) => `${k} ${v}`).join(' · ') || 'no events'}
            </div>
          </Card>

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            {/* Model */}
            <Card className="p-4">
              <div className="font-semibold mb-2">🧠 Deployed model</div>
              <div className="text-sm space-y-1">
                <div className="font-mono">{data.model.version ?? 'none deployed'}</div>
                <div>mAP50: <b>{num(data.model.map50, 3)}</b></div>
                <div>Precision: <b>{num(data.model.precision, 3)}</b> ·
                     Recall: <b>{num(data.model.recall, 3)}</b></div>
                <div className="text-xs text-slate-500 dark:text-slate-300">
                  Since {data.model.deployed_since?.slice(0, 10) ?? '—'}
                </div>
              </div>
            </Card>

            {/* Training */}
            <Card className="p-4">
              <div className="font-semibold mb-2">🏋️ Training pipeline</div>
              <div className="text-sm space-y-1">
                <div>{data.training.jobs_queued} queued ·
                     {' '}{data.training.jobs_running} running</div>
                <div>{data.training.jobs_completed_today} completed today</div>
                <div>Latest mAP50: <b>{num(data.training.latest_map50, 3)}</b></div>
              </div>
            </Card>

            {/* Alerts + DB */}
            <Card className="p-4">
              <div className="font-semibold mb-2">🚨 Alerts today</div>
              <div className="text-sm space-y-1">
                <div>🔴 {data.alerts.urgent_today} urgent ·
                     ⏳ {data.alerts.pending_today} pending ·
                     ✅ {data.alerts.resolved_today} resolved</div>
                <div className="text-xs text-slate-500 dark:text-slate-300 pt-1">
                  DB: {data.database.total_alerts.toLocaleString()} alerts ·
                  {' '}{data.database.detection_events_count.toLocaleString()} events
                </div>
              </div>
            </Card>
          </div>

          {/* Integrations */}
          <Card className="p-4">
            <div className="font-semibold mb-2">🔌 Integrations</div>
            <div className="flex gap-4 text-sm flex-wrap">
              <span>{data.integrations.bytetrack_active ? '🟢' : '🔴'} ByteTrack</span>
              <span>{data.integrations.supervision_active ? '🟢' : '🔴'} Supervision</span>
              <span>{data.integrations.mannequin_filter_active ? '🟢' : '⚪'} Mannequin filter</span>
            </div>
          </Card>
        </>
      )}
    </div>
  )
}
