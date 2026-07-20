// Store detail page — the operator's home for one store.
//
// Layout (top → bottom):
//   1. Header  — store name + status light + last update + "Edit" + "Open analytics" buttons
//   2. Cameras grid — one card per camera. Each card shows live thumbnail,
//      online/offline badge, AI-configured status, and a primary CTA
//      depending on state ("Configure AI" / "View analytics").
//   3. Add camera CTA — always visible
//   4. KPI summary — small tile row pulling from /analytics/store/{id}/live

import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  Badge, Button, Card, PageHeader, Skeleton, StatusLight,
} from '@/components/ui/Primitives'
import { api } from '@/api/client'
import { analytics, stores as storesApi, type Store } from '@/api/stores'
import { cameras as camsApi, type Camera } from '@/api/cameras'

const COUNTRY_FLAG: Record<string, string> = {
  Kenya: '🇰🇪', Uganda: '🇺🇬', Rwanda: '🇷🇼',
}

interface LiveResponse {
  status: 'live' | 'no_data_yet' | 'no_cameras'
  status_light?: 'green' | 'amber' | 'red'
  as_of: string
  tiles: Record<string, { value: any; visible: boolean; trend?: { direction: string; delta_pct: number | null } }>
  zone_capabilities: Record<string, boolean>
}

interface CameraWithZones extends Camera {
  zone_count?: number
}

export default function StoreDetailPage() {
  const { id } = useParams()
  const storeId = Number(id)
  const nav = useNavigate()

  const [store, setStore] = useState<Store | null>(null)
  const [cams, setCams] = useState<CameraWithZones[] | null>(null)
  const [live, setLive] = useState<LiveResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function reload() {
    try {
      const [s, c] = await Promise.all([
        storesApi.get(storeId),
        api<Camera[]>(`/stores/${storeId}/cameras`),
      ])
      setStore(s)
      // Fetch zone counts in parallel (best-effort).
      const withZones = await Promise.all(c.map(async cam => {
        try {
          const zs = await camsApi.listZones(cam.id)
          return { ...cam, zone_count: zs.length }
        } catch { return { ...cam, zone_count: 0 } }
      }))
      setCams(withZones)
    } catch (e) { setError(String(e)) }
  }

  useEffect(() => {
    reload()
    const tickLive = () =>
      analytics.storeDashboard(storeId, 1).catch(() => null)  // for legacy compatibility
    tickLive()
    const t = setInterval(() => {
      api<LiveResponse>(`/analytics/store/${storeId}/live`).then(setLive).catch(() => {})
    }, 30_000)
    api<LiveResponse>(`/analytics/store/${storeId}/live`).then(setLive).catch(() => {})
    return () => clearInterval(t)
  }, [storeId])

  if (error) return <div className="p-6 text-red-600">Error: {error}</div>
  if (!store || cams === null) {
    return (
      <div className="p-6 space-y-4">
        <Skeleton className="h-12 w-1/3" />
        <Skeleton className="h-48" />
      </div>
    )
  }

  return (
    <div className="p-6 space-y-6">
      {/* HEADER */}
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <div className="text-3xl font-semibold">{store.name}</div>
          <div className="text-slate-500 dark:text-slate-300 mt-1">
            {COUNTRY_FLAG[store.country] ?? '🏳️'} {store.city ?? '—'}, {store.country}
            {store.manager_name && <span className="ml-3">👤 {store.manager_name}</span>}
            {store.manager_phone && <span className="ml-3">📞 {store.manager_phone}</span>}
          </div>
        </div>
        <div className="flex items-center gap-3">
          {live?.status_light && <StatusLight value={live.status_light} />}
          <Link to={`/stores/${storeId}/analytics`} className="text-sky-600 hover:underline">
            Full analytics →
          </Link>
          <Link to="/stores">
            <Button variant="ghost">All stores</Button>
          </Link>
        </div>
      </div>

      {/* BUSINESS HOURS strip */}
      {store.business_hours_json && (
        <div className="text-xs text-slate-500 dark:text-slate-300 -mt-2">
          Hours:{' '}
          {(['mon','tue','wed','thu','fri','sat','sun'] as const).map(d => {
            const wins = store.business_hours_json?.[d] ?? []
            return (
              <span key={d} className="mr-3">
                <strong className="uppercase">{d}</strong>{' '}
                {wins.length === 0 ? <span className="text-slate-400">closed</span> : wins.join(', ')}
              </span>
            )
          })}
        </div>
      )}

      {/* CAMERAS */}
      <section>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold">Cameras ({cams.length})</h2>
          <Button onClick={() => nav(`/stores/${storeId}/add-camera`)}>+ Add camera</Button>
        </div>

        {cams.length === 0 ? (
          <Card className="p-10 text-center">
            <div className="text-5xl mb-2">📹</div>
            <div className="text-lg font-medium mb-1">No cameras yet</div>
            <div className="text-slate-500 dark:text-slate-300 mb-4">
              Add your first camera to start collecting data for this store.
            </div>
            <Button onClick={() => nav(`/stores/${storeId}/add-camera`)}>
              Add your first camera
            </Button>
          </Card>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {cams.map(c => <CameraCard key={c.id} cam={c} />)}
          </div>
        )}
      </section>

      {/* KPIs summary at bottom (full analytics on a dedicated page) */}
      {live?.status === 'live' && (
        <section>
          <h2 className="text-lg font-semibold mb-3">Today at a glance</h2>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Mini label="People in store now" value={live.tiles.occupancy_now?.value} />
            <Mini label="Unique visitors today" value={live.tiles.unique_visitors_today?.value} />
            <Mini label="Peak occupancy" value={live.tiles.occupancy_peak_today?.value} />
            {live.tiles.queue_length_now?.visible && (
              <Mini label="Queue now" value={live.tiles.queue_length_now.value} />
            )}
          </div>
          <div className="text-xs text-slate-500 dark:text-slate-300 mt-2">
            Updated {new Date(live.as_of).toLocaleTimeString()}.
            See the <Link to={`/stores/${storeId}/analytics`} className="text-sky-600 hover:underline">full analytics</Link>.
          </div>
        </section>
      )}
      {live?.status === 'no_data_yet' && (
        <Card className="p-4 text-slate-500 dark:text-slate-300 text-sm">
          Cameras just attached — collecting first measurements. Numbers appear within a minute or two.
        </Card>
      )}
    </div>
  )
}

