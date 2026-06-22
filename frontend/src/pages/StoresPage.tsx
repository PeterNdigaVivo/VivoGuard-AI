// Stores list — card grid, prominent "Add Store" CTA, plus the
// Zone Health tab (Feature 4) that surfaces missing / duplicate /
// typo'd zones across the chain in one screen.

import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Badge, Button, Card, Input, PageHeader, Skeleton } from '@/components/ui/Primitives'
import StoreForm from '@/components/StoreForm'
import { stores as storesApi, type Store } from '@/api/stores'
import { api } from '@/api/client'

const COUNTRY_FLAG: Record<string, string> = {
  Kenya: '🇰🇪', Uganda: '🇺🇬', Rwanda: '🇷🇼',
}

// ---- Zone Health types (mirror backend payload) ---------------------
interface ZoneHealthIssue {
  kind: 'missing_tag' | 'duplicate_zone' | 'typo_name' | 'camera_offline' | 'no_cameras'
  message: string
  tag?: string
  camera_id?: number
  camera_name?: string | null
  count?: number
  zone_id?: number
  current_name?: string
  suggested_name?: string
  similarity?: number
  deduction?: number
}
interface ZoneHealthRow {
  store_id: number
  store_name: string | null
  score: number
  badge: 'healthy' | 'warn' | 'critical'
  per_tag_present: {
    entry_exit: boolean
    queue: boolean
    staff_zone: boolean
    shutter: boolean
  }
  cameras_total: number
  cameras_online: number
  cameras_offline: number
  issues: ZoneHealthIssue[]
}

const BADGE_ICON: Record<ZoneHealthRow['badge'], string> = {
  healthy: '🟢', warn: '🟡', critical: '🔴',
}

type Tab = 'grid' | 'health'

export default function StoresPage() {
  const nav = useNavigate()
  const [list, setList] = useState<Store[] | null>(null)
  const [creating, setCreating] = useState(false)
  const [tab, setTab] = useState<Tab>('grid')
  // Client-side name filter — purely local, no extra API calls.
  const [query, setQuery] = useState('')

  // Zone-health rollup. One fetch powers BOTH the dashboard tab AND
  // the per-card badges on the grid view, so the API is hit once
  // regardless of which tab the operator opens.
  const [health, setHealth]   = useState<ZoneHealthRow[] | null>(null)
  const [healthErr, setHealthErr] = useState<string | null>(null)
  const [healthLoading, setHealthLoading] = useState(false)

  const reload = () => storesApi.list().then(setList).catch(console.error)
  useEffect(() => { reload() }, [])

  const loadHealth = async () => {
    setHealthLoading(true)
    try {
      const rows = await api<ZoneHealthRow[]>('/stores/zone-health')
      setHealth(rows); setHealthErr(null)
    } catch (e) {
      setHealthErr(String(e))
    } finally { setHealthLoading(false) }
  }
  useEffect(() => {
    loadHealth()
    const t = setInterval(loadHealth, 60_000)
    return () => clearInterval(t)
  }, [])

  const healthByStoreId = useMemo(() => {
    const m = new Map<number, ZoneHealthRow>()
    for (const r of health ?? []) m.set(r.store_id, r)
    return m
  }, [health])

  // Case-insensitive substring match on store name. Null list (still
  // loading) passes through so the skeleton stays visible.
  const filtered = useMemo(() => {
    if (!list) return list
    const q = query.trim().toLowerCase()
    if (!q) return list
    return list.filter(s => (s.name ?? '').toLowerCase().includes(q))
  }, [list, query])

  async function onCreate(data: Partial<Store>) {
    const created = await storesApi.create(data)
    setCreating(false)
    nav(`/stores/${created.id}`)
  }

  if (creating) {
    return (
      <div className="p-6">
        <PageHeader title="Add a new store" actions={
          <Button variant="ghost" onClick={() => setCreating(false)}>Cancel</Button>
        } />
        <StoreForm onSubmit={onCreate}
                   onCancel={() => setCreating(false)}
                   submitLabel="Create store" />
      </div>
    )
  }

  return (
    <div className="p-6">
      <PageHeader title="Stores" actions={
        <Button onClick={() => setCreating(true)}>+ Add store</Button>
      } />

      {/* Tab switcher */}
      <div className="mb-4 flex items-center gap-2 border-b border-slate-200">
        <TabBtn active={tab === 'grid'}   onClick={() => setTab('grid')}>Stores</TabBtn>
        <TabBtn active={tab === 'health'} onClick={() => setTab('health')}>
          Zone Health{healthByStoreId.size ? ` (${healthByStoreId.size})` : ''}
        </TabBtn>
      </div>

      {tab === 'grid' && (
        <StoresGrid
          list={list}
          filtered={filtered}
          query={query} setQuery={setQuery}
          healthByStoreId={healthByStoreId}
          onCreate={() => setCreating(true)} />
      )}

      {tab === 'health' && (
        <ZoneHealthTab
          health={health}
          loading={healthLoading}
          error={healthErr}
          onRefresh={loadHealth} />
      )}
    </div>
  )
}


