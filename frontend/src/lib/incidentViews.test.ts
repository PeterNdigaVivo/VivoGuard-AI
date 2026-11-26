import { describe, expect, it } from 'vitest'

import type { IncidentSummary } from '@/api/incidents'
import { VIEWS, bulkConfirmText, countFor, policyReasonText, shortAge } from './incidentViews'

const S: IncidentSummary = {
  urgent: 2, attention: 3, review: 4, resolved: 5, open: 5, all: 14,
  avg_response_seconds: 90, lifecycle_enabled: true,
}

describe('incident views', () => {
  it('has exactly the five agreed views, in order', () => {
    expect(VIEWS.map(v => v.label)).toEqual(
      ['Open Incidents', 'Needs Attention', 'Review Queue', 'Resolved', 'All'])
  })

  it('reads every count from the server summary, never from loaded rows', () => {
    expect(VIEWS.map(v => countFor(v.key, S))).toEqual([5, 3, 4, 5, 14])
    expect(countFor('open', null)).toBeNull()
  })

  it('only offers bulk resolve on open views', () => {
    expect(VIEWS.filter(v => v.bulkResolvable).map(v => v.key))
      .toEqual(['open', 'attention', 'review'])
  })

  it('states the exact number in the bulk confirmation', () => {
    const text = bulkConfirmText(3, 'Open Incidents', 'all stores', 'today')
    expect(text).toContain('exactly 3 incidents')
    expect(text).toContain('nothing will be resolved')
    expect(bulkConfirmText(1, 'Review Queue', 'Yaya', 'today')).toContain('exactly 1 incident ')
  })

  it('formats ages and explains policy reasons in plain words', () => {
    expect([shortAge(5), shortAge(125), shortAge(7200), shortAge(200000), shortAge(null)])
      .toEqual(['5s', '2m', '2h', '2d', '—'])
    expect(policyReasonText('expected_early_staff')).toMatch(/staff/)
    expect(policyReasonText('raw_review:not_confirmed_across_frames')).toMatch(/review/)
    expect(policyReasonText('unknown_code')).toBeNull()
  })
})
