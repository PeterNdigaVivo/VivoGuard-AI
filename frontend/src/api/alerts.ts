// /alerts/* wrappers.
import { api, wsUrl } from './client'

export interface Alert {
  id: number; event_id: number; status: string
  acknowledged_at: string | null; created_at: string
  camera_id: number | null; camera_name: string | null
  detection_type: string | null; confidence: number | null
  bbox_norm: number[] | null; zone_id: number | null
  thumbnail_path: string | null
}

export const alerts = {
  list:    (params: Record<string, string | number | undefined> = {}) => {
    const q = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== '') q.set(k, String(v))
    return api<Alert[]>(`/alerts${q.toString() ? `?${q}` : ''}`)
  },
  confirm: (id: number) => api<{ id: number; status: string }>(`/alerts/${id}/confirm`, { method: 'POST' }),
  dismiss: (id: number) => api<{ id: number; status: string }>(`/alerts/${id}/dismiss`, { method: 'POST' }),

  // Live alerts WebSocket
  subscribe: (onEvent: (data: any) => void) => {
    const ws = new WebSocket(wsUrl('/ws/alerts'))
    ws.onmessage = e => { try { onEvent(JSON.parse(e.data)) } catch {} }
    return () => ws.close()
  },
}
