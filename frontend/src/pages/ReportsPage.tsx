// Reports — create / manage scheduled reports + download historical
// PDF / CSV. Backed by the existing /reports/scheduled CRUD plus
// /analytics/report.pdf and /analytics/report.csv for on-demand
// downloads.
//
// Each row sends email (always) and optional WhatsApp via Twilio.
// time_of_day + day_of_week pin the fire to a wall-clock slot.

import { useEffect, useState } from 'react'
import { Button, Card, Input, PageHeader, Select } from '@/components/ui/Primitives'
import { api } from '@/api/client'
import { stores as storesApi, type Store } from '@/api/stores'


interface Schedule {
  id: number
  name: string
  store_id: number | null
  cadence: 'daily' | 'weekly' | 'monthly' | 'quarterly'
  format: 'pdf' | 'csv'
  recipients: string
  whatsapp_recipients: string | null
  time_of_day: string | null
  day_of_week: number | null
  is_active: boolean
  last_run_at: string | null
  last_fire_date: string | null
}

export default function ReportsPage() {
  const [schedules, setSchedules] = useState<Schedule[]>([])
  const [stores, setStores] = useState<Store[]>([])
  const [open, setOpen] = useState(false)

  const reload = () => api<Schedule[]>('/reports/scheduled').then(setSchedules)
  useEffect(() => {
    reload().catch(console.error)
    storesApi.list().then(setStores).catch(console.error)
  }, [])

  async function remove(id: number) {
    if (!confirm('Delete this schedule?')) return
    await api(`/reports/scheduled/${id}`, { method: 'DELETE' })
    reload()
  }

  async function runNow(id: number) {
    await api(`/reports/scheduled/${id}/run-now`, { method: 'POST' })
    alert('Queued — recipients should receive it within a minute.')
  }

  return (
    <div className="p-6 space-y-4">
      <PageHeader title="Reports"
                  actions={<Button onClick={() => setOpen(true)}>+ New schedule</Button>} />

      <OnDemandDownloads stores={stores} />

      <Card>
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600 text-left">
            <tr>
              <th className="p-3">Name</th>
              <th className="p-3">Scope</th>
              <th className="p-3">When</th>
              <th className="p-3">Format</th>
              <th className="p-3">Recipients</th>
              <th className="p-3">Last run</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {schedules.length === 0 && (
              <tr><td colSpan={7} className="p-8 text-center text-slate-500">
                No scheduled reports yet. Click "+ New schedule" to create one.
              </td></tr>
            )}
            {schedules.map(s => (
              <tr key={s.id} className="border-t hover:bg-slate-50">
                <td className="p-3 font-medium">{s.name}
                  {!s.is_active && <span className="ml-2 text-[10px] text-slate-400">paused</span>}
                </td>
                <td className="p-3">
                  {s.store_id
                    ? (stores.find(x => x.id === s.store_id)?.name ?? `Store #${s.store_id}`)
                    : 'Whole chain'}
                </td>
                <td className="p-3 capitalize">
                  {s.cadence}
                  {s.time_of_day && <span className="text-slate-500"> @ {s.time_of_day.slice(0, 5)}</span>}
                  {s.day_of_week !== null && s.day_of_week !== undefined && (
                    <span className="text-slate-500"> ({weekdayName(s.day_of_week)})</span>
                  )}
                </td>
                <td className="p-3 uppercase">{s.format}</td>
                <td className="p-3 text-xs text-slate-600">
                  <div className="truncate max-w-[20rem]" title={s.recipients}>
                    ✉ {s.recipients}
                  </div>
                  {s.whatsapp_recipients && (
                    <div className="truncate max-w-[20rem] text-emerald-700"
                         title={s.whatsapp_recipients}>
                      💬 {s.whatsapp_recipients}
                    </div>
                  )}
                </td>
                <td className="p-3 text-xs text-slate-500">
                  {s.last_run_at ? new Date(s.last_run_at).toLocaleString() : '—'}
                </td>
                <td className="p-3 text-right whitespace-nowrap">
                  <Button variant="ghost" onClick={() => runNow(s.id)}>Run now</Button>
                  <Button variant="ghost" onClick={() => remove(s.id)}>Delete</Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      {open && (
        <ScheduleEditor stores={stores}
                        onClose={() => setOpen(false)}
                        onSaved={() => { setOpen(false); reload() }} />
      )}
    </div>
  )
}


// ---- On-demand downloads --------------------------------------------

function OnDemandDownloads({ stores }: { stores: Store[] }) {
  const [storeId, setStoreId] = useState<number | ''>('')
  const [since, setSince]   = useState(() => new Date(Date.now() - 7 * 86400_000).toISOString().slice(0, 10))
  const [until, setUntil]   = useState(() => new Date().toISOString().slice(0, 10))

  async function download(format: 'pdf' | 'csv') {
    const tok = localStorage.getItem('vg_access_token') ?? ''
    const url = new URL(window.location.origin + `/api/analytics/report.${format}`)
    if (storeId) url.searchParams.set('store_id', String(storeId))
    url.searchParams.set('since', `${since}T00:00:00Z`)
    url.searchParams.set('until', `${until}T23:59:59Z`)
    const res = await fetch(url.toString(), { headers: { Authorization: `Bearer ${tok}` } })
    if (!res.ok) { alert(`Download failed: ${res.status}`); return }
    const blob = await res.blob()
    const link = document.createElement('a')
    link.href = URL.createObjectURL(blob)
    link.download = `vivoguard_${storeId || 'chain'}_${since}_to_${until}.${format}`
    link.click()
    setTimeout(() => URL.revokeObjectURL(link.href), 1000)
  }

  return (
    <Card className="p-4">
      <div className="text-sm font-semibold text-slate-700 mb-2">Download historical report</div>
      <div className="flex flex-wrap items-end gap-3 text-sm">
        <label className="block">
          <div className="text-xs text-slate-500 mb-1">Store</div>
          <Select value={storeId} onChange={e => setStoreId(e.target.value ? Number(e.target.value) : '')}>
            <option value="">Whole chain</option>
            {stores.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
          </Select>
        </label>
        <label className="block">
          <div className="text-xs text-slate-500 mb-1">From</div>
          <Input type="date" value={since} onChange={e => setSince(e.target.value)} />
        </label>
        <label className="block">
          <div className="text-xs text-slate-500 mb-1">To</div>
          <Input type="date" value={until} onChange={e => setUntil(e.target.value)} />
        </label>
        <Button onClick={() => download('pdf')}>Download PDF</Button>
        <Button variant="ghost" onClick={() => download('csv')}>Download CSV</Button>
      </div>
    </Card>
  )
}


// ---- Schedule editor modal ------------------------------------------

function ScheduleEditor({ stores, onClose, onSaved }: {
  stores: Store[]; onClose: () => void; onSaved: () => void
}) {
  const [form, setForm] = useState({
    name: '', store_id: '' as number | '',
    cadence: 'daily' as 'daily' | 'weekly' | 'monthly' | 'quarterly',
    format:  'pdf'   as 'pdf' | 'csv',
    recipients: '',
    whatsapp_recipients: '',
    time_of_day: '21:00',
    day_of_week: 0,    // Mon
    is_active: true,
  })
  const [busy, setBusy] = useState(false)

  async function save() {
    if (!form.name || !form.recipients) {
      alert('Name and email recipients are required.'); return
    }
    setBusy(true)
    try {
      await api('/reports/scheduled', {
        method: 'POST',
        body: {
          name: form.name,
          store_id: form.store_id || null,
          cadence: form.cadence,
          format:  form.format,
          recipients: form.recipients,
          whatsapp_recipients: form.whatsapp_recipients || null,
          time_of_day: form.time_of_day,
          day_of_week: form.cadence === 'weekly' ? form.day_of_week : null,
          is_active: form.is_active,
        },
      })
      onSaved()
    } catch (e) {
      alert(`Save failed: ${e}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4"
         onClick={() => !busy && onClose()}>
      <div onClick={e => e.stopPropagation()} className="max-w-xl w-full">
        <Card className="p-5">
          <div className="flex justify-between items-center mb-3">
            <div className="text-lg font-semibold">New scheduled report</div>
            <button onClick={onClose} disabled={busy} className="text-slate-400 hover:text-slate-700">✕</button>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <label className="col-span-2 block">
              <div className="text-xs text-slate-600 mb-1">Name *</div>
              <Input value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
                     placeholder="Junction daily 21:00" />
            </label>
            <label className="block">
              <div className="text-xs text-slate-600 mb-1">Scope</div>
              <Select value={form.store_id}
                      onChange={e => setForm(f => ({ ...f, store_id: e.target.value ? Number(e.target.value) : '' }))}>
                <option value="">Whole chain</option>
                {stores.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
              </Select>
            </label>
            <label className="block">
              <div className="text-xs text-slate-600 mb-1">Cadence</div>
              <Select value={form.cadence}
                      onChange={e => setForm(f => ({ ...f, cadence: e.target.value as any }))}>
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
                <option value="monthly">Monthly</option>
                <option value="quarterly">Quarterly</option>
              </Select>
            </label>
            <label className="block">
              <div className="text-xs text-slate-600 mb-1">Fire time (store-local)</div>
              <Input type="time" value={form.time_of_day}
                     onChange={e => setForm(f => ({ ...f, time_of_day: e.target.value }))} />
            </label>
            {form.cadence === 'weekly' && (
              <label className="block">
                <div className="text-xs text-slate-600 mb-1">Day of week</div>
                <Select value={form.day_of_week}
                        onChange={e => setForm(f => ({ ...f, day_of_week: Number(e.target.value) }))}>
                  <option value={0}>Monday</option>
                  <option value={1}>Tuesday</option>
                  <option value={2}>Wednesday</option>
                  <option value={3}>Thursday</option>
                  <option value={4}>Friday</option>
                  <option value={5}>Saturday</option>
                  <option value={6}>Sunday</option>
                </Select>
              </label>
            )}
            <label className="block">
              <div className="text-xs text-slate-600 mb-1">Format</div>
              <Select value={form.format}
                      onChange={e => setForm(f => ({ ...f, format: e.target.value as any }))}>
                <option value="pdf">PDF</option>
                <option value="csv">CSV</option>
              </Select>
            </label>
            <label className="col-span-2 block">
              <div className="text-xs text-slate-600 mb-1">Email recipients * (comma-separated)</div>
              <Input value={form.recipients}
                     onChange={e => setForm(f => ({ ...f, recipients: e.target.value }))}
                     placeholder="manager@vivo.co.ke,head-office@vivo.co.ke" />
            </label>
            <label className="col-span-2 block">
              <div className="text-xs text-slate-600 mb-1">WhatsApp recipients (optional)</div>
              <Input value={form.whatsapp_recipients}
                     onChange={e => setForm(f => ({ ...f, whatsapp_recipients: e.target.value }))}
                     placeholder="whatsapp:+254712345678,whatsapp:+254700000000" />
              <div className="text-xs text-slate-400 mt-1">
                Twilio format. Recipients receive a short headline-numbers
                text alongside the email PDF.
              </div>
            </label>
          </div>
          <div className="mt-4 flex justify-end gap-2">
            <Button variant="ghost" onClick={onClose} disabled={busy}>Cancel</Button>
            <Button onClick={save} disabled={busy || !form.name || !form.recipients}>
              {busy ? 'Saving…' : 'Schedule'}
            </Button>
          </div>
        </Card>
      </div>
    </div>
  )
}


function weekdayName(idx: number): string {
  return ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][idx] ?? '?'
}
