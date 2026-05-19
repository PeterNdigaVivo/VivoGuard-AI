// Reusable store create/edit form. Used inline on /stores (modal) and
// later on /stores/:id/edit. Includes a simple weekday-grid business
// hours editor and manager-contact fields for WhatsApp routing.

import { useEffect, useState } from 'react'
import { Button, Card, Input, Select } from '@/components/ui/Primitives'
import type { Store } from '@/api/stores'

const COUNTRIES = ['Kenya', 'Uganda', 'Rwanda', 'Other']
const TIMEZONES = [
  'Africa/Nairobi', 'Africa/Kampala', 'Africa/Kigali',
  'Africa/Dar_es_Salaam', 'Africa/Johannesburg', 'UTC',
]
const WEEKDAYS: { key: string; label: string }[] = [
  { key: 'mon', label: 'Mon' }, { key: 'tue', label: 'Tue' },
  { key: 'wed', label: 'Wed' }, { key: 'thu', label: 'Thu' },
  { key: 'fri', label: 'Fri' }, { key: 'sat', label: 'Sat' },
  { key: 'sun', label: 'Sun' },
]

type FormState = {
  name: string; code: string; country: string; city: string
  address: string; timezone: string; capacity: string
  manager_name: string; manager_phone: string
  default_rtsp_port: string   // empty = use brand default
  business_hours: Record<string, { open: boolean; start: string; end: string }>
}

function defaultBusinessHours(): FormState['business_hours'] {
  const out: FormState['business_hours'] = {}
  for (const d of WEEKDAYS) {
    out[d.key] = { open: d.key !== 'sun', start: '09:00', end: '20:00' }
  }
  return out
}

function parseBusinessHours(json: Record<string, string[]> | null): FormState['business_hours'] {
  const base = defaultBusinessHours()
  if (!json) return base
  for (const d of WEEKDAYS) {
    const wins = json[d.key]
    if (wins && wins.length > 0) {
      const [start, end] = wins[0].split('-')
      base[d.key] = { open: true, start: start || '09:00', end: end || '20:00' }
    } else if (wins && wins.length === 0) {
      base[d.key] = { ...base[d.key], open: false }
    }
  }
  return base
}

function serialiseBusinessHours(bh: FormState['business_hours']): Record<string, string[]> {
  const out: Record<string, string[]> = {}
  for (const d of WEEKDAYS) {
    const v = bh[d.key]
    out[d.key] = v.open ? [`${v.start}-${v.end}`] : []
  }
  return out
}

