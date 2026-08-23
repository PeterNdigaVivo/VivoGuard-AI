import { api } from './client'

export interface OdooAssurance {
  enabled: boolean
  mode: 'read_only'
  mapped: number
  unmapped: number
  mappings: Array<{
    store_id: number; store_name: string; mapped: boolean
    odoo_name: string | null; odoo_pos_config_id: number | null
    last_synced_at: string | null; sync_error: string | null
  }>
  sync: Array<{
    stream: string; last_success_at: string | null; consecutive_failures: number
    circuit_open_until: string | null; last_error: string | null
  }>
  till_conflicts: Array<{
    id: number; store_id: number; business_day: string; conflict_type: string
    camera_event_at: string | null; till_event_at: string | null; status: string
  }>
  conversion: Array<{
    store_id: number; period_start: string; footfall: number; transactions: number
    conversion_rate: number | null; data_quality_flag: boolean
  }>
  changing_room_reviews: Array<{
    id: number; store_id: number; camera_id: number | null; title: string; status: string
  }>
}

export const odooApi = {
  assurance: () => api<OdooAssurance>('/odoo/assurance'),
}
