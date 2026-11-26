// Model rollout (canary -> evaluate -> manual promote -> rollback) and the
// training-evidence review queue. Mirrors backend/app/api/model_rollouts.py
// and backend/app/api/training_evidence.py.
import { api } from './client'

export interface CameraModelRow {
  camera_id: number
  camera_name: string
  store_id: number | null
  assigned_model_id: number | null
  assigned_model: string | null
  effective_model: string
  fallback_reason: string | null
  canary_rollout_id: number | null
}

export interface FleetModels {
  base_model: string
  cameras: CameraModelRow[]
}

export interface ModelEvaluation {
  id: number
  model_id: number
  rollout_id: number | null
  split: 'validation' | 'held_out' | 'canary'
  precision: number | null
  recall: number | null
  false_positive_rate: number | null
  sample_count: number | null
  passed: boolean | null
  reasons: string[]
  metrics: Record<string, any> | null
  created_at: string | null
}

export interface Rollout {
  id: number
  model_id: number
  model_label: string | null
  stage: 'canary' | 'promoted' | 'rolled_back'
  camera_ids: number[]
  started_at: string | null
  evaluated_at: string | null
  promoted_at: string | null
  rolled_back_at: string | null
  latest_evaluation: ModelEvaluation | null
}

export type ReviewState = 'pending_review' | 'approved' | 'rejected' | 'excluded'
export type ExampleKind = 'positive' | 'negative' | 'uncertain' | 'policy_error'

export interface Evidence {
  id: number
  dataset_id: number | null
  camera_id: number | null
  review_state: string
  example_kind: ExampleKind | null
  target_class: string | null
  source_kind: string
  source_alert_id: number | null
  detection_type: string | null
  dismiss_reason: string | null
  box_count: number
  preview_url: string | null
}

export interface Readiness {
  target_class: string
  ready: boolean
  approved_positives: number
  approved_negatives: number
  pending_review: number
  reasons: string[]
}

export const modelRollouts = {
  fleet: () => api<FleetModels>('/model-rollouts/fleet'),
  list: () => api<Rollout[]>('/model-rollouts'),
  evaluations: (modelId: number) =>
    api<ModelEvaluation[]>(`/model-rollouts/models/${modelId}/evaluations`),
  startCanary: (modelId: number, cameraIds: number[], notes?: string) =>
    api<Rollout>('/model-rollouts/canary', {
      method: 'POST', body: { model_id: modelId, camera_ids: cameraIds, notes: notes ?? null },
    }),
  evaluate: (id: number) =>
    api<ModelEvaluation>(`/model-rollouts/${id}/evaluate`, { method: 'POST' }),
  promote: (id: number, cameraIds: number[]) =>
    api<Rollout>(`/model-rollouts/${id}/promote`, { method: 'POST', body: { camera_ids: cameraIds } }),
  rollback: (id: number) =>
    api<Rollout>(`/model-rollouts/${id}/rollback`, { method: 'POST' }),
}

export const trainingEvidence = {
  list: (state: ReviewState, kind?: string, limit = 50, offset = 0) => {
    const q = new URLSearchParams({ state, limit: String(limit), offset: String(offset) })
    if (kind) q.set('kind', kind)
    return api<{ total: number; items: Evidence[]; counts: Record<string, number> }>(
      `/training-evidence?${q}`)
  },
  review: (id: number, decision: 'approve' | 'reject' | 'exclude',
           kind?: ExampleKind, targetClass?: string) =>
    api<Evidence>(`/training-evidence/${id}/review`, {
      method: 'POST',
      body: { decision, kind: kind ?? null, target_class: targetClass ?? null },
    }),
  readiness: () => api<Readiness[]>('/training-evidence/readiness'),
}
