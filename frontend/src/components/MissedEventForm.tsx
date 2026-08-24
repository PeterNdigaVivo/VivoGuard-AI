import { type FormEvent, useEffect, useMemo, useState } from 'react'
import { cameras as camerasApi, type Camera } from '@/api/cameras'
import { operations, type AssuranceCase, type MissedEventResult } from '@/api/operations'
import { stores as storesApi, type Store } from '@/api/stores'
import { Button, Card, Input, Select } from '@/components/ui/Primitives'

const LABELS = [
  'intrusion', 'staff_zone', 'stockroom_access', 'tailgating',
  'shop_open_close', 'crowd', 'fall', 'fight', 'weapon', 'fire',
  'smoke', 'shrinkage',
]

function localDateTimeNow() {
  const now = new Date()
  now.setMinutes(now.getMinutes() - now.getTimezoneOffset())
  return now.toISOString().slice(0, 16)
}

function newSourceRef() {
  return `manual-${new Date().toISOString()}-${crypto.randomUUID()}`
}

export default function MissedEventForm({ onClose }: { onClose: () => void }) {
  const [stores, setStores] = useState<Store[]>([])
  const [cameras, setCameras] = useState<Camera[]>([])
  const [storeId, setStoreId] = useState('')
  const [cameraId, setCameraId] = useState('')
  const [occurredAt, setOccurredAt] = useState(localDateTimeNow)
  const [label, setLabel] = useState('intrusion')
  const [reportText, setReportText] = useState('')
  const [sourceRef, setSourceRef] = useState(newSourceRef)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<MissedEventResult | null>(null)
  const [cases, setCases] = useState<AssuranceCase[]>([])

  useEffect(() => {
    Promise.all([
      storesApi.list(), camerasApi.list(),
      operations.listMissedEvents().catch(() => []),
    ])
      .then(([storeRows, cameraRows, caseRows]) => {
        setStores(storeRows.filter(store => store.is_active))
        setCameras(cameraRows)
        setCases(caseRows)
      })
      .catch(err => setError(String(err)))
  }, [])

  const storeCameras = useMemo(
    () => cameras.filter(camera => String(camera.store_id) === storeId),
    [cameras, storeId],
  )

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setResult(null)
    if (!storeId || !occurredAt || reportText.trim().length < 4) {
      setError('Store, incident time and a clear description are required.')
      return
    }
    setBusy(true)
    try {
      const response = await operations.reportMissedEvent({
        source: 'manual',
        source_ref: sourceRef,
        store_id: Number(storeId),
        camera_id: cameraId ? Number(cameraId) : null,
        occurred_at: new Date(occurredAt).toISOString(),
        report_text: reportText.trim(),
        label: label.trim(),
        match_window_seconds: 120,
      })
      setResult(response)
      operations.listMissedEvents().then(setCases).catch(() => undefined)
      setReportText('')
      setSourceRef(newSourceRef())
    } catch (err) {
      setError(String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card className="p-4 mb-4 border-amber-300 bg-amber-50 dark:bg-slate-900">
      <div className="flex items-start justify-between gap-4 mb-3">
        <div>
          <div className="font-semibold text-slate-900 dark:text-slate-100">Report a missed or late alert</div>
          <div className="text-sm text-slate-600 dark:text-slate-300">
            Use this when a real incident was seen by a person but VivoGuard did not alert correctly. This creates recall evidence; it does not automatically train or accuse anyone.
          </div>
        </div>
        <button type="button" onClick={onClose} className="text-sm text-slate-500 hover:text-slate-900">Close</button>
      </div>

      <form onSubmit={submit} className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <label className="text-sm">Store
          <Select value={storeId} onChange={event => { setStoreId(event.target.value); setCameraId('') }} className="mt-1 w-full" required>
            <option value="">Select store</option>
            {stores.map(store => <option key={store.id} value={store.id}>{store.name}</option>)}
          </Select>
        </label>
        <label className="text-sm">Camera, if known
          <Select value={cameraId} onChange={event => setCameraId(event.target.value)} className="mt-1 w-full" disabled={!storeId}>
            <option value="">Unknown / camera not confirmed</option>
            {storeCameras.map(camera => <option key={camera.id} value={camera.id}>{camera.name}</option>)}
          </Select>
        </label>
        <label className="text-sm">Incident time
          <Input type="datetime-local" value={occurredAt} onChange={event => setOccurredAt(event.target.value)} className="mt-1 w-full" required />
        </label>
        <label className="text-sm">Expected alert type
          <Input list="missed-event-labels" value={label} onChange={event => setLabel(event.target.value)} className="mt-1 w-full" required />
          <datalist id="missed-event-labels">{LABELS.map(value => <option key={value} value={value} />)}</datalist>
        </label>
        <label className="text-sm md:col-span-2">What happened and what alert was expected?
          <textarea value={reportText} onChange={event => setReportText(event.target.value)} rows={3} maxLength={4000} required
            className="mt-1 w-full bg-white border border-slate-300 rounded px-2 py-1 text-sm dark:bg-slate-800 dark:border-slate-600 dark:text-slate-100 focus:ring-2 ring-sky-500 outline-none"
            placeholder="Example: A person entered the stockroom at 14:32, but no staff-area alert appeared. Do not include customer names or biometric data." />
        </label>
        <div className="md:col-span-2 flex items-center gap-3">
          <Button type="submit" disabled={busy}>{busy ? 'Recording…' : 'Create missed-event case'}</Button>
          <span className="text-xs text-slate-500">Evidence remains quarantined until independently verified.</span>
        </div>
      </form>

      {error && <div className="mt-3 text-sm text-red-700">{error}</div>}
      {result && (
        <div className="mt-3 rounded border border-emerald-300 bg-emerald-50 p-3 text-sm text-emerald-900">
          Case #{result.case_id} created · root cause: {result.root_cause.replaceAll('_', ' ')} · {result.training_status.replaceAll('_', ' ')}
        </div>
      )}

      <div className="mt-4 border-t border-amber-200 pt-3">
        <div className="text-sm font-semibold mb-2">Recent missed-event cases</div>
        {cases.length === 0 ? (
          <div className="text-sm text-slate-500">No missed incidents have been recorded yet.</div>
        ) : (
          <div className="space-y-2 max-h-56 overflow-y-auto">
            {cases.map(item => (
              <div key={item.id} className="rounded border border-slate-200 bg-white p-2 text-sm dark:bg-slate-800 dark:border-slate-700">
                <div className="flex justify-between gap-3">
                  <span className="font-medium">Case #{item.id} · {item.title}</span>
                  <span className="text-xs uppercase text-slate-500">{item.status}</span>
                </div>
                <div className="text-xs text-slate-500 mt-1">
                  {stores.find(store => store.id === item.store_id)?.name ?? 'Store not mapped'}
                  {' · '}{(item.root_cause ?? 'pending investigation').replaceAll('_', ' ')}
                  {' · '}{(item.training_status ?? 'not assessed').replaceAll('_', ' ')}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </Card>
  )
}