// ---- Tab button ------------------------------------------------------
function TabBtn({ active, onClick, children }: {
  active: boolean; onClick: () => void; children: React.ReactNode
}) {
  return (
    <button onClick={onClick}
            className={'px-3 py-2 text-sm border-b-2 -mb-px '
              + (active
                  ? 'border-sky-600 text-sky-700 font-semibold'
                  : 'border-transparent text-slate-500 hover:text-slate-700')}>
      {children}
    </button>
  )
}


// ---- Grid view (existing card list + health badge inline) -----------
function StoresGrid({ list, filtered, query, setQuery, healthByStoreId,
                      onCreate }: {
  list: Store[] | null
  filtered: Store[] | null
  query: string
  setQuery: (q: string) => void
  healthByStoreId: Map<number, ZoneHealthRow>
  onCreate: () => void
}) {
  return (
    <>
      {list === null && (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {[1, 2, 3].map(i => <Skeleton key={i} className="h-40" />)}
        </div>
      )}

      {list && list.length === 0 && (
        <Card className="p-12 text-center">
          <div className="text-5xl mb-3">🏬</div>
          <div className="text-lg font-medium mb-1">No stores yet</div>
          <div className="text-slate-500 mb-4">
            Start by adding your first store. Cameras attach to stores.
          </div>
          <Button onClick={onCreate}>+ Add your first store</Button>
        </Card>
      )}

      {list && list.length > 0 && (
        <div className="mb-4 flex items-center gap-2">
          <Input
            type="search"
            placeholder={`Search ${list.length} stores by name…`}
            value={query}
            onChange={e => setQuery(e.target.value)}
            className="max-w-md"
          />
          {query && (
            <button
              onClick={() => setQuery('')}
              className="text-sm text-slate-500 hover:text-slate-700">
              Clear
            </button>
          )}
          {query && filtered && (
            <span className="text-sm text-slate-500">
              {filtered.length} of {list.length}
            </span>
          )}
        </div>
      )}

      {list && list.length > 0 && filtered && filtered.length === 0 && (
        <Card className="p-8 text-center text-slate-500">
          No stores match “{query}”.
        </Card>
      )}

      {list && list.length > 0 && filtered && filtered.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {filtered.map(s => {
            const h = healthByStoreId.get(s.id)
            return (
              <Link key={s.id} to={`/stores/${s.id}`}
                    className="block hover:scale-[1.01] transition-transform">
                <Card className="p-5 h-full">
                  <div className="flex items-start justify-between gap-2 mb-2">
                    <div>
                      <div className="text-lg font-semibold flex items-center gap-1.5">
                        {h && <span title={`Zone health ${h.score}/100`}>{BADGE_ICON[h.badge]}</span>}
                        {s.name}
                      </div>
                      <div className="text-sm text-slate-500">
                        {COUNTRY_FLAG[s.country] ?? '🏳️'} {s.city ?? '—'}, {s.country}
                      </div>
                    </div>
                    {s.is_active
                      ? <Badge color="green">active</Badge>
                      : <Badge color="slate">disabled</Badge>}
                  </div>
                  {s.code && <div className="text-xs font-mono text-slate-400 mb-3">{s.code}</div>}
                  <div className="text-xs text-slate-500 space-y-0.5">
                    {s.manager_name && <div>👤 {s.manager_name}</div>}
                    {s.manager_phone && <div>📞 {s.manager_phone}</div>}
                    {s.capacity && <div>👥 capacity: {s.capacity}</div>}
                  </div>
                  <div className="mt-4 text-sky-600 text-sm hover:underline">
                    Open dashboard →
                  </div>
                </Card>
              </Link>
            )
          })}
        </div>
      )}
    </>
  )
}


