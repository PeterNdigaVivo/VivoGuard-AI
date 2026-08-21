import { api } from './client'

export type AgentStatus = 'ok' | 'warning' | 'critical' | string

export interface AgentReport {
  id: number
  agent_name: string
  run_at: string | null
  status: AgentStatus
  findings: unknown
  actions_taken: unknown
  gaps: unknown
  duration_ms: number | null
  error_message: string | null
}

export interface AgentLatest {
  agent_name: string
  report: AgentReport | null
}

export interface AgentScorecard {
  agent_name: string
  owner: string
  score: number
  compliant: boolean
  active_critical_override: boolean
  breaches: string[]
  completed_runs: number
  expected_runs: number
  run_coverage: number
  completion_reliability: number
  valid_output_rate: number
}

export function fetchLatest(): Promise<{ agents: AgentLatest[] }> {
  return api('/agents/latest')
}

export function fetchScorecards(): Promise<{ target: number; scorecards: AgentScorecard[] }> {
  return api('/agents/scorecards?window_hours=24')
}

export function fetchReports(
  params: { agent?: string; status?: string; limit?: number } = {},
): Promise<{ reports: AgentReport[] }> {
  const q = new URLSearchParams()
  if (params.agent) q.set('agent', params.agent)
  if (params.status) q.set('status', params.status)
  if (params.limit) q.set('limit', String(params.limit))
  const qs = q.toString()
  return api(`/agents/reports${qs ? `?${qs}` : ''}`)
}

export function runAgent(
  name: string,
): Promise<{ queued: boolean; agent: string; task_id: string | null }> {
  return api(`/agents/${name}/run`, { method: 'POST' })
}
