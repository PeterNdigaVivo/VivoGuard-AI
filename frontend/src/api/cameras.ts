// /cameras/* and /nvr/* wrappers.
import { api } from './client'

export interface Camera {
  id: number; name: string; site: string | null; brand: string
  connection_type: string; host: string; public_ip: string | null
  rtsp_port: number; http_port: number; nvr_id: number | null
  channel_number: number | null; network_type: string
  ai_model_id: number | null; ai_enabled: boolean; inference_fps: number
  status: string; last_seen_at: string | null; last_error: string | null
  // Store-first onboarding: every newly-created camera has a store.
  // Legacy rows can still be null until an operator attaches them.
  store_id: number | null
  // Transport: 'rtsp' (default) or 'http_snapshot' (Dahua CGI polling
  // — use when the store router doesn't forward port 554).
  transport: 'rtsp' | 'http_snapshot'
  snapshot_url_override: string | null
  created_at: string
}

export interface TestConnectionOut {
  ok: boolean; rtsp_url: string | null; snapshot_jpeg_b64: string | null
  detected_channels: number; device_model: string | null; error: string | null
}

export interface NVRChannel { channel: number; name: string; rtsp_main: string; rtsp_sub: string }

export const cameras = {
  list:      () => api<Camera[]>('/cameras'),
  get:       (id: number) => api<Camera>(`/cameras/${id}`),
  add:       (body: Record<string, unknown>) => api<Camera>('/cameras/add', { method: 'POST', body }),
  update:    (id: number, body: Record<string, unknown>) =>
    api<Camera>(`/cameras/${id}`, { method: 'PATCH', body }),
  remove:    (id: number) => api(`/cameras/${id}`, { method: 'DELETE' }),
  test:      (body: Record<string, unknown>) =>
    api<TestConnectionOut>('/cameras/test-connection', { method: 'POST', body }),
  discoverONVIF: () => api<Array<{ endpoint: string; scopes: string[]; types: string[] }>>('/cameras/discover-onvif'),
  snapshot:  (id: number) => api<{ jpeg_b64: string }>(`/cameras/${id}/snapshot`),

  // detection config + zones
  getDetectionConfig: (id: number) => api<any[]>(`/cameras/${id}/detection-config`),
  upsertDetectionConfig: (id: number, payload: any[]) =>
    api<any[]>(`/cameras/${id}/detection-config`, { method: 'POST', body: payload }),
  listZones: (id: number) => api<any[]>(`/cameras/${id}/zones`),
  createZone: (id: number, body: any) =>
    api<any>(`/cameras/${id}/zones`, { method: 'POST', body }),
  deleteZone: (id: number, zoneId: number) =>
    api(`/cameras/${id}/zones/${zoneId}`, { method: 'DELETE' }),
}

export const nvr = {
  connect: (body: Record<string, unknown>) =>
    api<{ nvr_id: number; device_model: string | null; channels: NVRChannel[] }>(
      '/nvr/connect', { method: 'POST', body }),
  channels: (id: number) => api<NVRChannel[]>(`/nvr/${id}/channels`),
  addChannels: (id: number, channels: NVRChannel[]) =>
    api<Camera[]>(`/nvr/${id}/add-channels`, {
      method: 'POST',
      body: { nvr_id: id, device_model: null, channels },
    }),
}
