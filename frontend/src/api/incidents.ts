// Typed client for the incident queue and the canonical fleet status.
// Mirrors backend/app/schemas/incident.py and app/services/fleet.py.
import { api } from './client'
import type { Alert } from './alerts'

export type IncidentView = 'open' | 'attention' | 'review' | 'resolved' | 'all'
export type IncidentAction = 'acknowledge' | 'resolve' | 'dismiss' | 'escalate'

export interface Incident {
  id: number
  status: 'open' | 'acknowledged' | 'escalated' | 'resolved' | 'dismissed'
  queue: 'incident' | 'review'
  priority: 'urgent' | 'attention' | 'low'
  detection_type: string
  rule: string | null
  store_id: number | null
  store_name: string | null
  camera_id: number | null
  camera_name: string | null
  zone_id: number | null
  zone_name: string | null
  opened_at: string | null
  first_seen_at: string | null
  last_seen_at: string | null
  acknowledged_at: string | null
  escalated_at: string | null
  closed_at: string | null
  close_reason: string | null
  event_count: number
  notify_count: number
  policy_decision: string | null
  policy_reason: string | null
  version: number
  head: Alert | null
}

export interface IncidentPage {
  view: IncidentView
  total: number
  limit: number
  offset: number
  items: Incident[]
}

export interface IncidentSummary {
  urgent: number
  attention: number
  review: number
  resolved: number
  open: number
  all: number
  avg_response_seconds: number | null
  lifecycle_enabled: boolean
}

export interface IncidentFilters {
  store_id?: string | number
  since?: string
  until?: string
}

export interface CoverageGap {
  camera_id: number
  camera_name: string
  store_id: number | null
  store_name: string | null
  state: 'offline' | 'stale' | 'unconfigured'
  seconds_since_frame: number | null
  reason: string
}

export interface FleetStatus {
  health: 'healthy' | 'degraded' | 'critical'
  health_reasons: string[]
  monitoring: 'active' | 'degraded' | 'offline'
  totals: {
    total: number; configured: number; fresh: number; stale: number
    offline: number; unconfigured: number; ai_active: number
  }
  queue: {
    pipeline_age_seconds: number | null
    waiting_for_worker: number
    queue_depth: number | null
    full_rotation_seconds: number | null
    sla_overdue: number
  }
  latest_detection_age_seconds: number | null
  latest_incident_age_seconds: number | null
  coverage_gaps: CoverageGap[]
}

function qs(params: Record<string, string | number | undefined>): string {
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== '') q.set(k, String(v))
  const s = q.toString()
  return s ? `?${s}` : ''
}

export const incidents = {
  list: (view: IncidentView, f: IncidentFilters, limit = 50, offset = 0) =>
    api<IncidentPage>(`/incidents${qs({ view, ...f, limit, offset })}`),
  summary: (f: IncidentFilters = {}) =>
    api<IncidentSummary>(`/incidents/summary${qs({ ...f })}`),
  act: (id: number, action: IncidentAction,
        body: { reason?: string; note?: string; verdict?: 'true'; version?: number } = {}) =>
    api<Incident>(`/incidents/${id}/${action}`, { method: 'POST', body }),
  bulkResolve: (view: IncidentView, f: IncidentFilters, expectedCount: number) =>
    api<{ resolved: number; ids: number[] }>('/incidents/bulk-resolve', {
      method: 'POST',
      body: {
        view,
        store_id: f.store_id === undefined || f.store_id === '' ? null : Number(f.store_id),
        since: f.since ?? null, until: f.until ?? null,
        expected_count: expectedCount,
      },
    }),
  exportUrl: (view: IncidentView, f: IncidentFilters) =>
    `/api/incidents/export.xlsx${qs({ view, ...f })}`,
}

export const fleet = {
  status: () => api<FleetStatus>('/system/fleet'),
}
