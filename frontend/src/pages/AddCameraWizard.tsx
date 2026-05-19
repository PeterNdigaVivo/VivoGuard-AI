// Add-camera wizard — Store-first onboarding.
//
//   Step 0: pick a store (mandatory). If the URL is /stores/:id/add-camera
//           the store is pre-locked. If no stores exist we surface a
//           prominent CTA to create one first.
//   Step 1: pick connection type (single IP camera vs NVR multi-channel)
//   Step 2: enter host / credentials
//   Step 3: test → preview thumbnail → save
//           (NVR: choose channels → bulk-add to the selected store)

import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { Badge, Button, Card, Input, PageHeader, Select } from '@/components/ui/Primitives'
import { cameras as camsApi, nvr, type NVRChannel, type TestConnectionOut } from '@/api/cameras'
import { stores as storesApi, type Store } from '@/api/stores'
import { api } from '@/api/client'

const TYPES: Array<{ value: string; label: string; isNvr?: boolean; brand: string }> = [
  { value: 'lan_rtsp',   label: 'LAN IP camera (RTSP)',                 brand: 'generic'   },
  { value: 'wan_rtsp',   label: 'WAN IP camera via public IP (RTSP)',   brand: 'generic'   },
  { value: 'wan_dahua',  label: 'Dahua camera over WAN (HTTP+RTSP)',    brand: 'dahua'     },
  { value: 'wan_hik',    label: 'Hikvision camera over WAN (ISAPI+RTSP)',brand: 'hikvision' },
  { value: 'onvif',      label: 'ONVIF camera (auto)',                  brand: 'onvif'     },
  { value: 'nvr_dahua',  label: 'Dahua NVR (multi-channel)',            brand: 'dahua',     isNvr: true },
  { value: 'nvr_hik',    label: 'Hikvision NVR (multi-channel)',        brand: 'hikvision', isNvr: true },
]

