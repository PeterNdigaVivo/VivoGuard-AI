// Stores list + create — head office adds the 26 retail locations here.

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge, Button, Card, Input, PageHeader, Select } from '@/components/ui/Primitives'
import { stores, type Store } from '@/api/stores'

const COUNTRIES = ['Kenya', 'Uganda', 'Rwanda']

export default function StoresPage() {
  const [list, setList] = useState<Store[]>([])
  const [form, setForm] = useState({ name: '', code: '', country: 'Kenya', city: '', capacity: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const reload = () => stores.list().then(setList).catch(e => setError(String(e)))
  useEffect(() => { reload() }, [])

  async function create() {
    if (!form.name || !form.country) return
    setBusy(true); setError(null)
    try {
      await stores.create({
        name: form.name, code: form.code || null,
        country: form.country, city: form.city || null,
        capacity: form.capacity ? Number(form.capacity) : null,
      })
      setForm({ name: '', code: '', country: 'Kenya', city: '', capacity: '' })
      reload()
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }

  // Group by country so the list scans like a chain map.
  const groups: Record<string, Store[]> = {}
  for (const s of list) (groups[s.country] ??= []).push(s)

  return (
    <div className="p-6">
      <PageHeader title="Stores" />

      <Card className="p-4 mb-6">
        <div className="font-medium mb-3">New store</div>
        <div className="grid grid-cols-1 md:grid-cols-6 gap-2 items-end">
          <Input placeholder="Name" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} />
          <Input placeholder="Code (e.g. NRB-001)" value={form.code} onChange={e => setForm({ ...form, code: e.target.value })} />
          <Select value={form.country} onChange={e => setForm({ ...form, country: e.target.value })}>
            {COUNTRIES.map(c => <option key={c} value={c}>{c}</option>)}
          </Select>
          <Input placeholder="City" value={form.city} onChange={e => setForm({ ...form, city: e.target.value })} />
          <Input placeholder="Capacity" type="number" value={form.capacity}
                 onChange={e => setForm({ ...form, capacity: e.target.value })} />
          <Button onClick={create} disabled={busy || !form.name}>Add</Button>
        </div>
        {error && <div className="text-red-600 text-sm mt-2">{error}</div>}
      </Card>

      {Object.entries(groups).map(([country, items]) => (
        <Card key={country} className="mb-4">
          <div className="px-4 py-2 bg-slate-50 text-slate-600 font-medium border-b">{country}</div>
          <table className="w-full text-sm">
            <thead className="text-slate-600">
              <tr>
                <th className="text-left p-3">Store</th>
                <th className="text-left p-3">Code</th>
                <th className="text-left p-3">City</th>
                <th className="text-left p-3">Capacity</th>
                <th className="text-left p-3">Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {items.map(s => (
                <tr key={s.id} className="border-t hover:bg-slate-50">
                  <td className="p-3 font-medium">
                    <Link to={`/stores/${s.id}`} className="text-sky-600 hover:underline">{s.name}</Link>
                  </td>
                  <td className="p-3 font-mono text-xs">{s.code ?? '—'}</td>
                  <td className="p-3">{s.city ?? '—'}</td>
                  <td className="p-3">{s.capacity ?? '—'}</td>
                  <td className="p-3">
                    {s.is_active ? <Badge color="green">active</Badge> : <Badge color="slate">disabled</Badge>}
                  </td>
                  <td className="p-3 text-right">
                    <Link to={`/stores/${s.id}`} className="text-sky-600 hover:underline">Dashboard</Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      ))}

      {!list.length && (
        <Card className="p-8 text-center text-slate-500">
          No stores yet — add one above.
        </Card>
      )}
    </div>
  )
}
