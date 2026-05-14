// Natural-language event search. Type "show me people near fitting
// rooms after 6pm" — the backend parses the phrase into filters and
// returns matching DetectionEvents.

import { useState } from 'react'
import { Badge, Button, Card, Input, PageHeader } from '@/components/ui/Primitives'
import { api } from '@/api/client'

interface Result {
  id: number; timestamp: string; detection_type: string; confidence: number
  bbox_norm: number[]; camera_id: number; camera_name: string
  store_id: number | null; zone_id: number | null; zone_name: string | null
  thumbnail_path: string | null; extra: any
}
interface SearchResponse {
  parsed_filter: Record<string, any>
  matched_terms: string[]
  results: Result[]
  result_count: number
}

const EXAMPLES = [
  'show me people near the fitting rooms after 6pm',
  'weapons today at Sarit',
  'fights this week',
  'vehicles in last 24 hours',
  'queue events yesterday',
]

export default function SearchPage() {
  const [q, setQ] = useState('')
  const [busy, setBusy] = useState(false)
  const [data, setData] = useState<SearchResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function run(query?: string) {
    const text = (query ?? q).trim()
    if (!text) return
    setBusy(true); setError(null)
    try {
      const r = await api<SearchResponse>('/search', { method: 'POST', body: { q: text, limit: 200 } })
      setData(r)
      if (query) setQ(query)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="p-6">
      <PageHeader title="Search events" />

      <Card className="p-4 mb-4">
        <div className="flex gap-2">
          <Input className="flex-1"
                 placeholder='try: "weapons today at Sarit" or "people near fitting rooms after 6pm"'
                 value={q} onChange={e => setQ(e.target.value)}
                 onKeyDown={e => e.key === 'Enter' && run()} />
          <Button onClick={() => run()} disabled={busy || !q.trim()}>
            {busy ? 'Searching…' : 'Search'}
          </Button>
        </div>
        <div className="text-xs text-slate-500 mt-3">
          Try: {EXAMPLES.map(s => (
            <button key={s} onClick={() => run(s)}
                    className="mr-2 mb-1 inline-block text-sky-600 hover:underline">"{s}"</button>
          ))}
        </div>
        {error && <div className="text-red-600 text-sm mt-2">{error}</div>}
      </Card>

      {data && (
        <>
          <Card className="p-3 mb-4 text-sm">
            <div className="text-slate-500">
              Parsed filter:{' '}
              {data.matched_terms.length === 0
                ? <span className="text-slate-400">none — showing recent of all types</span>
                : data.matched_terms.map(t => <Badge key={t} color="sky">{t}</Badge>)}
            </div>
            <div className="mt-1 text-slate-700">{data.result_count} result{data.result_count === 1 ? '' : 's'}</div>
          </Card>

          <Card>
            <table className="w-full text-sm">
              <thead className="bg-slate-50 text-slate-600">
                <tr>
                  <th className="text-left p-3">When</th>
                  <th className="text-left p-3">Camera</th>
                  <th className="text-left p-3">Zone</th>
                  <th className="text-left p-3">Type</th>
                  <th className="text-left p-3">Confidence</th>
                  <th className="text-left p-3">Extra</th>
                </tr>
              </thead>
              <tbody>
                {data.results.map(r => (
                  <tr key={r.id} className="border-t hover:bg-slate-50">
                    <td className="p-3 whitespace-nowrap">{new Date(r.timestamp).toLocaleString()}</td>
                    <td className="p-3">{r.camera_name}</td>
                    <td className="p-3">{r.zone_name ?? '—'}</td>
                    <td className="p-3"><Badge color="sky">{r.detection_type}</Badge></td>
                    <td className="p-3">{r.confidence?.toFixed(2)}</td>
                    <td className="p-3 text-xs font-mono text-slate-500 max-w-md truncate"
                        title={JSON.stringify(r.extra)}>
                      {r.extra ? JSON.stringify(r.extra) : ''}
                    </td>
                  </tr>
                ))}
                {!data.results.length && (
                  <tr><td colSpan={6} className="p-8 text-center text-slate-500">No matches.</td></tr>
                )}
              </tbody>
            </table>
          </Card>
        </>
      )}
    </div>
  )
}
