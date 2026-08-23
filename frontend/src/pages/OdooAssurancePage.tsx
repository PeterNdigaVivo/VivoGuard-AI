import { useEffect, useState } from 'react'
import { useAuth } from '@/auth/AuthContext'
import { odooApi, type OdooAssurance } from '@/api/odoo'
import { isSystemAdmin } from '@/lib/systemAdmins'

export default function OdooAssurancePage() {
  const { user } = useAuth()
  const [data, setData] = useState<OdooAssurance | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!isSystemAdmin(user?.email)) return
    odooApi.assurance().then(setData).catch(e => setError(e?.response?.data?.detail || String(e)))
  }, [user?.email])

  if (!isSystemAdmin(user?.email)) return <div className="p-8 text-red-700">System-admin access required.</div>
  if (error) return <div className="p-8 text-red-700">{error}</div>
  if (!data) return <div className="p-8 text-slate-500">Loading Odoo assurance…</div>

  const qualityFlags = data.conversion.filter(row => row.data_quality_flag).length
  return (
    <div className="p-6 space-y-6 text-slate-900 dark:text-slate-100">
      <div>
        <h1 className="text-2xl font-semibold">Odoo Assurance</h1>
        <p className="text-sm text-slate-500">Read-only, advisory integration. Odoo context never suppresses CCTV alerts.</p>
      </div>
      {!data.enabled && <div className="rounded border border-amber-300 bg-amber-50 p-3 text-amber-900">
        Sync is safely disabled. Complete and validate the store mapping before enabling it.
      </div>}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        {[['Mapped', data.mapped], ['Unmapped', data.unmapped],
          ['Till conflicts', data.till_conflicts.length], ['Conversion flags', qualityFlags],
          ['Changing-room reviews', data.changing_room_reviews.length]].map(([label, value]) =>
          <div key={String(label)} className="rounded bg-white dark:bg-slate-900 p-4 shadow-sm">
            <div className="text-xs text-slate-500">{label}</div><div className="text-2xl font-semibold">{value}</div>
          </div>)}
      </div>
      <section className="rounded bg-white dark:bg-slate-900 p-4 shadow-sm overflow-auto">
        <h2 className="font-semibold mb-3">Store mapping</h2>
        <table className="w-full text-sm"><thead><tr className="text-left border-b">
          <th className="py-2">VivoGuard</th><th>Odoo POS</th><th>Status</th><th>Last sync</th>
        </tr></thead><tbody>{data.mappings.map(row => <tr key={row.store_id} className="border-b border-slate-100 dark:border-slate-800">
          <td className="py-2">{row.store_name}</td><td>{row.odoo_name || '—'}</td>
          <td className={row.mapped ? 'text-emerald-600' : 'text-red-600'}>{row.mapped ? 'Mapped' : 'Unmapped'}</td>
          <td>{row.last_synced_at ? new Date(row.last_synced_at).toLocaleString() : '—'}</td>
        </tr>)}</tbody></table>
      </section>
      <section className="rounded bg-white dark:bg-slate-900 p-4 shadow-sm">
        <h2 className="font-semibold mb-3">Sync health</h2>
        {data.sync.length === 0 ? <p className="text-sm text-slate-500">No sync has run.</p> : data.sync.map(row =>
          <div key={row.stream} className="text-sm py-2 border-b border-slate-100 dark:border-slate-800">
            <span className="font-medium">{row.stream}</span> — {row.last_success_at ? `last success ${new Date(row.last_success_at).toLocaleString()}` : 'never successful'}
            {row.last_error && <span className="text-red-600"> · {row.last_error}</span>}
          </div>)}
      </section>
    </div>
  )
}
