// /alerts/* wrappers.
import { api, wsUrl } from './client'

export interface Alert {
  id: number; event_id: number; status: string
  review_only: boolean
  notification_suppressed: boolean
  quality_mode: 'active' | 'review_only' | 'quarantined' | string
  quality_reason: string | null
  acknowledged_at: string | null
  resolved_at: string | null
  notes: string | null
  created_at: string
  event_timestamp: string | null
  delivery_delay_seconds: number | null
  scope: 'camera' | 'fleet' | string
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
  severity_label: 'URGENT' | 'ATTENTION' | 'INFO' | 'POSITIVE – AUTOMATED' | 'POSITIVE – VERIFIED' | null
  // Four-tier severity ladder used by the redesigned page.
  severity_4: 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW' | null
  severity_4_color: string | null     // hex e.g. "#dc2626"
  severity_4_emoji: string | null     // "🔴" / "🟠" / "🟡" / "🔵"
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
  // Checkout-dwell timeline filmstrip (NULL for other alert types).
  // Render via /api/alerts/{id}/snapshot/{idx} — paths stay server-side.
  snapshot_paths: string[] | null
  snapshot_count: number   | null
  // Recorded video clip for this alert, when the recorder extracted one.
  // Play via <video src={clip_url}>. NULL when no clip (falls back to
  // the snapshot thumbnail/filmstrip).
  clip_url: string | null
  clip_status: 'ready' | 'pending' | 'unavailable'
  // VLM scene description (Sprint 2.1). NULL until the async analysis
  // task writes it, or when VLM is disabled / type ineligible.
  vlm_scene: string | null
  // Store Intelligence (Part 5) — structured BI payload for the special
  // store_intelligence card. NULL for every other alert type.
  store_intel?: {
    store_name: string | null; city: string | null
    time_eat: string | null; time_period: string | null
    people_count: number | null; staff_count: number | null
    counter_status: string | null; busiest_zone: string | null
    entry_count_45m: number | null; alert_count_45m: number | null
    hours_open: string | null
    ai_summary: string | null; recommendation: string | null
  } | null
}

export const alerts = {
  list:    (params: Record<string, string | number | undefined> = {}) => {
    const q = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== '') q.set(k, String(v))
    return api<Alert[]>(`/alerts${q.toString() ? `?${q}` : ''}`)
  },
  // Quick counts for the executive summary bar + sidebar badge.
  // Returns both the legacy 3-tier counts and the new 4-tier
  // ladder (critical/high/medium/low), plus avg-time-to-resolve
  // and the vs-yesterday trend.
  summary: (storeId?: number) =>
    api<{
      urgent: number; attention: number
      resolved_today: number; dismissed_today: number
      unread_urgent: number
      critical_today: number; high_today: number
      medium_today: number;   low_today: number
      calibration_today: number
      avg_response_seconds: number | null
      today_count: number; operational_today_count: number
      yesterday_count: number
      trend_vs_yesterday_pct: number | null
      date_label: string | null
    }>(`/alerts/summary${storeId ? `?store_id=${storeId}` : ''}`),
  confirm: (id: number) => api<{ id: number; status: string }>(`/alerts/${id}/confirm`, { method: 'POST' }),
  dismiss: (id: number) => api<{ id: number; status: string }>(`/alerts/${id}/dismiss`, { method: 'POST' }),
  // Mark as acknowledged — drives the Generated → Acknowledged →
  // Resolved progress bar on the alert card. Idempotent.
  acknowledge: (id: number) =>
    api<{ id: number; status: string }>(`/alerts/${id}/acknowledge`, { method: 'POST' }),
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
