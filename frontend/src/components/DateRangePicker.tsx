// Reusable date-range picker with the operator presets requested:
//   today, yesterday, this_week, last_week, this_month, last_month,
//   last_7_days, last_30_days, last_90_days, last_year, custom.
//
// Returns ISO timestamps (UTC) so the backend can filter consistently.
// CALENDAR BOUNDARIES ARE COMPUTED IN AFRICA/NAIROBI (EAT, UTC+3, no DST)
// — every store in the chain is on EAT-aligned local time. Using the
// browser's local clock (the previous behaviour) silently broke quick
// filters whenever the operator's device or container was on a
// different TZ, e.g. cloud-hosted dashboards on UTC: "Yesterday"
// would resolve to a UTC window that missed the first 3 hours of
// the EAT day and bled into 3 hours of the EAT day-after.

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
  since: string   // ISO 8601 — inclusive lower bound (EAT-day-aligned)
  until: string   // ISO 8601 — exclusive upper bound (next EAT-day boundary)
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


// ---------- EAT calendar helpers --------------------------------------
//
// EAT is UTC+3 year-round (Kenya, Uganda, Rwanda observe no DST), so a
// fixed offset string is safe and avoids an extra date library. Days
// roll over at the EAT midnight regardless of browser timezone.

const EAT_TZ     = 'Africa/Nairobi'
const EAT_OFFSET = '+03:00'

interface Ymd { y: number; m: number; d: number }

// Return today's EAT calendar Y/M/D + Mon=0..Sun=6 weekday.
function nowEat(): Ymd & { dow: number } {
  const fmt = new Intl.DateTimeFormat('en-CA', {
    timeZone: EAT_TZ,
    year: 'numeric', month: '2-digit', day: '2-digit', weekday: 'short',
  })
  const parts = fmt.formatToParts(new Date())
  const get = (t: string) => parts.find(p => p.type === t)?.value ?? ''
  const wk: Record<string, number> = {
    Mon: 0, Tue: 1, Wed: 2, Thu: 3, Fri: 4, Sat: 5, Sun: 6,
  }
  return {
    y:   Number(get('year')),
    m:   Number(get('month')),
    d:   Number(get('day')),
    dow: wk[get('weekday')] ?? 0,
  }
}

// Midnight EAT on the given Y/M/D as a real instant (in UTC under the hood).
function eatMidnight(ymd: Ymd): Date {
  const mm = String(ymd.m).padStart(2, '0')
  const dd = String(ymd.d).padStart(2, '0')
  return new Date(`${ymd.y}-${mm}-${dd}T00:00:00${EAT_OFFSET}`)
}

// Calendar arithmetic in UTC against a placeholder — keeps month/year
// rollovers honest without dragging in a date lib.
function addDays(ymd: Ymd, n: number): Ymd {
  const base = Date.UTC(ymd.y, ymd.m - 1, ymd.d)
  const next = new Date(base + n * 86400_000)
  return { y: next.getUTCFullYear(),
           m: next.getUTCMonth() + 1,
           d: next.getUTCDate() }
}

function startOfMonthYmd(ymd: Ymd): Ymd {
  return { y: ymd.y, m: ymd.m, d: 1 }
}

function nextMonthFirst(ymd: Ymd): Ymd {
  // Day 1 of next month — handle December → January.
  return ymd.m === 12
    ? { y: ymd.y + 1, m: 1,         d: 1 }
    : { y: ymd.y,     m: ymd.m + 1, d: 1 }
}


export function rangeFor(key: DateRangeKey, customSince?: string, customUntil?: string): DateRange {
  const t = nowEat()
  let since: Date, until: Date
  switch (key) {
    case 'today': {
      since = eatMidnight(t)
      until = eatMidnight(addDays(t, 1))
      break
    }
    case 'yesterday': {
      since = eatMidnight(addDays(t, -1))
      until = eatMidnight(t)
      break
    }
    case 'this_week': {
      const ws = addDays(t, -t.dow)              // Mon of this EAT week
      since = eatMidnight(ws)
      until = eatMidnight(addDays(ws, 7))
      break
    }
    case 'last_week': {
      const ws = addDays(t, -t.dow - 7)          // Mon of last EAT week
      since = eatMidnight(ws)
      until = eatMidnight(addDays(ws, 7))
      break
    }
    case 'this_month': {
      const first = startOfMonthYmd(t)
      since = eatMidnight(first)
      until = eatMidnight(nextMonthFirst(first))
      break
    }
    case 'last_month': {
      const thisFirst = startOfMonthYmd(t)
      const lastFirst = addDays(thisFirst, -1)   // last day of prev month
      since = eatMidnight(startOfMonthYmd(lastFirst))
      until = eatMidnight(thisFirst)
      break
    }
    // "Last N days" spans exactly N calendar days INCLUSIVE of today.
    // until is the half-open next-midnight (= end of today); since is the
    // start of the day (N-1) days ago. Using -N here spanned N+1 days.
    case 'last_7_days': {
      since = eatMidnight(addDays(t, -6))
      until = eatMidnight(addDays(t, 1))
      break
    }
    case 'last_30_days': {
      since = eatMidnight(addDays(t, -29))
      until = eatMidnight(addDays(t, 1))
      break
    }
    case 'last_90_days': {
      since = eatMidnight(addDays(t, -89))
      until = eatMidnight(addDays(t, 1))
      break
    }
    case 'last_year': {
      // Last calendar year — Jan 1 .. Dec 31 inclusive in EAT.
      since = eatMidnight({ y: t.y - 1, m: 1, d: 1 })
      until = eatMidnight({ y: t.y,     m: 1, d: 1 })
      break
    }
    case 'custom': {
      // Custom values come from <input type="datetime-local"> which
      // is local-time-without-zone. Interpret as EAT to stay
      // consistent with the presets.
      since = customSince
        ? new Date(`${customSince}:00${EAT_OFFSET}`)
        : eatMidnight(t)
      until = customUntil
        ? new Date(`${customUntil}:00${EAT_OFFSET}`)
        : eatMidnight(addDays(t, 1))
      break
    }
  }
  return { key, since: since.toISOString(), until: until.toISOString(),
           label: LABELS[key] }
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
