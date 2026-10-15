// Model-evaluation test set — system admins only (docs/MODEL_EVALUATION.md).
import { api } from '@/api/client'

export type LabelKind = 'people' | 'clip'

export interface EvalSampleRef {
  id: number
  kind: LabelKind
  camera_id: number | null
  captured_at: string | null
  media_type: string
}

export interface EvalSummary {
  collection_enabled: boolean
  targets: { per_alert_type: number; people: number; clips: number }
  retention_days: number
  alerts: Record<string, { total: number; true: number; false: number }>
  people: Record<string, number>
  clips: Record<string, number>
}

export const evaluation = {
  summary: () => api<EvalSummary>('/eval/summary'),
  next: (kind: LabelKind) => api<EvalSampleRef | null>(`/eval/next?kind=${kind}`),
  label: (id: number, body: { points?: number[][]; count?: number; skip?: boolean }) =>
    api<{ id: number; status: string }>(`/eval/samples/${id}/label`, { method: 'POST', body }),
  /** The test image/clip as an object URL (the request carries the token). */
  mediaUrl: async (id: number): Promise<string> => {
    const res = await api<Response>(`/eval/samples/${id}/media`, { raw: true })
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
    return URL.createObjectURL(await res.blob())
  },
}