export default function AddCameraWizard() {
  const nav = useNavigate()
  const params = useParams()                  // when nested under /stores/:id
  const [search] = useSearchParams()           // legacy ?store_id=
  const presetStoreId = Number(params.id ?? search.get('store_id') ?? 0) || null

  const [stores, setStores] = useState<Store[] | null>(null)
  const [storeId, setStoreId] = useState<number | ''>(presetStoreId ?? '')
  const [step, setStep] = useState<number>(presetStoreId ? 1 : 0)

  const [type, setType] = useState(TYPES[0])
  const [form, setForm] = useState({
    name: '', host: '', sdk_port: '', rtsp_port: 554, http_port: 80,
    username: 'admin', password: '', channel_number: 1,
    rtsp_url_override: '', ddns_hostname: '', network_type: 'lan',
  })
  const upd = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm({ ...form, [k]: (e.target as HTMLInputElement).type === 'number' ? Number(e.target.value) : e.target.value })

  const [test, setTest] = useState<TestConnectionOut | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // NVR-specific.
  const [nvrChannels, setNvrChannels] = useState<NVRChannel[] | null>(null)
  const [pickedChannels, setPickedChannels] = useState<Set<number>>(new Set())
  const [nvrId, setNvrId] = useState<number | null>(null)

  useEffect(() => { storesApi.list().then(setStores).catch(console.error) }, [])

  async function doTest() {
    setBusy(true); setError(null); setTest(null)
    try {
      const r = await camsApi.test({
        brand: type.brand, connection_type: type.value, host: form.host,
        rtsp_port: form.rtsp_port, http_port: form.http_port,
        username: form.username, password: form.password,
        channel_number: form.channel_number, rtsp_url_override: form.rtsp_url_override || null,
      })
      setTest(r)
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }

  // "Auto-detect ports" — calls /cameras/intelligent-probe to find
  // which RTSP and HTTP ports respond on this host, then pre-fills
  // the form so operators don't have to remember per-NVR-firmware
  // quirks (Moi Ave uses HTTP=7000, etc.).
  type ProbeResult = {
    host: string
    rtsp_port: number | null
    http_port: number | null
    rtsp_candidates: number[]
    http_candidates: number[]
    recommended_transport: 'rtsp' | 'http_snapshot' | null
  }
  const [probe, setProbe] = useState<ProbeResult | null>(null)
  async function autoDetectPorts() {
    if (!form.host) { setError('Enter the host first.'); return }
    setBusy(true); setError(null); setProbe(null)
    try {
      const r = await api<ProbeResult>('/cameras/intelligent-probe', {
        method: 'POST',
        body: {
          host: form.host, username: form.username,
          password: form.password, channel_number: form.channel_number,
        },
      })
      setProbe(r)
      // Pre-fill whichever ports responded. If both worked, prefer
      // RTSP (lower bandwidth) and keep the discovered HTTP port as
      // a fallback for the streamer's auto-failover.
      setForm(f => ({
        ...f,
        rtsp_port: r.rtsp_port ?? f.rtsp_port,
        http_port: r.http_port ?? f.http_port,
      }))
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function connectNVR() {
    setBusy(true); setError(null)
    try {
      const r = await nvr.connect({
        name: form.name || `${type.brand} NVR`,
        brand: type.brand, host: form.host,
        sdk_port: form.sdk_port ? Number(form.sdk_port) : null,
        rtsp_port: form.rtsp_port, http_port: form.http_port,
        username: form.username, password: form.password,
        network_type: form.network_type,
      })
      setNvrId(r.nvr_id); setNvrChannels(r.channels)
      setPickedChannels(new Set(r.channels.map(c => c.channel)))
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }

  async function saveCamera() {
    if (!storeId) { setError('Pick a store before saving.'); return }
    setBusy(true); setError(null)
    try {
      await camsApi.add({
        store_id: storeId,
        name: form.name,
        brand: type.brand, connection_type: type.value,
        host: form.host, public_ip: form.network_type !== 'lan' ? form.host : null,
        sdk_port: form.sdk_port ? Number(form.sdk_port) : null,
        rtsp_port: form.rtsp_port, http_port: form.http_port,
        username: form.username, password: form.password,
        channel_number: form.channel_number,
        rtsp_url_override: form.rtsp_url_override || null,
        ddns_hostname: form.ddns_hostname || null,
        network_type: form.network_type, ai_enabled: true, inference_fps: 5,
      })
      nav(`/stores/${storeId}`)
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }

  async function saveNVRChannels() {
    if (!nvrId || !nvrChannels) return
    if (!storeId) { setError('Pick a store before saving.'); return }
    setBusy(true); setError(null)
    try {
      const picked = nvrChannels.filter(c => pickedChannels.has(c.channel))
      // The legacy API accepted a free-form body; the new shape includes
      // store_id at the top level.
      await api(`/nvr/${nvrId}/add-channels`, {
        method: 'POST',
        body: { nvr_id: nvrId, store_id: storeId, channels: picked },
      })
      nav(`/stores/${storeId}`)
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }

  // ----- Step 0: pick a store -----
  if (step === 0) {
    if (stores === null) return <div className="p-6 text-slate-500">Loading…</div>
    if (stores.length === 0) {
      return (
        <div className="p-6 max-w-2xl">
          <PageHeader title="Add a camera" />
          <Card className="p-8 text-center">
            <div className="text-5xl mb-2">🏬</div>
            <div className="text-lg font-medium mb-1">Create a store first</div>
            <div className="text-slate-500 mb-4">
              Cameras attach to stores. Add at least one store before adding cameras.
            </div>
            <Link to="/stores"><Button>Go to Stores</Button></Link>
          </Card>
        </div>
      )
    }
    return (
      <div className="p-6 max-w-2xl">
        <PageHeader title="Add a camera" actions={
          <Button variant="ghost" onClick={() => nav('/cameras')}>Cancel</Button>
        } />
        <Card className="p-4">
          <div className="text-sm font-medium mb-2">1. Which store is this camera for?</div>
          <Select className="w-full" value={storeId}
                  onChange={e => setStoreId(Number(e.target.value) || '')}>
            <option value="">— pick a store —</option>
            {stores.map(s => (
              <option key={s.id} value={s.id}>
                {s.name} — {s.city ?? '—'}, {s.country}
              </option>
            ))}
          </Select>
          <div className="mt-4 text-right">
            <Button onClick={() => setStep(1)} disabled={!storeId}>Next →</Button>
          </div>
        </Card>
      </div>
    )
  }

  const lockedStore = stores?.find(s => s.id === storeId)

  return (
    <div className="p-6 max-w-3xl">
      <PageHeader title={lockedStore ? `Add a camera to ${lockedStore.name}` : 'Add a camera'} actions={
        <Button variant="ghost" onClick={() => storeId ? nav(`/stores/${storeId}`) : nav('/cameras')}>
          Cancel
        </Button>
      } />

      {/* Locked-store strip */}
      {lockedStore && (
        <div className="mb-4 text-sm text-slate-500">
          📍 {lockedStore.name} · {lockedStore.city ?? '—'}, {lockedStore.country}
        </div>
      )}

      {/* Step 1: pick type */}
      {step === 1 && (
        <Card className="p-4">
          <div className="font-medium mb-3">2. Connection type</div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
            {TYPES.map(t => (
              <label key={t.value}
                     className={'p-3 rounded border cursor-pointer ' +
                       (type.value === t.value ? 'border-sky-500 bg-sky-50' : 'border-slate-200')}>
                <input type="radio" name="type" className="mr-2"
                       checked={type.value === t.value}
                       onChange={() => setType(t)} />
                {t.label}
                {t.isNvr && <Badge color="amber">NVR</Badge>}
              </label>
            ))}
          </div>
          <div className="mt-4 flex justify-between">
            <Button variant="ghost" onClick={() => setStep(0)}>← Store</Button>
            <Button onClick={() => setStep(2)}>Next →</Button>
          </div>
        </Card>
      )}

      {/* Step 2: details */}
      {step === 2 && (
        <Card className="p-4">
          <div className="font-medium mb-3">3. Connection details</div>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Camera name *">
              <Input value={form.name} onChange={upd('name')}
                     placeholder={type.isNvr ? 'Junction NVR' : 'Entrance Camera'} />
            </Field>
            <Field label="Host (IP or DDNS) *">
              <Input value={form.host} onChange={upd('host')} placeholder="41.90.110.206" />
            </Field>
            <Field label="Network">
              <Select value={form.network_type}
                      onChange={(e) => setForm({ ...form, network_type: e.target.value })}>
                <option value="lan">LAN</option>
                <option value="wan">WAN (public IP / DDNS)</option>
                <option value="vpn">VPN</option>
              </Select>
            </Field>
            <Field label="RTSP port"><Input type="number" value={form.rtsp_port} onChange={upd('rtsp_port')} /></Field>
            <Field label="HTTP port"><Input type="number" value={form.http_port} onChange={upd('http_port')} /></Field>

            <div className="col-span-2 -mt-1 mb-1">
              <Button variant="ghost" onClick={autoDetectPorts}
                      disabled={busy || !form.host}>
                {busy ? 'Probing…' : '🔍 Auto-detect ports'}
              </Button>
              <span className="ml-2 text-xs text-slate-500">
                Probes RTSP 554/10554/5544/8554 and HTTP 80/8080/8000/800/7000.
              </span>
              {probe && (
                <div className="mt-2 text-xs">
                  {probe.rtsp_candidates.length > 0 && (
                    <div className="text-emerald-700">
                      ✓ RTSP responded on: {probe.rtsp_candidates.join(', ')}
                    </div>
                  )}
                  {probe.http_candidates.length > 0 && (
                    <div className="text-emerald-700">
                      ✓ HTTP snapshot responded on: {probe.http_candidates.join(', ')}
                    </div>
                  )}
                  {!probe.rtsp_candidates.length && !probe.http_candidates.length && (
                    <div className="text-red-600">
                      ✗ Nothing answered. Verify the host is reachable and the router
                      forwards at least one of the common ports.
                    </div>
                  )}
                  {probe.recommended_transport === 'http_snapshot' && (
                    <div className="text-amber-700 mt-1">
                      RTSP is blocked but HTTP works — this camera will auto-switch
                      to HTTP snapshot polling after save.
                    </div>
                  )}
                </div>
              )}
            </div>
            <Field label="SDK port (optional)">
              <Input type="number" value={form.sdk_port} onChange={upd('sdk_port')} placeholder="" />
            </Field>
            {!type.isNvr && (
              <Field label="Channel number (for NVR-attached cameras)">
                <Input type="number" value={form.channel_number} onChange={upd('channel_number')} />
              </Field>
            )}
            <Field label="Username"><Input value={form.username} onChange={upd('username')} /></Field>
            <Field label="Password"><Input type="password" value={form.password} onChange={upd('password')} /></Field>
            <Field label="RTSP URL override (optional)" full>
              <Input value={form.rtsp_url_override} onChange={upd('rtsp_url_override')}
                     placeholder="rtsp://user:pass@host:554/..." />
            </Field>
            <Field label="DDNS hostname (optional)" full>
              <Input value={form.ddns_hostname} onChange={upd('ddns_hostname')}
                     placeholder="mycamera.dyndns.org" />
            </Field>
          </div>

          <div className="mt-4 flex justify-between">
            <Button variant="ghost" onClick={() => setStep(1)}>← Type</Button>
            <Button onClick={() => { setStep(3); type.isNvr ? connectNVR() : doTest() }}
                    disabled={!form.host || !form.username || !form.name}>
              {type.isNvr ? 'Connect NVR' : 'Test connection'}
            </Button>
          </div>
        </Card>
      )}

      {/* Step 3: result */}
      {step === 3 && !type.isNvr && (
        <Card className="p-4">
          <div className="font-medium mb-3">4. Test result</div>
          {busy && <div className="text-slate-500">Probing…</div>}
          {error && <div className="text-red-600">{error}</div>}
          {test && (
            <>
              <div className="mb-3">
                {test.ok ? <Badge color="green">OK</Badge> : <Badge color="red">Failed</Badge>}
                {test.device_model && (
                  <span className="ml-2 text-sm text-slate-600">model: {test.device_model}</span>
                )}
              </div>
              {test.snapshot_jpeg_b64 && (
                <img src={`data:image/jpeg;base64,${test.snapshot_jpeg_b64}`}
                     alt="thumbnail" className="rounded border max-h-72" />
              )}
              {test.rtsp_url && (
                <div className="mt-3 font-mono text-xs break-all bg-slate-50 p-2 rounded border">
                  {test.rtsp_url}
                </div>
              )}
              {!test.ok && test.error && <div className="text-red-600 text-sm mt-2">{test.error}</div>}
            </>
          )}
          <div className="mt-4 flex justify-between">
            <Button variant="ghost" onClick={() => setStep(2)}>← Edit</Button>
            <Button onClick={saveCamera} disabled={busy || !form.name}>Save camera</Button>
          </div>
        </Card>
      )}
      {step === 3 && type.isNvr && (
        <Card className="p-4">
          <div className="font-medium mb-3">4. NVR channels</div>
          {busy && <div className="text-slate-500">Connecting…</div>}
          {error && <div className="text-red-600">{error}</div>}
          {nvrChannels && (
            <div className="space-y-1 max-h-96 overflow-auto">
              {nvrChannels.map(c => (
                <label key={c.channel} className="flex items-center gap-2 p-2 hover:bg-slate-50 rounded">
                  <input type="checkbox" checked={pickedChannels.has(c.channel)}
                    onChange={(e) => {
                      const next = new Set(pickedChannels)
                      e.target.checked ? next.add(c.channel) : next.delete(c.channel)
                      setPickedChannels(next)
                    }} />
                  <span className="font-medium">Ch {c.channel}</span>
                  <span className="text-slate-500">{c.name}</span>
                  <span className="ml-auto font-mono text-xs text-slate-400 truncate" title={c.rtsp_main}>
                    {c.rtsp_main}
                  </span>
                </label>
              ))}
            </div>
          )}
          <div className="mt-4 flex justify-between">
            <Button variant="ghost" onClick={() => setStep(2)}>← Edit</Button>
            <Button onClick={saveNVRChannels} disabled={busy || !nvrId || pickedChannels.size === 0}>
              Add {pickedChannels.size} channel{pickedChannels.size === 1 ? '' : 's'}
            </Button>
          </div>
        </Card>
      )}
    </div>
  )
}

function Field({ label, children, full }: { label: string; children: React.ReactNode; full?: boolean }) {
  return (
    <label className={'block ' + (full ? 'col-span-2' : '')}>
      <div className="text-xs text-slate-600 mb-1">{label}</div>
      {children}
    </label>
  )
}
