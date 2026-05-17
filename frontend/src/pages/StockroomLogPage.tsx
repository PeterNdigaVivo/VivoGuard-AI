// Audit trail for stockroom access. Filter by store / date range,
// mark rows authorised/unauthorised after review, and export CSV.

import { useEffect, useState } from 'react'
import { Badge, Button, Card, Input, PageHeader, Select } from '@/components/ui/Primitives'
import DateRangePicker, { rangeFor, type DateRange } from '@/components/DateRangePicker'
import { api } from '@/api/client'
import { stores as storesApi, type Store } from '@/api/stores'

interface AccessRow {
  id: number; store_id: number | null; camera_id: number
  zone_id: number | null; direction: 'in' | 'out'; track_id: number
  is_authorised: boolean | null; snapshot_path: string | null
  timestamp: string
}

export default function StockroomLogPage() {
  const [stores, setStores] = useState<Store[]>([])
  const [storeId, setStoreId] = useState<string>('')
  const [range, setRange] = useState<DateRange>(() => rangeFor('today'))
  const [rows, setRows] = useState<AccessRow[]>([])

  const reload = () => {
    const params = new URLSearchParams()
    if (storeId) params.set('store_id', storeId)
    params.set('since', range.since)
    params.set('until', range.until)
    api<AccessRow[]>(`/stockroom/accesses?${params}`).then(setRows).catch(console.error)
  }

  useEffect(() => { storesApi.list().then(setStores) }, [])
  useEffect(() => { reload() }, [storeId, range.since, range.until])

  async function mark(id: number, ok: boolean) {
    await api(`/stockroom/accesses/${id}/mark-authorised?is_authorised=${ok}`, { method: 'POST' })
    reload()
  }

  function downloadCSV() {
    const params = new URLSearchParams()
    if (storeId) params.set('store_id', storeId)
    params.set('since', range.since)
    params.set('until', range.until)
    window.location.href = `/api/stockroom/accesses.csv?${params}`
  }

  return (
    <div className="p-6">
      <PageHeader title="Stockroom access log" actions={
        <>
          <DateRangePicker value={range} onChange={setRange} />
          <Button variant="ghost" onClick={downloadCSV}>Export CSV</Button>
        </>
      } />

      <Card className="p-3 mb-4 flex flex-wrap gap-3 items-end">
        <label className="text-sm">
          <div className="text-xs text-slate-500 mb-1">Store</div>
          <Select value={storeId} onChange={e => setStoreId(e.target.value)}>
            <option value="">all stores</option>
            {stores.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
          </Select>
        </label>
        <div className="text-xs text-slate-500 ml-auto">
          Showing <strong>{range.label}</strong> · {rows.length} entr{rows.length === 1 ? 'y' : 'ies'}
        </div>
      </Card>

      <Card>
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600">
            <tr>
              <th className="text-left p-3">When</th>
              <th className="text-left p-3">Camera</th>
              <th className="text-left p-3">Direction</th>
              <th className="text-left p-3">Track</th>
              <th className="text-left p-3">Authorisation</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.id} className="border-t hover:bg-slate-50">
                <td className="p-3 whitespace-nowrap">{new Date(r.timestamp).toLocaleString()}</td>
                <td className="p-3">cam #{r.camera_id}</td>
                <td className="p-3">
                  {r.direction === 'in' ? <Badge color="amber">in</Badge>
                                        : <Badge color="slate">out</Badge>}
                </td>
                <td className="p-3 font-mono text-xs">tr{r.track_id}</td>
                <td className="p-3">
                  {r.is_authorised === true  && <Badge color="green">authorised</Badge>}
                  {r.is_authorised === false && <Badge color="red">UNAUTHORISED</Badge>}
                  {r.is_authorised === null  && <Badge color="slate">unreviewed</Badge>}
                </td>
                <td className="p-3 text-right space-x-2 whitespace-nowrap">
                  <Button onClick={() => mark(r.id, true)}>Authorised</Button>
                  <Button variant="danger" onClick={() => mark(r.id, false)}>Unauthorised</Button>
                </td>
              </tr>
            ))}
            {!rows.length && (
              <tr><td colSpan={6} className="p-8 text-center text-slate-500">
                No stockroom access events.
              </td></tr>
            )}
          </tbody>
        </table>
      </Card>
    </div>
  )
}
