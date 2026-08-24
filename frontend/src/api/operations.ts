import { api } from './client'

export interface MissedEventInput {
  source: 'manual'
  source_ref: string
  store_id: number
  camera_id: number | null
  occurred_at: string
  report_text: string
  label: string
  match_window_seconds: number
}

export interface MissedEventResult {
  case_id: number
  root_cause: string
  training_status: string
  training_image_id: number | null
  matched_event_id: number | null
}

export interface AssuranceCase {
  id: number
  case_type: string
  severity: string
  status: string
  store_id: number | null
  camera_id: number | null
  title: string
  root_cause: string | null
  training_status: string | null
  first_seen_at: string
  alert_id: number | null
  evidence: Record<string, unknown> | null
}

export interface QualityScorecard {
  store_name: string | null
  camera_name: string
  detection_type: string
  reviewed_sample_size: number
  target_minimum_reviewed: number
  precision: number | null
  precision_lower_bound_95: number | null
  recall: number | null
  recall_lower_bound_95: number | null
  recall_true_positive_events: number
  recall_false_negative_events: number
  target_minimum_recall_events: number
  target_99_precision_evidence_met: boolean
  target_99_recall_evidence_met: boolean
  target_99_evidence_met: boolean
  quality_mode: string
}

export const operations = {
  reportMissedEvent: (body: MissedEventInput) =>
    api<MissedEventResult>('/operations/missed-events', {
      method: 'POST', body,
    }),
  listMissedEvents: () =>
    api<AssuranceCase[]>('/operations/cases?case_type=missed_event&limit=20'),
  listReviewerDisagreements: () =>
    api<AssuranceCase[]>('/operations/cases?case_type=reviewer_disagreement&limit=50'),
  adjudicateReviewerDisagreement: (
    caseId: number,
    verdict: 'confirm' | 'dismiss' | 'unclear',
    rationale: string,
  ) => api<{
    case_id: number
    status: string
    verdict: string
    training_eligible: boolean
  }>(`/operations/cases/${caseId}/adjudicate`, {
    method: 'POST', body: { verdict, rationale },
  }),
  generateRecallSamples: (body: {
    store_id?: number
    sample_count: number
    duration_seconds: number
    seed: string
  }) => api<{ created: number; reused: number; case_ids: number[]; seed_hash: string }>(
    '/operations/recall-samples/generate', { method: 'POST', body }),
  listRecallSamples: () =>
    api<AssuranceCase[]>('/operations/cases?case_type=recall_sample&limit=100'),
  getQualityScorecards: () =>
    api<{ window_days: number; scorecards: QualityScorecard[] }>(
      '/quality/scorecards?days=7'),
  reviewRecallSample: (
    caseId: number,
    outcome: 'target_event' | 'no_target_event' | 'unclear',
    eventLabel: string | null,
    rationale: string,
  ) => api<{ case_id: number; status: string; result: string }>(
    `/operations/recall-samples/${caseId}/review`, {
      method: 'POST',
      body: { outcome, event_label: eventLabel, rationale },
    }),
}
