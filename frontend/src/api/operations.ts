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
}

export const operations = {
  reportMissedEvent: (body: MissedEventInput) =>
    api<MissedEventResult>('/operations/missed-events', {
      method: 'POST', body,
    }),
  listMissedEvents: () =>
    api<AssuranceCase[]>('/operations/cases?case_type=missed_event&limit=20'),
}