// ---- Zone Health tab -------------------------------------------------
function ZoneHealthTab({ health, loading, error, onRefresh }: {
  health: ZoneHealthRow[] | null
  loading: boolean
  error: string | null
  onRefresh: () => void
}) {
  const [drawerFor, setDrawerFor] = useState<ZoneHealthRow | null>(null)
  // Sort: critical first, then warn, then healthy. Within a band,
  // lowest score first — operators see what needs attention.
  const sorted = useMemo(() => {
    if (!health) return null
    const bandOrder: Record<ZoneHealthRow['badge'], number> = {
      critical: 0, warn: 1, healthy: 2,
    }
    return [...health].sort((a, b) =>
      bandOrder[a.badge] - bandOrder[b.badge] || a.score - b.score)
  }, [health])

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Button onClick={onRefresh} variant="ghost" disabled={loading}>
          {loading ? 'Refreshing…' : '↻ Refresh'}
        </Button>
        <a href="/api/stores/zone-health.csv"
           className="px-3 py-1.5 text-sm rounded bg-slate-100 text-slate-700 hover:bg-slate-200">
          ⬇ Export CSV
        </a>
        {health && (
          <span className="text-sm text-slate-500">
            {countByBadge(health, 'critical')} critical · {countByBadge(health, 'warn')} need attention · {countByBadge(health, 'healthy')} healthy
          </span>
        )}
        <span className="text-xs text-slate-400 ml-auto">
          Polls every 60s
        </span>
      </div>

      {error && <Card className="p-3 text-rose-700">Failed to load: {error}</Card>}

      {!sorted && loading && (
        <div className="space-y-2">{[1, 2, 3].map(i =>
          <Skeleton key={i} className="h-12" />)}</div>
      )}

      {sorted && (
        <Card className="overflow-x-auto p-0">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 border-b border-slate-200">
              <tr className="text-left">
                <Th>Store</Th>
                <Th>Score</Th>
                <Th title="Footfall / entry_exit zone present">🚪</Th>
                <Th title="Checkout / queue zone present">💳</Th>
                <Th title="Staff / staff_zone present">👤</Th>
                <Th title="Shutter zone present">🪟</Th>
                <Th>Cameras</Th>
                <Th>Issues</Th>
                <Th className="text-right">Actions</Th>
              </tr>
            </thead>
            <tbody>
              {sorted.map(r => (
                <tr key={r.store_id} className="border-b border-slate-100 hover:bg-slate-50">
                  <td className="px-3 py-2 font-medium">
                    <span className="mr-1.5">{BADGE_ICON[r.badge]}</span>
                    {r.store_name || `Store ${r.store_id}`}
                  </td>
                  <td className="px-3 py-2 tabular-nums">
                    <ScorePill score={r.score} badge={r.badge} />
                  </td>
                  <Cell ok={r.per_tag_present.entry_exit} />
                  <Cell ok={r.per_tag_present.queue} />
                  <Cell ok={r.per_tag_present.staff_zone} />
                  <Cell ok={r.per_tag_present.shutter} />
                  <td className="px-3 py-2 text-slate-600 tabular-nums">
                    {r.cameras_online}/{r.cameras_total}
                    {r.cameras_offline > 0 && (
                      <span className="ml-1 text-rose-600">
                        ({r.cameras_offline} off)
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-slate-700 max-w-md">
                    <IssueChips issues={r.issues} />
                  </td>
                  <td className="px-3 py-2 text-right">
                    <button onClick={() => setDrawerFor(r)}
                            className="text-sky-600 text-sm hover:underline">
                      Fix this store →
                    </button>
                  </td>
                </tr>
              ))}
              {sorted.length === 0 && (
                <tr><td colSpan={9} className="p-6 text-center text-slate-500">
                  No active stores.
                </td></tr>
              )}
            </tbody>
          </table>
        </Card>
      )}

      {drawerFor && (
        <FixDrawer row={drawerFor} onClose={() => setDrawerFor(null)} />
      )}
    </>
  )
}


function countByBadge(rows: ZoneHealthRow[], b: ZoneHealthRow['badge']): number {
  return rows.filter(r => r.badge === b).length
}


function Th({ children, className = '', title }: {
  children: React.ReactNode; className?: string; title?: string
}) {
  return (
    <th title={title}
        className={'px-3 py-2 text-xs font-medium text-slate-500 ' + className}>
      {children}
    </th>
  )
}


function Cell({ ok }: { ok: boolean }) {
  return (
    <td className="px-3 py-2 text-center">
      <span className={ok ? 'text-emerald-600' : 'text-rose-500'}>
        {ok ? '✅' : '❌'}
      </span>
    </td>
  )
}


function ScorePill({ score, badge }: {
  score: number; badge: ZoneHealthRow['badge']
}) {
  const cls = badge === 'healthy'
    ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
    : badge === 'warn'
      ? 'bg-amber-50 text-amber-700 border-amber-200'
      : 'bg-rose-50 text-rose-700 border-rose-200'
  return (
    <span className={'inline-block min-w-[2.75rem] text-center text-xs '
                     + 'border rounded px-1.5 py-0.5 font-semibold ' + cls}>
      {score}
    </span>
  )
}


function IssueChips({ issues }: { issues: ZoneHealthIssue[] }) {
  if (issues.length === 0) return <span className="text-slate-400">—</span>
  const head = issues.slice(0, 2)
  const more = issues.length - head.length
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {head.map((i, idx) => (
        <span key={idx}
              className="text-xs px-1.5 py-0.5 rounded bg-slate-100 text-slate-700">
          {i.message}
        </span>
      ))}
      {more > 0 && (
        <span className="text-xs text-slate-500">+{more} more</span>
      )}
    </div>
  )
}