function CameraCard({ cam }: { cam: CameraWithZones }) {
  const configured = (cam.zone_count ?? 0) > 0
  return (
    <Card className="p-3 flex flex-col">
      <div className="flex items-start justify-between mb-2">
        <div className="font-medium truncate flex-1">{cam.name}</div>
        <Badge color={cam.status === 'online' ? 'green' :
                      cam.status === 'offline' ? 'red' : 'amber'}>
          {cam.status}
        </Badge>
      </div>
      <div className="relative aspect-video bg-slate-900 rounded overflow-hidden">
        <img src={`/api/cameras/${cam.id}/snapshot?_=${Math.floor(Date.now() / 60_000)}`}
             alt=""
             onError={(e) => ((e.target as HTMLImageElement).style.display = 'none')}
             className="w-full h-full object-cover" />
      </div>
      <div className="mt-2 flex items-center justify-between text-xs">
        <div>
          {configured
            ? <Badge color="sky">AI configured ({cam.zone_count} zone{cam.zone_count === 1 ? '' : 's'})</Badge>
            : <Badge color="amber">AI not configured</Badge>}
        </div>
      </div>
      <div className="mt-3 flex justify-between">
        <Link to={`/cameras/${cam.id}/setup`}
              className="text-sm text-sky-600 hover:underline">
          {configured ? 'Edit zones' : 'Configure AI'}
        </Link>
        <Link to={`/cameras/${cam.id}/heatmap`}
              className="text-sm text-slate-500 dark:text-slate-300 hover:underline">
          Heatmap
        </Link>
      </div>
    </Card>
  )
}

function Mini({ label, value }: { label: string; value: any }) {
  return (
    <Card className="p-3">
      <div className="text-xs text-slate-500 dark:text-slate-300">{label}</div>
      <div className="text-xl font-semibold mt-1">
        {value === null || value === undefined ? '0' : Math.round(Number(value) || 0)}
      </div>
    </Card>
  )
}
