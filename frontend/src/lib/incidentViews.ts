// The five Alerts-page views and how each maps onto the server summary.
// Every count shown on the page comes from ONE /incidents/summary call
// for the same store + date range the list uses; nothing is counted from
// the rows that happen to be loaded in the browser.
import type { IncidentSummary, IncidentView } from '@/api/incidents'

export interface ViewDef {
  key: IncidentView
  label: string
  hint: string
  bulkResolvable: boolean
}

export const VIEWS: ViewDef[] = [
  { key: 'open', label: 'Open Incidents',
    hint: 'Every open security or operational incident, urgent first',
    bulkResolvable: true },
  { key: 'attention', label: 'Needs Attention',
    hint: 'Open incidents that are not urgent',
    bulkResolvable: true },
  { key: 'review', label: 'Review Queue',
    hint: 'Kept for a human to look at; never pages anyone',
    bulkResolvable: true },
  { key: 'resolved', label: 'Resolved',
    hint: 'Resolved or dismissed in this period',
    bulkResolvable: false },
  { key: 'all', label: 'All',
    hint: 'Everything in this period',
    bulkResolvable: false },
]

/** The count for a view, read from the server summary. */
export function countFor(view: IncidentView, s: IncidentSummary | null): number | null {
  if (!s) return null
  switch (view) {
    case 'open': return s.open
    case 'attention': return s.attention
    case 'review': return s.review
    case 'resolved': return s.resolved
    case 'all': return s.all
  }
}

/** Seconds -> "45s", "12m", "3h", "2d". */
export function shortAge(seconds: number | null | undefined): string {
  if (seconds == null) return '—'
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`
  return `${Math.floor(seconds / 86400)}d`
}

/** The bulk-resolve confirmation sentence, built from the SAME number
 *  that is sent to the server as expected_count. */
export function bulkConfirmText(count: number, viewLabel: string,
                                storeLabel: string, rangeLabel: string): string {
  const noun = count === 1 ? 'incident' : 'incidents'
  return `Resolve exactly ${count} ${noun} in “${viewLabel}” for ${storeLabel} `
    + `(${rangeLabel})? If the number has changed by the time you confirm, `
    + 'nothing will be resolved and the page will refresh.'
}

const POLICY_REASON_TEXT: Record<string, string> = {
  confirmed_after_hours_intrusion: 'Person confirmed in the store after hours',
  confirmed_before_hours_intrusion: 'Person confirmed in a protected area before opening',
  expected_early_staff: 'Recognised staff arriving before opening',
  known_staff_after_hours: 'Recognised staff in the store after hours',
  before_hours_no_protected_zone: 'Person before opening, but no protected zone is drawn',
  before_hours_no_store_schedule: 'Person before opening, but the store has no opening hours set',
  confirmed_person_in_staff_zone: 'Person confirmed inside the staff-only zone',
  likely_staff_in_staff_zone: 'Probably staff inside the staff-only zone',
  person_in_protected_zone: 'Person inside a protected zone',
  store_not_opened_by_schedule: 'Store not opened at its scheduled time',
  not_opened_without_schedule: 'Store may not have opened (no opening hours set)',
  review_only_until_validated: 'This detector is still being validated — review only',
  safety_critical_with_evidence: 'Safety event confirmed by the detector',
  safety_low_confidence: 'Possible safety event, low confidence — please check',
  safety_not_confirmed_across_frames: 'Possible safety event, seen only briefly — please check',
  quality_control_review_only: 'This camera is held for review after false alerts',
  detector_rule_matched: 'Detector rule matched',
}

/** Plain-language reason for why this incident exists. */
export function policyReasonText(reason: string | null | undefined): string | null {
  if (!reason) return null
  if (reason.startsWith('raw_review:')) return 'Filtered detection kept for review'
  return POLICY_REASON_TEXT[reason] ?? null
}