// ---- Fix This Store drawer ------------------------------------------
function FixDrawer({ row, onClose }: {
  row: ZoneHealthRow; onClose: () => void
}) {
  // Group issues for the drawer's three sections.
  const missing   = row.issues.filter(i => i.kind === 'missing_tag')
  const dups      = row.issues.filter(i => i.kind === 'duplicate_zone')
  const typos     = row.issues.filter(i => i.kind === 'typo_name')
  const offline   = row.issues.filter(i => i.kind === 'camera_offline')

  return (
    <>
      <div className="fixed inset-0 bg-slate-900/40 z-40"
           onClick={onClose} />
      <aside className="fixed top-0 right-0 bottom-0 w-full sm:w-[480px]
                        bg-white shadow-xl z-50 overflow-y-auto p-5">
        <div className="flex items-start justify-between mb-3">
          <div>
            <div className="text-xs text-slate-500">Zone health</div>
            <div className="text-lg font-semibold">
              {BADGE_ICON[row.badge]} {row.store_name || `Store ${row.store_id}`}
            </div>
            <div className="text-sm text-slate-500">
              Score {row.score}/100 · {row.cameras_online}/{row.cameras_total} cameras online
            </div>
          </div>
          <button onClick={onClose}
                  className="text-slate-500 hover:text-slate-800 text-xl leading-none">
            ✕
          </button>
        </div>

        {missing.length > 0 && (
          <Section title="Missing zones">
            {missing.map((i, idx) => (
              <div key={idx} className="flex items-center justify-between
                                         py-2 border-b border-slate-100">
                <div>
                  <div className="font-medium">No {i.tag} zone</div>
                  <div className="text-xs text-slate-500">
                    Add this zone to any camera in the store.
                  </div>
                </div>
                <Link to={`/cameras?addZone=${i.tag}&store=${row.store_id}`}
                      className="text-sm text-sky-600 hover:underline">
                  Add zone →
                </Link>
              </div>
            ))}
          </Section>
        )}

        {dups.length > 0 && (
          <Section title="Duplicate zones (same-camera)">
            {dups.map((i, idx) => (
              <div key={idx} className="flex items-center justify-between
                                         py-2 border-b border-slate-100">
                <div>
                  <div className="font-medium">
                    {i.camera_name || `Camera ${i.camera_id}`} has {i.count} {i.tag} zones
                  </div>
                  <div className="text-xs text-slate-500">
                    Delete the extras — this is causing {i.tag === 'entry_exit'
                      ? 'double / triple footfall counts' : 'noisy alerts'}.
                  </div>
                </div>
                <Link to={`/cameras/${i.camera_id}?filter=${i.tag}`}
                      className="text-sm text-sky-600 hover:underline">
                  Review →
                </Link>
              </div>
            ))}
          </Section>
        )}

        {typos.length > 0 && (
          <Section title="Typo'd zone names">
            {typos.map((i, idx) => (
              <div key={idx} className="flex items-center justify-between
                                         py-2 border-b border-slate-100">
                <div className="text-sm">
                  <div className="font-medium">
                    {i.camera_name || `Camera ${i.camera_id}`}
                  </div>
                  {/* Explicit before/after — operators see exactly the
                      rename before they click. */}
                  <div className="text-slate-600">
                    Rename:{' '}
                    <span className="font-mono bg-rose-50 px-1 rounded text-rose-700">
                      “{i.current_name}”
                    </span>
                    {' → '}
                    <span className="font-mono bg-emerald-50 px-1 rounded text-emerald-700">
                      “{i.suggested_name}”
                    </span>
                  </div>
                  <div className="text-xs text-slate-400">
                    similarity {((i.similarity ?? 0) * 100).toFixed(0)}%
                  </div>
                </div>
                <Link to={`/cameras/${i.camera_id}?renameZone=${i.zone_id}&to=${encodeURIComponent(i.suggested_name || '')}`}
                      className="text-sm text-sky-600 hover:underline whitespace-nowrap">
                  Rename →
                </Link>
              </div>
            ))}
          </Section>
        )}

        {offline.length > 0 && (
          <Section title="Offline cameras">
            {offline.map((i, idx) => (
              <div key={idx} className="flex items-center justify-between
                                         py-2 border-b border-slate-100">
                <div>
                  <div className="font-medium">
                    {i.camera_name || `Camera ${i.camera_id}`}
                  </div>
                  <div className="text-xs text-slate-500">
                    Check the NVR feed for this camera.
                  </div>
                </div>
                <Link to={`/cameras/${i.camera_id}`}
                      className="text-sm text-sky-600 hover:underline">
                  Open →
                </Link>
              </div>
            ))}
          </Section>
        )}

        {row.issues.length === 0 && (
          <Card className="p-4 text-center text-emerald-700 bg-emerald-50 border-emerald-200">
            ✅ No issues — this store is fully configured.
          </Card>
        )}

        {/* Clone from best camera — wired in Feature 6 */}
        <div className="mt-4 pt-4 border-t border-slate-200">
          <button disabled
                  title="Available when Feature 6 (Zone Cloning Tool) ships"
                  className="w-full px-3 py-2 rounded bg-slate-100 text-slate-400
                             cursor-not-allowed text-sm">
            🔁 Clone zones from best camera (Feature 6 — coming)
          </button>
        </div>
      </aside>
    </>
  )
}


function Section({ title, children }: {
  title: string; children: React.ReactNode
}) {
  return (
    <div className="mb-4">
      <div className="text-xs font-semibold text-slate-500 uppercase mb-1">
        {title}
      </div>
      <div>{children}</div>
    </div>
  )
}
