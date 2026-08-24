// Thin wrappers around the /training/* backend endpoints.
import { api } from './client'

export interface Dataset { id: number; name: string; description: string | null; classes_json: string[]; created_at: string }
export interface TrainingImage {
  id: number; dataset_id: number; camera_id: number | null; file_path: string
  labeled: boolean; split: string | null; captured_at: string | null
  source_kind: string; eligible_for_training: boolean; review_state: string
  simulation_run_id: string | null; evidence_source: string | null; synthetic: boolean | null
}
export interface AnnotationIn { class_label: string; bbox_json: number[]; verified?: boolean; auto_suggested?: boolean }
export interface AnnotationOut extends AnnotationIn { id: number; image_id: number }

export interface TrainingJob {
  id: number; model_name: string; dataset_id: number; status: string
  current_epoch: number; total_epochs: number; best_map50: number | null
  error_message: string | null; started_at: string | null; completed_at: string | null
  config_json: Record<string, unknown>
}

export interface AIModel {
  id: number; name: string; version: string; base_model: string; classes_json: string[]
  map50: number | null; map50_95: number | null; precision: number | null; recall: number | null
  weights_path: string; export_format: string | null; deployed: boolean; is_base: boolean
  training_job_id: number | null; created_at: string
  // Chain-training metadata (NULL on legacy per-store models).
  is_chain_model?: boolean
  detector_type?: string | null
  trained_on_stores?: number[] | null
  sample_count?: number | null
}

export const training = {
  listDatasets:  () => api<Dataset[]>('/training/datasets'),
  getDataset:    (id: number) => api<Dataset>(`/training/datasets/${id}`),
  createDataset: (body: { name: string; description?: string; classes?: string[] }) =>
    api<Dataset>('/training/datasets/create', { method: 'POST', body }),

  listImages:    (dsId: number) => api<TrainingImage[]>(`/training/images/${dsId}`),
  imageFileUrl:  (dsId: number, imgId: number) => `/api/training/images/${dsId}/${imgId}/file`,
  uploadImages:  (dsId: number, files: FileList) => {
    const fd = new FormData()
    fd.append('dataset_id', String(dsId))
    Array.from(files).forEach(f => fd.append('files', f))
    return api<TrainingImage[]>('/training/images/upload', { method: 'POST', form: fd })
  },
  captureFromCamera: (body: { camera_id: number; dataset_id: number; count?: number }) =>
    api<TrainingImage[]>('/training/images/capture', { method: 'POST', body }),

  autoSuggest:   (imageId: number) =>
    api<AnnotationIn[]>(`/training/images/${imageId}/auto-suggest`, { method: 'POST' }),

  listAnnotations: (imageId: number) =>
    api<AnnotationOut[]>(`/training/images/${imageId}/annotations`),

  saveAnnotations: (imageId: number, payload: AnnotationIn[]) =>
    api<AnnotationOut[]>(`/training/annotate?image_id=${imageId}`, { method: 'POST', body: payload }),

  reviewSimulationEvidence: (imageId: number, verdict: 'approve' | 'reject', rationale: string) =>
    api<{ training_image_id: number; verdict: string; eligible_for_training: boolean; review_state: string }>(
      `/training/images/${imageId}/independent-review`,
      { method: 'POST', body: { verdict, rationale } },
    ),

  startJob:      (body: Record<string, unknown>) =>
    api<TrainingJob>('/training/jobs/start', { method: 'POST', body }),
  jobStatus:     (id: number) => api<TrainingJob>(`/training/jobs/${id}/status`),

  listModels:    () => api<AIModel[]>('/training/models'),
  deployModel:   (id: number, cameraIds: number[]) =>
    api<number[]>(`/training/models/${id}/deploy`, { method: 'POST', body: { camera_ids: cameraIds } }),
  rollbackModel: (id: number) => api(`/training/models/${id}/rollback`, { method: 'POST' }),
  exportModel:   (id: number, format: 'torchscript' | 'onnx' | 'engine') =>
    api<{ path: string }>(`/training/models/${id}/export`, { method: 'POST', body: { format } }),
}
