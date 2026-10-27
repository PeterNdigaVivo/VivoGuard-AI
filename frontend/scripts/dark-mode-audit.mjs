// Dark-mode readability audit — opens every page in a real browser
// against a fake API, and for every piece of visible text works out the
// colour actually painted behind it, then reports any text whose
// contrast is below 4.5:1 (WCAG AA for normal text).
//
// Usage (from frontend/, with `pnpm dev --host 127.0.0.1` running):
//   node scripts/dark-mode-audit.mjs <out-dir> [dark|light]
//
// Writes <out-dir>/<theme>-<page>.png screenshots and
// <out-dir>/<theme>-report.json. Exits 1 when a dark-mode failure is
// found. All data below is invented; no real stores or addresses.
import { mkdirSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { join } from 'node:path'

// Playwright is not a project dependency; use a global install
// (`npm i -g playwright && npx playwright install chromium`).
const { chromium } = createRequire(import.meta.url)('playwright')

const BASE = process.env.AUDIT_BASE_URL ?? 'http://127.0.0.1:5173'
const OUT = process.argv[2] ?? 'dark-mode-audit'
const THEME = process.argv[3] ?? 'dark'
const NOW = new Date('2026-10-27T09:30:00Z')
const iso = (minAgo = 0) => new Date(NOW.getTime() - minAgo * 60_000).toISOString()

const ROUTES = [
  ['stores', '/stores'], ['store-detail', '/stores/1'], ['store-analytics', '/stores/1/analytics'],
  ['store-cameras', '/stores/1/cameras'], ['store-add-camera', '/stores/1/add-camera'],
  ['store-heatmaps', '/heatmaps/1'], ['detectors', '/detectors'], ['users', '/users'],
  ['cameras', '/cameras'], ['camera-add', '/cameras/add'], ['camera-setup', '/cameras/1/setup'],
  ['camera-detection', '/cameras/1/detection'], ['camera-heatmap', '/cameras/1/heatmap'],
  ['search', '/search'], ['live', '/live'], ['alerts', '/alerts'], ['training', '/training'],
  ['training-shutter', '/training/shutter'], ['training-uniform', '/training/uniform'],
  ['training-chain', '/training/chain'], ['annotation', '/training/datasets/1'],
  ['training-job', '/training/jobs/1'], ['system', '/system'], ['odoo', '/system/odoo'],
  ['evaluation', '/evaluation'],
]

// ── Fake API ────────────────────────────────────────────────────────
const store = (id, name, city) => ({
  id, name, code: `S${id}`, country: 'Kenya', city, address: 'Example Mall, Level 1',
  timezone: 'Africa/Nairobi', business_hours_json: null, capacity: 40,
  manager_name: 'Store Manager', manager_phone: null, default_rtsp_port: 554,
  queue_sla_seconds: 180, queue_sla_length: 6, is_active: true, created_at: iso(90_000),
})
const camera = (id, name, status) => ({
  id, name, site: 'Example Mall', brand: 'hikvision', connection_type: 'nvr',
  host: `192.0.2.${10 + id}`, public_ip: null, rtsp_port: 554, http_port: 80, nvr_id: 1,
  channel_number: id, network_type: 'lan', ai_model_id: 1, ai_enabled: true,
  inference_fps: 2, status, last_seen_at: iso(1), last_error: status === 'offline'
    ? 'Connection timed out' : null, store_id: 1, transport: 'rtsp',
  snapshot_url_override: null, rtsp_transport: 'tcp', created_at: iso(90_000),
})
const alert = (id, type, sev4, title, extra = {}) => ({
  id, event_id: id, status: 'new', review_only: false, notification_suppressed: false,
  quality_mode: 'active', quality_reason: null, ai_verdict: 'true_alert', ai_confidence: 0.91,
  ai_outcome: 'Likely real', ai_action: null, ai_reason: 'Person visible at the till.',
  ai_verified_at: iso(2), ai_model: 'verifier', ai_enabled: true, acknowledged_at: null,
  resolved_at: null, notes: null, created_at: iso(id * 7), event_timestamp: iso(id * 7),
  delivery_delay_seconds: 4, scope: 'camera', camera_id: 1, camera_name: 'Till Camera',
  detection_type: type, confidence: 0.82, bbox_norm: null, zone_id: 1, zone_name: 'Till',
  thumbnail_path: null, severity: 'warning', severity_label: 'ATTENTION', severity_4: sev4,
  severity_4_color: '#ea580c', severity_4_emoji: '🟠', title, plain_title: title,
  body: 'The till area has been unattended for 6 minutes.',
  what_to_do: ['Call the store', 'Check the till camera'], time_range: '🕒 Detected at 12:23 PM',
  snapshot_url: null, snapshot_paths: null, snapshot_count: null, clip_url: null,
  clip_status: 'unavailable', vlm_scene: null, store_intel: null, ...extra,
})

const FIXTURES = [
  [/^\/auth\/me$/, { id: 1, email: 'itsupport@vivofashiongroup.com', full_name: 'IT Support', role: 'admin', is_active: true }],
  [/^\/stores$/, [store(1, 'Example Store One', 'Nairobi'), store(2, 'Example Store Two', 'Mombasa')]],
  [/^\/stores\/\d+$/, store(1, 'Example Store One', 'Nairobi')],
  [/^\/stores\/\d+\/cameras$/, [camera(1, 'Till Camera', 'streaming'), camera(2, 'Entrance', 'offline')]],
  [/^\/cameras$/, [camera(1, 'Till Camera', 'streaming'), camera(2, 'Entrance', 'offline'), camera(3, 'Shop Floor', 'connecting')]],
  [/^\/cameras\/\d+$/, camera(1, 'Till Camera', 'streaming')],
  [/^\/cameras\/\d+\/(zones|detection-config)$/, []],
  [/^\/alerts\/summary/, {
    urgent: 2, attention: 3, resolved_today: 4, dismissed_today: 1, unread_urgent: 1,
    critical_today: 1, high_today: 2, medium_today: 3, low_today: 1, calibration_today: 0,
    avg_response_seconds: 240, today_count: 7, operational_today_count: 7, yesterday_count: 5,
    trend_vs_yesterday_pct: 40, date_label: 'Today', ai_true_today: 4, ai_false_today: 1,
    ai_uncertain_today: 1, ai_pending_today: 1, ai_verifier_enabled: true,
  }],
  [/^\/alerts(\?|$)/, [
    alert(1, 'till_unattended', 'HIGH', 'Till Unattended'),
    alert(2, 'unusual_activity', 'CRITICAL', 'Unusual Activity Seen', {
      vlm_scene: 'A person is standing behind the counter after closing time.' }),
    alert(3, 'staff_uniform', 'MEDIUM', 'Staff Not in Uniform', { ai_verdict: 'uncertain' }),
    alert(4, 'queue_long', 'LOW', 'Long Queue', { status: 'resolved', resolved_at: iso(3) }),
  ]],
  [/^\/eval\/summary$/, {
    collection_enabled: true, targets: { per_alert_type: 200, people: 300, clips: 100 },
    retention_days: 180,
    alerts: { till_unattended: { total: 40, true: 30, false: 10 }, unusual_activity: { total: 12, true: 4, false: 8 } },
    people: { pending: 20, labelled: 15, skipped: 2 }, clips: { pending: 5, labelled: 3, skipped: 0 },
  }],
  [/^\/eval\/next/, null],
  [/^\/odoo\/assurance$/, {
    enabled: true, mode: 'read_only', mapped: 1, unmapped: 1,
    mappings: [
      { store_id: 1, store_name: 'Example Store One', mapped: true, odoo_name: 'Store One POS', odoo_pos_config_id: 7, last_synced_at: iso(10), sync_error: null },
      { store_id: 2, store_name: 'Example Store Two', mapped: false, odoo_name: null, odoo_pos_config_id: null, last_synced_at: null, sync_error: 'No POS mapping' },
    ],
    sync: [{ stream: 'pos_orders', last_success_at: iso(5), consecutive_failures: 2, circuit_open_until: null, last_error: 'Timeout' }],
    till_conflicts: [{ id: 1, store_id: 1, business_day: '2026-10-26', conflict_type: 'till_open_no_staff', camera_event_at: iso(60), till_event_at: iso(58), status: 'open' }],
    conversion: [{ store_id: 1, period_start: iso(120), footfall: 140, transactions: 31, conversion_rate: 0.22, data_quality_flag: true }],
    changing_room_reviews: [{ id: 1, store_id: 1, camera_id: 1, title: 'Changing room review', status: 'pending' }],
  }],
  [/^\/users$/, [
    { id: 1, email: 'itsupport@vivofashiongroup.com', full_name: 'IT Support', role: 'admin', is_active: true, created_at: iso(90_000) },
    { id: 2, email: 'viewer@example.com', full_name: 'Example Viewer', role: 'viewer', is_active: false, created_at: iso(9_000) },
  ]],
  [/^\/analytics\/store\/\d+\/live/, {
    store_id: 1, store_name: 'Example Store One', country: 'Kenya', status: 'live', status_light: 'amber',
    as_of: iso(), camera_count: 3, cameras_total: 3, cameras_online: 2, high_alert_count: 1,
    total_alert_count: 4, is_open: true, hours_label: '9:00 AM – 9:00 PM',
    session_open: iso(150), session_close: iso(-570),
    zone_capabilities: { entrance: true, till: true, shop_floor: true, fitting_room: false },
    tiles: Object.fromEntries(['visitors_today', 'occupancy_now', 'queue_length_now', 'staff_present', 'conversion_rate', 'dwell_avg']
      .map((k, i) => [k, { value: 10 + i * 7, visible: true, trend: { direction: i % 2 ? 'down' : 'up', delta_pct: 12 } }])),
  }],
  [/^\/analytics\/store\/\d+\/multi-camera/, {
    store_id: 1, store_name: 'Example Store One', as_of: iso(), totals: { in: 120, out: 98, net: 22, occupancy: 22 },
    cameras: [{ camera_id: 1, name: 'Entrance', status: 'streaming', entrance: true, in: 120, out: 98, occupancy: 22 },
      { camera_id: 2, name: 'Shop Floor', status: 'offline', entrance: false, in: 0, out: 0, occupancy: 0 }],
  }],
  [/^\/analytics\/store\/\d+\/heatmaps/, { cameras: [
    { camera_id: 1, camera_name: 'Entrance', status: 'streaming', heatmap_url: '/api/cameras/1/heatmap.png', rank_label: 'Busiest', hotspot: { value: 0.8, norm_x: 0.4, norm_y: 0.5 } },
    { camera_id: 2, camera_name: 'Shop Floor', status: 'offline', heatmap_url: '/api/cameras/2/heatmap.png', rank_label: null, hotspot: null },
  ] }],
  [/^\/analytics\/store\/\d+\/customer-flow/, { zones: [], edges: [], total_journeys: 0 }],
  [/^\/analytics\/store\/\d+\/peak-prediction/, { headline: 'Busiest between 5 PM and 7 PM', label: 'Evening peak' }],
  [/^\/analytics\/store\/\d+\/journeys/, {
    store_id: 1, date: '2026-10-27', unique_people: 54, unique_customers: 47, camera_appearances: 130,
    top_paths: [{ path: ['Entrance', 'Shop Floor', 'Till'], count: 12 }], journeys: [],
  }],
  [/^\/analytics\/store\/\d+\/anomalies/, { anomalies: [{ headline: 'Footfall 40% above normal at 3 PM', hour: 15, z_score: 2.6 }], note: 'Compared with the last 4 weeks.' }],
  [/^\/analytics\/store\/\d+\/staffing-prediction/, { recommendations: [
    { weekday: 'Saturday', hour: 17, expected_visitors: 80, recommended_staff: 4, headline: '4 staff from 5 PM' },
    { weekday: 'Friday', hour: 18, expected_visitors: 60, recommended_staff: 3, headline: '3 staff from 6 PM' }], note: 'Based on 8 weeks.' }],
  [/^\/analytics\/store\/\d+\/health-score/, { score: 72, last_week_score: 78, delta: -6, headline: 'Slightly down on last week',
    components: [{ name: 'Till cover', weight: 0.4, value: 65 }, { name: 'Uniform', weight: 0.3, value: 90 }] }],
  [/^\/analytics\/store\/\d+\/loss-prevention/, { total_alerts_this_week: 9, peak_hour: { hour: 19, count: 4 },
    hotspots: [{ camera_name: 'Till Camera', count: 5 }], headline_time: 'Most alerts at 7 PM', headline_camera: 'Till Camera', headline_staff: 'Cover the till at closing' }],
  [/^\/analytics\/store\/\d+\/behaviour-trends/, { avg_visit_seconds: { this_week: 410, last_week: 380, delta_pct: 8 },
    top_zones_this_week: [{ zone: 'Shop Floor', visits: 300 }], conversion_funnel: { this_week_pct: 22, last_week_pct: 25, headline: 'Conversion down 3 points' } }],
  [/^\/analytics\/store\/\d+\/week-summary/, {
    days: ['Mon', 'Tue', 'Wed'].map((w, i) => ({ date: `2026-10-2${i + 4}`, weekday: w, value: 30 + i * 10, source: 'entrance' })),
    best_day: { date: '2026-10-26', weekday: 'Wed', value: 50 }, worst_day: { date: '2026-10-24', weekday: 'Mon', value: 30 },
    weekday_avg_occupancy: [{ weekday: 'Mon', value: 8 }], top_hours: [{ hour: 17, label: '5 PM', value: 22 }],
    detector_activity: [
      { detector: 'till_unattended', events_today: 3, events_week: 14, needs_zone: true, zone_present: true, status: 'active' },
      { detector: 'fitting_room', events_today: 0, events_week: 0, needs_zone: true, zone_present: false, status: 'needs_setup' }],
  }],
  [/^\/analytics\/store\/\d+\/hourly/, {
    hours: [9, 10, 11, 12, 13].map((h, i) => ({ hour: h, label: `${h}:00`, visitors: [4, 12, 20, 8, 0][i],
      intensity: ['low', 'medium', 'high', 'low', 'empty'][i], is_current: h === 12 })),
    hours_yesterday: [{ hour: 9, visitors: 5 }, { hour: 10, visitors: 9 }], peak: { hour: 11, visitors: 20 },
    quiet: { hour: 9, visitors: 4 }, busy_hour_range: [10, 12], trend_delta_pct: 12,
    insights: ['Busiest at 11 AM', 'Quieter than usual before 10 AM'], open_hour: 9, close_hour: 21, current_hour: 12,
    today_total: 44, yesterday_so_far: 38, metric_source: 'entrance', data_source_label: 'Entrance counter', restock_hour: 10,
  }],
  [/^\/analytics\/store\/\d+\/behaviour(\?|$)/, { has_journey_data: true, total_journeys: 40, avg_journey_seconds: 300,
    completion_pct: 62, common_path: ['Entrance', 'Shop Floor', 'Till'], most_skipped_zone: 'Fitting Room',
    zone_visit_counts: { Entrance: 40, 'Shop Floor': 32, Till: 18 }, top_paths: [{ path: ['Entrance', 'Till'], count: 9 }] }],
  [/^\/analytics\/store\/\d+\/staff-timeline/, { has_data: true, today_pct: 78, current_status: 'unstaffed',
    gaps: [{ start: iso(60), end: iso(48), duration_minutes: 12 }, { start: iso(5), end: null, duration_minutes: 5, ongoing: true }],
    timeline: Array.from({ length: 30 }, (_, i) => ({ minute: iso(30 - i), staffed: i % 7 !== 0 })),
    insights: ['Till left uncovered twice today'], uniform_compliance_pct: 85, uniform_compliance_rag: 'amber',
    uniform_violations_today: 2, uniform_violations: [{ time: iso(40) }], staff_identified_total: 4, staff_by_uniform: 3,
    staff_by_zone: 1, staff_zone_breaches_today: 1, nametag_violations_today: 0, compliance_verdict: 'Needs attention' }],
  [/^\/analytics\/store\/\d+\/zone-performance/, { insights: ['Fitting Room is a bottleneck'], zones: [
    { zone_id: 1, name: 'Shop Floor', engagement_pct: 70, engagement_score: 7, avg_dwell_seconds: 240, traffic_count: 120, peak_hour: '5 PM', rag: 'green', rank: 1, tags: ['Popular'], recommendation: 'Keep stocked' },
    { zone_id: 2, name: 'Fitting Room', engagement_pct: 30, avg_dwell_seconds: 400, traffic_count: 40, is_bottleneck: true, rag: 'amber', rank: 2, recommendation: 'Add a staff member' },
    { zone_id: 3, name: 'Till', engagement_pct: 10, avg_dwell_seconds: 90, traffic_count: 30, is_queue_zone: true, rag: 'red', rank: 3 }] }],
  [/^\/analytics\/store\/\d+\/scorecard/, { overall_direction: 'worse', overall_rag: 'amber', headline: 'Slightly behind yesterday',
    rows: [
      { metric: 'Visitors', today: 44, yesterday: 38, unit: '', higher_is_better: true, direction: 'up', delta_pct: 16, rag: 'green' },
      { metric: 'Avg wait', today: 190, yesterday: 120, unit: 's', higher_is_better: false, direction: 'up', delta_pct: 58, rag: 'red' },
      { metric: 'Dwell', today: 300, yesterday: 300, unit: 's', higher_is_better: true, direction: 'flat', delta_pct: 0, rag: 'slate' }] }],
  [/^\/analytics\/store\/\d+\/visitor-intelligence/, { total_persons: 54, staff_tracks: 7, estimated_customers: 47,
    staff_signatures: ['uniform'], avg_dwell_seconds: 300, zones_visited_avg: 2.4, total_zones: 4, visit_quality_score: 64,
    confidence: 'estimated', explanation: 'Staff removed by uniform colour.', footnote: 'Estimates may vary by ±10%.' }],
  [/^\/analytics\/store\/\d+\/queue-snapshot/, { taken_at: iso(1), active_counters: 1, has_data: true,
    customers_in_queue: [{ position: 1, wait_seconds: 200, status: 'RED' }, { position: 2, wait_seconds: 90, status: 'AMBER' }, { position: 3, wait_seconds: 20, status: 'GREEN' }],
    summary: { avg_wait: '01:50', peak_wait: '03:20', shortest_wait: '00:20', red_count: 1, amber_count: 1, green_count: 1, sla_target: '03:00' } }],
  [/^\/analytics\/store\/\d+\/queue-intelligence/, { store_name: 'Example Store One', date: '2026-10-27', generated_at: iso(),
    summary: { avg_wait_seconds: 110, peak_wait_seconds: 200, shortest_wait_seconds: 20, avg_queue_length: 2.1, peak_queue_length: 5, total_snapshots: 80 },
    sla: { target_max_wait_seconds: 180, target_max_queue_length: 6, breaches: 4, breach_pct: 5, status: 'AT RISK' },
    status_breakdown: { green_pct: 70, amber_pct: 25, red_pct: 5 },
    hourly_breakdown: [{ hour: '11:00', avg_wait: 100, avg_queue: 2, status: 'AMBER', snapshots: 12 }, { hour: '12:00', avg_wait: 200, avg_queue: 4, status: 'RED', snapshots: 12 }],
    peak_hour: '12:00', quietest_hour: '09:00', trend: 'WORSENING', staffing_recommendation: 'Open a second till at noon',
    recommendations: ['Open a second till at noon'], executive_summary: 'Queues peaked at lunchtime.' }],
  [/^\/system-health$/, {
    generated_at: iso(), overall: { emoji: '🟡', label: 'Needs attention' },
    containers: [{ name: 'backend', status: 'running', uptime: '3 days', healthy: true }, { name: 'worker', status: 'restarting', uptime: null, healthy: false }],
    cameras: { total: 3, streaming: 2, offline: 1, offline_names: ['Entrance'], ai_enabled_active: 2 },
    detection: { events_last_30min_by_type: { person: 12 }, total_events_today: 140, events_by_hour_24h: [] },
    model: { version: 'v1', map50: 0.71, precision: 0.8, recall: 0.7, deployed_since: iso(9_000) },
    training: { jobs_queued: 0, jobs_running: 1, jobs_completed_today: 0, latest_map50: 0.7 },
    storage: { total_gb: 1000, used_gb: 820, free_gb: 180, percent_used: 82, docker_images_gb: 40, recordings_gb: 700, alert_clips_gb: 20 },
    database: { total_alerts: 1000, alerts_today: 7, detection_events_count: 50_000 },
    alerts: { urgent_today: 2, resolved_today: 4, pending_today: 3 },
    integrations: { bytetrack_active: true, supervision_active: false },
  }],
]

function fakeApi(path) {
  for (const [re, body] of FIXTURES) if (re.test(path)) return body
  // Unknown endpoints: an empty list renders each page's empty state.
  return []
}

// ── In-page contrast measurement ────────────────────────────────────
function measure() {
  const parse = c => {
    const m = c.match(/rgba?\(([^)]+)\)/)
    if (!m) return null
    const [r, g, b, a = 1] = m[1].split(/[ ,/]+/).filter(Boolean).map(Number)
    return { r, g, b, a }
  }
  const over = (top, under) => ({
    r: top.r * top.a + under.r * (1 - top.a), g: top.g * top.a + under.g * (1 - top.a),
    b: top.b * top.a + under.b * (1 - top.a), a: 1,
  })
  const lum = ({ r, g, b }) => {
    const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4 }
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
  }
  const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05) }
  // Paint the background stack from the page up to the element.
  const background = el => {
    const chain = []
    for (let n = el; n; n = n.parentElement) chain.push(n)
    let bg = parse(getComputedStyle(document.documentElement).backgroundColor)
    bg = bg && bg.a > 0 ? bg : { r: 255, g: 255, b: 255, a: 1 }
    for (const n of chain.reverse()) {
      const s = getComputedStyle(n)
      if (s.backgroundImage !== 'none' || ['IMG', 'VIDEO', 'CANVAS'].includes(n.tagName)) return null
      const c = parse(s.backgroundColor)
      if (c && c.a > 0) bg = over({ ...c, a: c.a * Number(s.opacity) }, bg)
    }
    return bg
  }
  const describe = el => {
    const cls = typeof el.className === 'string' ? el.className.trim().split(/\s+/).slice(0, 8).join('.') : ''
    return `${el.tagName.toLowerCase()}${cls ? '.' + cls : ''}`
  }
  const out = []
  const seen = new Set()
  const check = (el, text, color, kind) => {
    const r = el.getBoundingClientRect()
    if (r.width === 0 || r.height === 0) return
    const s = getComputedStyle(el)
    if (s.visibility === 'hidden' || s.display === 'none') return
    if (el.closest('[disabled],[aria-disabled="true"],[aria-hidden="true"]')) return
    let opacity = 1
    for (let n = el; n; n = n.parentElement) opacity *= Number(getComputedStyle(n).opacity)
    if (opacity < 0.05) return
    const bg = background(el)
    const fg = parse(color)
    if (!bg || !fg) return
    const ratioV = ratio(over({ ...fg, a: fg.a * opacity }, bg), bg)
    if (!el.dataset.vgAudit) el.dataset.vgAudit = String(window.__vgAuditId = (window.__vgAuditId ?? 0) + 1)
    const key = `${el.dataset.vgAudit}|${kind}|${text.slice(0, 40)}`
    if (seen.has(key)) return
    seen.add(key)
    out.push({ key, kind, el: describe(el), text: text.slice(0, 60), color, ratio: Math.round(ratioV * 100) / 100,
      bg: `rgb(${Math.round(bg.r)},${Math.round(bg.g)},${Math.round(bg.b)})` })
  }
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
  for (let t = walker.nextNode(); t; t = walker.nextNode()) {
    const text = t.textContent.trim()
    if (!text || !/[\p{L}\p{N}]/u.test(text) || !t.parentElement) continue
    if (['SCRIPT', 'STYLE', 'OPTION'].includes(t.parentElement.tagName)) continue
    check(t.parentElement, text, getComputedStyle(t.parentElement).color, 'text')
  }
  for (const el of document.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=range]):not([type=color]), textarea, select')) {
    if (el.value) check(el, el.value, getComputedStyle(el).color, 'value')
    if (el.placeholder) check(el, el.placeholder, getComputedStyle(el, '::placeholder').color, 'placeholder')
  }
  return out
}

