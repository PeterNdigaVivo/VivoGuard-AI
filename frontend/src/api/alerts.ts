// /alerts/* wrappers.
import { api, wsUrl } from './client'

export interface Alert {
  id: number; event_id: number; status: string
  acknowledged_at: string | null
  resolved_at: string | null
  notes: string | null
  created_at: string
  camera_id: number | null; camera_name: string | null
  detection_type: string | null; confidence: number | null
  bbox_norm: number[] | null
  zone_id: number | null; zone_name: string | null
  thumbnail_path: string | null
  // May-2026 redesign — server-rendered presentation fields. The
  // frontend stops translating detection_type strings; it just
  // renders these.
  severity: 'critical' | 'warning' | 'info' | null
  title: string | null
  body:  string | null
  snapshot_url: string | null
}

export const alerts = {
  list:    (params: Record<string, string | number | undefined> = {}) => {
    const q = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== '') q.set(k, String(v))
    return api<Alert[]>(`/alerts${q.toString() ? `?${q}` : ''}`)
  },
  confirm: (id: number) => api<{ id: number; status: string }>(`/alerts/${id}/confirm`, { method: 'POST' }),
  dismiss: (id: number) => api<{ id: number; status: string }>(`/alerts/${id}/dismiss`, { method: 'POST' }),
  // Resolved is the everyday "I handled it" action — distinct from
  // confirm (which also feeds ML training as a true positive).
  resolve: (id: number) => api<{ id: number; status: string }>(`/alerts/${id}/resolve`, { method: 'POST' }),
  // Append an investigation note. Server timestamps + author-stamps
  // each entry so the trail reads chronologically.
  addNote: (id: number, note: string) =>
    api<Alert>(`/alerts/${id}/note`, { method: 'POST', body: { note } }),

  // Live alerts WebSocket
  subscribe: (onEvent: (data: any) => void) => {
    const ws = new WebSocket(wsUrl('/ws/alerts'))
    ws.onmessage = e => { try { onEvent(JSON.parse(e.data)) } catch {} }
    return () => ws.close()
  },
}
