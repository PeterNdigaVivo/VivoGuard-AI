// Stores list — card grid, prominent "Add Store" CTA. After save we
// jump straight to the new store's detail page so the operator can
// add their first camera.

import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Badge, Button, Card, Input, PageHeader, Skeleton } from '@/components/ui/Primitives'
import StoreForm from '@/components/StoreForm'
import { stores as storesApi, type Store } from '@/api/stores'

const COUNTRY_FLAG: Record<string, string> = {
  Kenya: '🇰🇪', Uganda: '🇺🇬', Rwanda: '🇷🇼',
}

export default function StoresPage() {
  const nav = useNavigate()
  const [list, setList] = useState<Store[] | null>(null)
  const [creating, setCreating] = useState(false)
  // Client-side name filter — purely local, no extra API calls.
  const [query, setQuery] = useState('')

  const reload = () => storesApi.list().then(setList).catch(console.error)
  useEffect(() => { reload() }, [])

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
          <Button onClick={() => setCreating(true)}>+ Add your first store</Button>
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
          {filtered.map(s => (
            <Link key={s.id} to={`/stores/${s.id}`}
                  className="block hover:scale-[1.01] transition-transform">
              <Card className="p-5 h-full">
                <div className="flex items-start justify-between gap-2 mb-2">
                  <div>
                    <div className="text-lg font-semibold">{s.name}</div>
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
          ))}
        </div>
      )}
    </div>
  )
}
