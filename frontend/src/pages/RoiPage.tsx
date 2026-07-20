// VivoGuard Value Report — plain-English summary of incidents
// caught + an estimated KES value for the current month.
//
// Reads /analytics/roi which composes the figures from real
// detection_event + alert counts and per-incident KES values
// configurable via the .env (ROI_THEFT_PER_INCIDENT_KES, etc).

import { useEffect, useState } from 'react'
import { Card, PageHeader } from '@/components/ui/Primitives'
import { api } from '@/api/client'

interface RoiResponse {
  month_label: string
  as_of: string
  incidents: {
    theft_alerts:                   number
    theft_confirmed:                number
    unauthorised_access:            number
    queue_issues_resolved:          number
    uniform_compliance_change_pts:  number
  }
  estimated_value_kes: {
    theft_prevention:    number
    operational_savings: number
    total:               number
  }
  monthly_cost_kes:  number
  roi_multiple:      number | null
  per_incident_kes:  { theft: number; unauthorised: number; queue: number }
}

function fmtKES(n: number): string {
  // KES values are always whole shillings in our system. Use the
  // locale's grouping so 245000 reads as 245,000 at a glance.
  return new Intl.NumberFormat('en-KE').format(Math.round(n))
}

export default function RoiPage() {
  const [data, setData] = useState<RoiResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    api<RoiResponse>('/analytics/roi')
      .then(d => { if (alive) setData(d) })
      .catch(e => { if (alive) setError(String(e)) })
    return () => { alive = false }
  }, [])

  return (
    <div className="p-6 space-y-4 max-w-3xl">
      <PageHeader
        title="VivoGuard Value Report"
        actions={data && (
          <span className="text-xs text-slate-500 dark:text-slate-300">
            {data.month_label} · updated {new Date(data.as_of).toLocaleTimeString()}
          </span>
        )} />

      {error && (
        <Card className="p-4 text-sm text-red-600">
          Could not load value report. {error}
        </Card>
      )}

      {!data && !error && (
        <Card className="p-8 text-center text-slate-400 dark:text-slate-400">Loading…</Card>
      )}

      {data && (
        <>
          {/* Incidents caught */}
          <Card className="p-5">
            <div className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-300 mb-3">
              Incidents Caught This Month
            </div>
            <ul className="space-y-2 text-sm">
              <li className="flex items-baseline gap-2">
                <span>🔴</span>
                <span className="flex-1 text-slate-700 dark:text-slate-200">Theft alerts</span>
                <span className="tabular-nums font-semibold">
                  {data.incidents.theft_alerts}
                </span>
                <span className="text-xs text-slate-500 dark:text-slate-300">
                  ({data.incidents.theft_confirmed} confirmed)
                </span>
              </li>
              <li className="flex items-baseline gap-2">
                <span>🟠</span>
                <span className="flex-1 text-slate-700 dark:text-slate-200">Unauthorized access prevented</span>
                <span className="tabular-nums font-semibold">
                  {data.incidents.unauthorised_access}
                </span>
              </li>
              <li className="flex items-baseline gap-2">
                <span>🟡</span>
                <span className="flex-1 text-slate-700 dark:text-slate-200">Queue issues resolved</span>
                <span className="tabular-nums font-semibold">
                  {data.incidents.queue_issues_resolved}
                </span>
              </li>
              <li className="flex items-baseline gap-2">
                <span>✅</span>
                <span className="flex-1 text-slate-700 dark:text-slate-200">Staff uniform compliance change</span>
                <span className={'tabular-nums font-semibold ' + (
                  data.incidents.uniform_compliance_change_pts > 0 ? 'text-emerald-600'
                  : data.incidents.uniform_compliance_change_pts < 0 ? 'text-red-600'
                  : 'text-slate-600 dark:text-slate-300')}>
                  {data.incidents.uniform_compliance_change_pts > 0 ? '+' : ''}
                  {data.incidents.uniform_compliance_change_pts} pts vs last month
                </span>
              </li>
            </ul>
          </Card>

          {/* Estimated value */}
          <Card className="p-5">
            <div className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-300 mb-3">
              Estimated Value Delivered
            </div>
            <table className="w-full text-sm">
              <tbody>
                <tr className="border-b dark:border-slate-800">
                  <td className="py-1.5 text-slate-700 dark:text-slate-200">Theft prevention</td>
                  <td className="py-1.5 text-right tabular-nums font-semibold">
                    KES {fmtKES(data.estimated_value_kes.theft_prevention)}
                  </td>
                </tr>
                <tr className="border-b dark:border-slate-800">
                  <td className="py-1.5 text-slate-700 dark:text-slate-200">Operational savings</td>
                  <td className="py-1.5 text-right tabular-nums font-semibold">
                    KES {fmtKES(data.estimated_value_kes.operational_savings)}
                  </td>
                </tr>
                <tr>
                  <td className="py-2 text-slate-900 dark:text-slate-100 font-medium">
                    Total estimated value this month
                  </td>
                  <td className="py-2 text-right tabular-nums text-lg font-semibold text-emerald-700">
                    KES {fmtKES(data.estimated_value_kes.total)}
                  </td>
                </tr>
              </tbody>
            </table>
          </Card>

          {/* Cost + ROI */}
          <Card className="p-5">
            <div className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-300 mb-3">
              Return on Investment
            </div>
            <div className="grid grid-cols-2 gap-4 text-sm">
              <div>
                <div className="text-slate-500 dark:text-slate-300 text-xs">VivoGuard cost / month</div>
                <div className="text-lg font-semibold tabular-nums">
                  KES {fmtKES(data.monthly_cost_kes)}
                </div>
              </div>
              <div>
                <div className="text-slate-500 dark:text-slate-300 text-xs">ROI</div>
                <div className="text-lg font-semibold tabular-nums text-emerald-700">
                  {data.roi_multiple == null
                    ? '—'
                    : `${data.roi_multiple}× return on investment`}
                </div>
              </div>
            </div>
          </Card>

          {/* Methodology — operators need to trust the numbers, so
              expose the per-incident KES assumptions used. */}
          <Card className="p-4 text-xs text-slate-500 dark:text-slate-300">
            <div className="font-medium text-slate-700 dark:text-slate-200 mb-1">How we estimate the value</div>
            <ul className="space-y-0.5">
              <li>• Theft prevention: confirmed shrinkage alerts × KES {fmtKES(data.per_incident_kes.theft)} per incident</li>
              <li>• Unauthorised access: trespass / intrusion / staff-zone alerts × KES {fmtKES(data.per_incident_kes.unauthorised)} per incident</li>
              <li>• Operational: resolved queue alerts × KES {fmtKES(data.per_incident_kes.queue)} per incident</li>
              <li>• Per-incident values are configurable in .env (ROI_*_PER_INCIDENT_KES).</li>
            </ul>
          </Card>
        </>
      )}
    </div>
  )
}
