// Reusable date-range picker with the operator presets requested:
//   today, yesterday, this_week, last_week, this_month, last_month,
//   last_7_days, last_30_days, last_90_days, last_year, custom.
//
// Returns ISO timestamps (UTC) so the backend can filter consistently.
// Compute boundaries in the BROWSER's local time so "yesterday" means
// the operator's yesterday — store dashboards are always read locally.

import { useState } from 'react'
import { Input, Select } from './ui/Primitives'

export type DateRangeKey =
  | 'today' | 'yesterday'
  | 'this_week' | 'last_week'
  | 'this_month' | 'last_month'
  | 'last_7_days' | 'last_30_days' | 'last_90_days' | 'last_year'
  | 'custom'

export interface DateRange {
  key: DateRangeKey
  since: string   // ISO 8601 — inclusive lower bound
  until: string   // ISO 8601 — exclusive upper bound (next-day boundary)
  label: string   // human-readable
}

const LABELS: Record<DateRangeKey, string> = {
  today:        'Today',
  yesterday:    'Yesterday',
  this_week:    'This week',
  last_week:    'Last week',
  this_month:   'This month',
  last_month:   'Last month',
  last_7_days:  'Last 7 days',
  last_30_days: 'Last 30 days',
  last_90_days: 'Last 90 days',
  last_year:    'Last year',
  custom:       'Custom range',
}

function startOfDay(d: Date): Date {
  const x = new Date(d); x.setHours(0, 0, 0, 0); return x
}
function addDays(d: Date, n: number): Date {
  const x = new Date(d); x.setDate(x.getDate() + n); return x
}
function startOfWeek(d: Date): Date {
  const x = startOfDay(d)
  const dow = (x.getDay() + 6) % 7   // Mon=0
  return addDays(x, -dow)
}
function startOfMonth(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), 1)
}

export function rangeFor(key: DateRangeKey, customSince?: string, customUntil?: string): DateRange {
  const now = new Date()
  let since: Date, until: Date
  switch (key) {
    case 'today':        since = startOfDay(now);                          until = addDays(since, 1);   break
    case 'yesterday':    since = addDays(startOfDay(now), -1);             until = startOfDay(now);     break
    case 'this_week':    since = startOfWeek(now);                          until = addDays(since, 7);   break
    case 'last_week':    since = addDays(startOfWeek(now), -7);             until = startOfWeek(now);    break
    case 'this_month':   since = startOfMonth(now);
                         until = startOfMonth(addDays(startOfMonth(now), 32)); break
    case 'last_month':   { const thisMonth = startOfMonth(now)
                           const lastMonth = startOfMonth(addDays(thisMonth, -1))
                           since = lastMonth; until = thisMonth }          break
    case 'last_7_days':  since = addDays(startOfDay(now), -7);             until = addDays(startOfDay(now), 1); break
    case 'last_30_days': since = addDays(startOfDay(now), -30);            until = addDays(startOfDay(now), 1); break
    case 'last_90_days': since = addDays(startOfDay(now), -90);            until = addDays(startOfDay(now), 1); break
    case 'last_year':    since = new Date(now.getFullYear() - 1, 0, 1);
                         until = new Date(now.getFullYear(),     0, 1);    break
    case 'custom':
      since = customSince ? new Date(customSince) : startOfDay(now)
      until = customUntil ? new Date(customUntil) : addDays(startOfDay(now), 1)
      break
  }
  return { key, since: since.toISOString(), until: until.toISOString(), label: LABELS[key] }
}

export default function DateRangePicker({ value, onChange }: {
  value: DateRange
  onChange: (r: DateRange) => void
}) {
  const [customSince, setCustomSince] = useState('')
  const [customUntil, setCustomUntil] = useState('')

  return (
    <div className="flex items-center gap-2">
      <Select value={value.key}
              onChange={e => {
                const k = e.target.value as DateRangeKey
                onChange(rangeFor(k, customSince, customUntil))
              }}>
        {(Object.keys(LABELS) as DateRangeKey[]).map(k => (
          <option key={k} value={k}>{LABELS[k]}</option>
        ))}
      </Select>
      {value.key === 'custom' && (
        <>
          <Input type="datetime-local" value={customSince}
                 onChange={e => {
                   setCustomSince(e.target.value)
                   onChange(rangeFor('custom', e.target.value, customUntil))
                 }} />
          <span className="text-xs text-slate-500">→</span>
          <Input type="datetime-local" value={customUntil}
                 onChange={e => {
                   setCustomUntil(e.target.value)
                   onChange(rangeFor('custom', customSince, e.target.value))
                 }} />
        </>
      )}
    </div>
  )
}
