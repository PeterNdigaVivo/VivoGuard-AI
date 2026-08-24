// /labels/* — the Labelling Sprint API. See backend/app/api/labels.py.
import { api } from './client'
import type { Alert } from './alerts'

// Server splices dwell_seconds into the AlertOut payload (it's not a
// column on Alert — it comes from DetectionEvent.extra). N/A → null.
export interface SprintAlert extends Alert {
  dwell_seconds: number | null
}

export interface UndoResult {
  undone: boolean
  training_image_removed: boolean
}

export interface TodayCounts {
  reviewed_today: number
  queue_total:    number
}

export const labels = {
  queue: (params: { detection_type?: string; store_id?: number; camera_id?: number; limit?: number } = {}) => {
    const q = new URLSearchParams()
    if (params.detection_type) q.set('detection_type', params.detection_type)
    if (params.store_id != null) q.set('store_id', String(params.store_id))
    if (params.camera_id != null) q.set('camera_id', String(params.camera_id))
    q.set('limit', String(params.limit ?? 20))
    return api<SprintAlert[]>(`/labels/queue?${q}`)
  },
  confirm: (id: number) =>
    api<{ id: number; status: string }>(`/labels/${id}`, {
      method: 'POST', body: { verdict: 'confirm' } }),
  dismiss: (id: number) =>
    api<{ id: number; status: string }>(`/labels/${id}`, {
      method: 'POST', body: { verdict: 'dismiss' } }),
  auditQueue: (limit = 20) =>
    api<SprintAlert[]>(`/labels/audit-queue?limit=${limit}`),
  audit: (id: number, verdict: 'confirm' | 'dismiss') =>
    api<{
      alert_id: number; agreed: boolean; training_eligible: boolean
    }>(`/labels/${id}/audit`, { method: 'POST', body: { verdict } }),
  undo: (id: number) =>
    api<UndoResult>(`/labels/${id}/undo`, { method: 'DELETE' }),
  today: () => api<TodayCounts>(`/labels/today`),
}
