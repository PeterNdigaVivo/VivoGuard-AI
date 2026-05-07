// Add Camera wizard:
//   step 1: pick connection type
//   step 2: enter host/credentials
//   step 3: test → preview thumbnail → save (or pick channels for an NVR)

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Badge, Button, Card, Input, PageHeader, Select } from '@/components/ui/Primitives'
import { cameras as camsApi, nvr, type NVRChannel, type TestConnectionOut } from '@/api/cameras'

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
  const [step, setStep] = useState(1)

  const [type, setType] = useState(TYPES[0])
  const [form, setForm] = useState({
    name: '', site: '', host: '', sdk_port: '', rtsp_port: 554, http_port: 80,
    username: 'admin', password: '', channel_number: 1,
    rtsp_url_override: '', ddns_hostname: '', network_type: 'lan',
  })
  const upd = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm({ ...form, [k]: (e.target as HTMLInputElement).type === 'number' ? Number(e.target.value) : e.target.value })

  const [test, setTest] = useState<TestConnectionOut | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // NVR-specific state.
  const [nvrChannels, setNvrChannels] = useState<NVRChannel[] | null>(null)
  const [pickedChannels, setPickedChannels] = useState<Set<number>>(new Set())
  const [nvrId, setNvrId] = useState<number | null>(null)

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
    setBusy(true); setError(null)
    try {
      await camsApi.add({
        name: form.name, site: form.site || null,
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
      nav('/cameras')
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }

  async function saveNVRChannels() {
    if (!nvrId || !nvrChannels) return
    setBusy(true); setError(null)
    try {
      const picked = nvrChannels.filter(c => pickedChannels.has(c.channel))
      await nvr.addChannels(nvrId, picked)
      nav('/cameras')
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }

  return (
    <div className="p-6 max-w-3xl">
      <PageHeader title="Add Camera" actions={
        <Button variant="ghost" onClick={() => nav('/cameras')}>Cancel</Button>
      } />

      {/* Step 1: pick type */}
      {step === 1 && (
        <Card className="p-4">
          <div className="font-medium mb-3">Connection type</div>
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
          <div className="mt-4 text-right">
            <Button onClick={() => setStep(2)}>Next →</Button>
          </div>
        </Card>
      )}

      {/* Step 2: details */}
      {step === 2 && (
        <Card className="p-4">
          <div className="font-medium mb-3">Connection details</div>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Name"><Input value={form.name} onChange={upd('name')} /></Field>
            <Field label="Site / Location"><Input value={form.site} onChange={upd('site')} /></Field>
            <Field label="Host (IP or DDNS)"><Input value={form.host} onChange={upd('host')} placeholder="41.90.110.206" /></Field>
            <Field label="Network">
              <Select value={form.network_type} onChange={(e) => setForm({ ...form, network_type: e.target.value })}>
                <option value="lan">LAN</option>
                <option value="wan">WAN (public IP / DDNS)</option>
                <option value="vpn">VPN</option>
              </Select>
            </Field>
            <Field label="RTSP port"><Input type="number" value={form.rtsp_port} onChange={upd('rtsp_port')} /></Field>
            <Field label="HTTP port"><Input type="number" value={form.http_port} onChange={upd('http_port')} /></Field>
            <Field label="SDK port (optional, e.g. 7000 Dahua / 8000 Hik)">
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
              <Input value={form.rtsp_url_override} onChange={upd('rtsp_url_override')} placeholder="rtsp://user:pass@host:554/..." />
            </Field>
            <Field label="DDNS hostname (optional)" full>
              <Input value={form.ddns_hostname} onChange={upd('ddns_hostname')} placeholder="mycamera.dyndns.org" />
            </Field>
          </div>

          <div className="mt-4 flex justify-between">
            <Button variant="ghost" onClick={() => setStep(1)}>← Back</Button>
            <Button onClick={() => { setStep(3); type.isNvr ? connectNVR() : doTest() }}
                    disabled={!form.host || !form.username}>
              {type.isNvr ? 'Connect NVR' : 'Test connection'}
            </Button>
          </div>
        </Card>
      )}

      {/* Step 3a: regular camera result */}
      {step === 3 && !type.isNvr && (
        <Card className="p-4">
          <div className="font-medium mb-3">Test result</div>
          {busy && <div className="text-slate-500">Probing…</div>}
          {error && <div className="text-red-600">{error}</div>}
          {test && (
            <>
              <div className="mb-3">
                {test.ok
                  ? <Badge color="green">OK</Badge>
                  : <Badge color="red">Failed</Badge>}
                {test.device_model && <span className="ml-2 text-sm text-slate-600">model: {test.device_model}</span>}
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
            <Button variant="ghost" onClick={() => setStep(2)}>← Back</Button>
            <Button onClick={saveCamera} disabled={busy || !form.name}>Save camera</Button>
          </div>
        </Card>
      )}

      {/* Step 3b: NVR result */}
      {step === 3 && type.isNvr && (
        <Card className="p-4">
          <div className="font-medium mb-3">NVR channels</div>
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
            <Button variant="ghost" onClick={() => setStep(2)}>← Back</Button>
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