export default function StoreForm({
  initial, onSubmit, onCancel, submitLabel = 'Save',
}: {
  initial?: Partial<Store>
  onSubmit: (data: Partial<Store>) => Promise<void> | void
  onCancel?: () => void
  submitLabel?: string
}) {
  const [form, setForm] = useState<FormState>({
    name:    initial?.name    ?? '',
    code:    initial?.code    ?? '',
    country: initial?.country ?? 'Kenya',
    city:    initial?.city    ?? '',
    address: initial?.address ?? '',
    timezone: initial?.timezone ?? 'Africa/Nairobi',
    capacity: initial?.capacity ? String(initial.capacity) : '',
    manager_name:  initial?.manager_name  ?? '',
    manager_phone: initial?.manager_phone ?? '',
    default_rtsp_port: initial?.default_rtsp_port
      ? String(initial.default_rtsp_port) : '',
    business_hours: parseBusinessHours(initial?.business_hours_json ?? null),
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function save() {
    if (!form.name || !form.country) {
      setError('Store name and country are required'); return
    }
    setBusy(true); setError(null)
    try {
      await onSubmit({
        name: form.name,
        code: form.code || null,
        country: form.country,
        city: form.city || null,
        address: form.address || null,
        timezone: form.timezone,
        capacity: form.capacity ? Number(form.capacity) : null,
        manager_name:  form.manager_name  || null,
        manager_phone: form.manager_phone || null,
        default_rtsp_port: form.default_rtsp_port
          ? Number(form.default_rtsp_port) : null,
        business_hours_json: serialiseBusinessHours(form.business_hours),
      })
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  const upd = (k: keyof FormState) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm({ ...form, [k]: e.target.value })

  return (
    <Card className="p-6 max-w-3xl">
      <div className="text-lg font-semibold mb-4">
        {initial?.id ? 'Edit store' : 'New store'}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Field label="Store name *">
          <Input value={form.name} onChange={upd('name')} placeholder="Vivo Junction" />
        </Field>
        <Field label="Code (optional)">
          <Input value={form.code} onChange={upd('code')} placeholder="NRB-001" />
        </Field>
        <Field label="Country *">
          <Select value={form.country} onChange={upd('country')}>
            {COUNTRIES.map(c => <option key={c} value={c}>{c}</option>)}
          </Select>
        </Field>
        <Field label="City">
          <Input value={form.city} onChange={upd('city')} placeholder="Nairobi" />
        </Field>
        <Field label="Address" full>
          <Input value={form.address} onChange={upd('address')} placeholder="Junction Mall, Ngong Rd" />
        </Field>
        <Field label="Timezone">
          <Select value={form.timezone} onChange={upd('timezone')}>
            {TIMEZONES.map(t => <option key={t} value={t}>{t}</option>)}
          </Select>
        </Field>
        <Field label="Capacity (max people in store)">
          <Input type="number" value={form.capacity} onChange={upd('capacity')} placeholder="200" />
        </Field>
        <Field label="Manager name">
          <Input value={form.manager_name} onChange={upd('manager_name')} placeholder="Jane Wanjiku" />
        </Field>
        <Field label="Manager WhatsApp / phone">
          <Input value={form.manager_phone} onChange={upd('manager_phone')}
                 placeholder="+254712345678" />
        </Field>
        <Field label="Default NVR port (RTSP)" full>
          <Select value={form.default_rtsp_port}
                  onChange={upd('default_rtsp_port')}>
            <option value="">— use brand default (Dahua=7000, others=554) —</option>
            <option value="7000">7000 (most Dahua NVRs)</option>
            <option value="554">554 (standard RTSP)</option>
            <option value="80">80 (HTTP tunnel)</option>
            <option value="800">800</option>
            <option value="8000">8000 (Hikvision ISAPI)</option>
            <option value="8080">8080</option>
          </Select>
          <div className="text-xs text-slate-500 mt-1">
            Cameras added to this store will inherit this port. Pick
            7000 for Dahua-on-7000 stores (Moi Avenue, Junction,
            Sarit and most others in the Vivo fleet).
          </div>
        </Field>
      </div>

      {/* Business hours per day */}
      <div className="mt-6">
        <div className="text-sm font-medium mb-2">Business hours</div>
        <div className="space-y-1">
          {WEEKDAYS.map(d => {
            const v = form.business_hours[d.key]
            return (
              <div key={d.key} className="flex items-center gap-2 text-sm">
                <div className="w-12 font-medium">{d.label}</div>
                <label className="flex items-center gap-1">
                  <input type="checkbox" checked={v.open}
                         onChange={e => setForm({
                           ...form,
                           business_hours: {
                             ...form.business_hours,
                             [d.key]: { ...v, open: e.target.checked },
                           },
                         })} />
                  <span>open</span>
                </label>
                {v.open && (
                  <>
                    <Input type="time" value={v.start}
                           onChange={e => setForm({
                             ...form,
                             business_hours: {
                               ...form.business_hours,
                               [d.key]: { ...v, start: e.target.value },
                             },
                           })} />
                    <span>to</span>
                    <Input type="time" value={v.end}
                           onChange={e => setForm({
                             ...form,
                             business_hours: {
                               ...form.business_hours,
                               [d.key]: { ...v, end: e.target.value },
                             },
                           })} />
                  </>
                )}
                {!v.open && <span className="text-slate-400">closed</span>}
              </div>
            )
          })}
        </div>
      </div>

      {error && <div className="text-red-600 text-sm mt-3">{error}</div>}

      <div className="mt-6 flex justify-end gap-2">
        {onCancel && <Button variant="ghost" onClick={onCancel}>Cancel</Button>}
        <Button onClick={save} disabled={busy || !form.name || !form.country}>
          {busy ? 'Saving…' : submitLabel}
        </Button>
      </div>
    </Card>
  )
}

function Field({ label, children, full }: { label: string; children: React.ReactNode; full?: boolean }) {
  return (
    <label className={'block ' + (full ? 'md:col-span-2' : '')}>
      <div className="text-xs text-slate-600 mb-1">{label}</div>
      {children}
    </label>
  )
}