// ── Run ─────────────────────────────────────────────────────────────
mkdirSync(OUT, { recursive: true })
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
await context.clock.setFixedTime(NOW)
await context.addInitScript(theme => {
  localStorage.setItem('vg_theme', theme)
  localStorage.setItem('vg_access_token', 'audit-token')
}, THEME)
await context.route(/^https?:\/\/[^/]+\/api\//, route => {
  const url = new URL(route.request().url())
  const path = url.pathname.replace(/^\/api/, '') + url.search
  const body = fakeApi(path)
  if (route.request().resourceType() === 'image' || /snapshot|media|heatmap\.png/.test(path)) {
    return route.fulfill({ status: 404, body: '' })
  }
  return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
})
await context.route(/\/ws\//, route => route.abort())

const report = {}
const requested = new Set()
context.on('request', r => { const m = r.url().match(/\/api(\/[^?]*)/); if (m) requested.add(m[1]) })
let failures = 0
const only = process.env.AUDIT_ONLY?.split(',')
for (const [name, path] of ROUTES.filter(([n]) => !only || only.includes(n))) {
  const page = await context.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e.stack ?? e).slice(0, 300)))
  await page.goto(BASE + path, { waitUntil: 'networkidle' }).catch(e => errors.push(String(e)))
  await page.waitForTimeout(800)
  const items = await page.evaluate(measure)
  if (process.env.AUDIT_DEBUG) console.log([...requested].join('\n')), requested.clear()
  await page.screenshot({ path: join(OUT, `${THEME}-${name}.png`), fullPage: true, animations: 'disabled' })
  // Same elements in the other theme: a low contrast that is no worse
  // than in light mode is a pre-existing design issue, not a dark-mode one.
  const other = THEME === 'dark' ? 'light' : 'dark'
  await page.evaluate(theme => document.documentElement.classList.toggle('dark', theme === 'dark'), other)
  await page.waitForTimeout(400)   // body colour has a 200 ms transition
  const otherItems = await page.evaluate(measure)
  const otherRatio = new Map(otherItems.map(i => [i.key, i.ratio]))
  const low = items.filter(i => i.ratio < 4.5)
  const bad = low.filter(i => THEME !== 'dark' || !(otherRatio.get(i.key) <= i.ratio + 0.05))
  const both = low.filter(i => !bad.includes(i)).map(i => ({ ...i, light_ratio: otherRatio.get(i.key) }))
  failures += bad.length
  report[name] = { path, checked: items.length, failing: bad, same_in_both_themes: both, errors }
  console.log(`${name.padEnd(18)} checked ${String(items.length).padStart(4)}  below 4.5:1 ${String(bad.length).padStart(3)}`
    + (THEME === 'dark' ? `  (same in light mode: ${both.length})` : '')
    + (errors.length ? '  page errors: ' + errors.length : ''))
  await page.close()
}
writeFileSync(join(OUT, `${THEME}-report.json`), JSON.stringify(report, null, 2))
await browser.close()
console.log(`\n${THEME}: ${failures} text items below 4.5:1 across ${ROUTES.length} pages`
  + (THEME === 'dark' ? ' (not counting ones exactly as low in light mode)' : ''))
process.exit(THEME === 'dark' && failures > 0 ? 1 : 0)
