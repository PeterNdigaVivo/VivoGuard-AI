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
  // Non-technical traffic-light label: 'URGENT' | 'ATTENTION' | 'INFO'.
  severity_label: 'URGENT' | 'ATTENTION' | 'INFO' | null
  title: string | null
  // Plain-English heading with no camera suffix ("Staff Not in Uniform").
  plain_title: string | null
  body:  string | null
  // Up to 3 plain-English "what to do" steps.
  what_to_do: string[] | null
  // "🕒 Between 9:30 PM and 9:55 PM (25 min)" for duration events;
  // "🕒 Detected at 9:30 PM" for point-in-time events. Formatted in
  // the camera's store-local timezone.
  time_range: string | null
  snapshot_url: string | null
}

export const alerts = {
  list:    (params: Record<string, string | number | undefined> = {}) => {
    const q = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== '') q.set(k, String(v))
    return api<Alert[]>(`/alerts${q.toString() ? `?${q}` : ''}`)
  },
  // Quick counts for the header stats + sidebar badge.
  summary: (storeId?: number) =>
    api<{ urgent: number; attention: number; resolved_today: number; unread_urgent: number }>(
      `/alerts/summary${storeId ? `?store_id=${storeId}` : ''}`),
  confirm: (id: number) => api<{ id: number; status: string }>(`/alerts/${id}/confirm`, { method: 'POST' }),
  dismiss: (id: number) => api<{ id: number; status: string }>(`/alerts/${id}/dismiss`, { method: 'POST' }),
  // Resolved is the everyday "I handled it" action — distinct from
  // confirm (which also feeds ML training as a true positive).
  resolve: (id: number) => api<{ id: number; status: string }>(`/alerts/${id}/resolve`, { method: 'POST' }),
  // Bulk-resolve every still-new alert in the window. Mirrors the
  // server-side filters the list uses so the operator clears exactly
  // what's on screen, not the whole database.
  resolveAll: (params: { store_id?: string | number; since?: string; until?: string } = {}) => {
    const q = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== '') q.set(k, String(v))
    return api<{ resolved: number }>(`/alerts/resolve-all${q.toString() ? `?${q}` : ''}`, { method: 'POST' })
  },
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
