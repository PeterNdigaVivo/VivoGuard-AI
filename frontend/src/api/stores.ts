// /stores and /analytics wrappers.
import { api } from './client'

export interface Store {
  id: number; name: string; code: string | null
  country: string; city: string | null; address: string | null
  timezone: string
  business_hours_json: Record<string, string[]> | null
  capacity: number | null
  manager_name:  string | null
  manager_phone: string | null
  // Per-store default RTSP port. New cameras attached to this store
  // inherit this in the Add Camera wizard. Null = no default; use
  // brand default (Dahua=7000, others=554).
  default_rtsp_port: number | null
  is_active: boolean
  created_at: string
}

export interface MetricPoint {
  id: number
  store_id: number | null
  camera_id: number | null
  zone_id: number | null
  metric_type: string
  period_start: string
  period_end: string
  value: number
  dims: Record<string, any> | null
}

export interface StoreDashboard {
  store_id: number
  store_name: string
  country: string
  as_of: string
  window_days: number
  kpis: {
    occupancy_now: number | null
    occupancy_avg: number | null
    queue_length_now: number | null
    queue_length_avg: number | null
    queue_wait_avg_sec: number | null
    staff_present_avg: number | null
    unique_visitors_today: number
    passersby_avg: number | null
    stop_rate_avg: number | null
    dwell_seconds_avg: number | null
    shutter_open_now: number | null
  }
  alerts_breakdown: Record<string, number>
}

export const stores = {
  list:   ()                  => api<Store[]>('/stores'),
  get:    (id: number)        => api<Store>(`/stores/${id}`),
  create: (body: Partial<Store>) => api<Store>('/stores', { method: 'POST', body }),
  update: (id: number, body: Partial<Store>) =>
    api<Store>(`/stores/${id}`, { method: 'PATCH', body }),
  remove: (id: number) => api(`/stores/${id}`, { method: 'DELETE' }),
  attachCamera: (storeId: number, camId: number) =>
    api(`/stores/${storeId}/cameras/${camId}/attach`, { method: 'POST' }),
  detachCamera: (storeId: number, camId: number) =>
    api(`/stores/${storeId}/cameras/${camId}/detach`, { method: 'POST' }),
}

export const analytics = {
  metrics: (params: Record<string, string | number | undefined>) => {
    const q = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== '') q.set(k, String(v))
    return api<MetricPoint[]>(`/analytics/metrics?${q}`)
  },
  storeDashboard: (storeId: number, days = 7) =>
    api<StoreDashboard>(`/analytics/dashboard/store/${storeId}?days=${days}`),
  // Chain dashboard payload. The May-2026 redesign adds RAG status
  // per store, a needs_attention list, and a best_store_today field.
  // Old call sites that read .stores / .totals keep working.
  multiDashboard: (days = 7) =>
    api<{
      stores: (StoreDashboard & {
        cameras_online: number
        cameras_total: number
        is_open: boolean
        rag_status: 'red' | 'amber' | 'green'
        rag_reasons: string[]
        recent_critical_alerts: number
      })[]
      totals: {
        unique_visitors_today: number
        stores: number
        alerts_total: number
        alerts_critical: number
        stores_open: number
        stores_attention: number
      }
      best_store_today: { store_id: number; store_name: string; visitors: number } | null
      needs_attention: {
        store_id: number; store_name: string; country: string
        rag_status: 'red' | 'amber'
        rag_reasons: string[]
        cameras_online: number; cameras_total: number
      }[]
      as_of: string
    }>(`/analytics/dashboard/multi?days=${days}`),
}
