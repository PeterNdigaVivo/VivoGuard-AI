// Shared shell — sidebar nav + top bar. Used by every authenticated page.

import { useEffect, useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useAuth } from '@/auth/AuthContext'
import { alerts as alertsApi } from '@/api/alerts'

const NAV = [
  { to: '/chain',    label: 'Chain' },
  { to: '/compare',  label: 'Compare' },
  { to: '/stores',   label: 'Stores' },
  { to: '/search',   label: 'Search' },
  { to: '/cameras',  label: 'Cameras' },
  { to: '/live',     label: 'Live View' },
  { to: '/alerts',   label: 'Alerts' },
  { to: '/reports',  label: 'Reports' },
  { to: '/analytics/roi', label: 'Value Report' },
  { to: '/stockroom', label: 'Stockroom Log' },
  { to: '/campaigns', label: 'Campaigns' },
  { to: '/training', label: 'AI Training' },
  { to: '/detectors',label: 'Detectors' },
  { to: '/models',   label: 'Models' },
  { to: '/users',    label: 'Users' },
  { to: '/system',   label: 'System' },
]

export default function Layout() {
  const { user, logout } = useAuth()
  const nav = useNavigate()

  // Unread-urgent badge on the Alerts nav item. Polls every 30s,
  // refreshes on every websocket push, AND listens for the local
  // `vg:alert-resolved` event so clicking "I handled this" / "Resolve
  // all" decrements the badge before the next poll lands.
  const [urgentBadge, setUrgentBadge] = useState(0)
  useEffect(() => {
    let alive = true
    const refresh = () => alertsApi.summary()
      .then(s => { if (alive) setUrgentBadge(s.unread_urgent) })
      .catch(() => {})
    refresh()
    const t = setInterval(refresh, 30_000)
    const unsub = alertsApi.subscribe(() => refresh())
    const onResolved = (e: Event) => {
      const detail = (e as CustomEvent).detail
      const bulk = typeof detail?.bulk === 'number' ? detail.bulk : 1
      setUrgentBadge(b => Math.max(0, b - bulk))
      // Re-fetch in 2s to reconcile with the server's view.
      setTimeout(refresh, 2000)
    }
    window.addEventListener('vg:alert-resolved', onResolved)
    return () => {
      alive = false; clearInterval(t); unsub()
      window.removeEventListener('vg:alert-resolved', onResolved)
    }
  }, [])

  return (
    <div className="h-full flex">
      {/* Sidebar */}
      <aside className="w-56 bg-slate-900 text-slate-200 flex flex-col">
        <div className="px-4 py-5 text-lg font-semibold border-b border-slate-700">
          🎥 VivoGuard
        </div>
        <nav className="flex-1 p-2 space-y-1">
          {NAV.map(item => (
            <NavLink key={item.to} to={item.to}
              className={({ isActive }) =>
                'flex items-center justify-between rounded px-3 py-2 text-sm ' +
                (isActive ? 'bg-sky-700 text-white' : 'hover:bg-slate-800')}>
              <span>{item.label}</span>
              {item.to === '/alerts' && urgentBadge > 0 && (
                <span className="ml-2 inline-flex items-center justify-center min-w-[20px] h-5 px-1.5
                                 rounded-full bg-red-600 text-white text-[11px] font-bold"
                      title="Unresolved urgent alerts">
                  {urgentBadge > 99 ? '99+' : urgentBadge}
                </span>
              )}
            </NavLink>
          ))}
        </nav>
        <div className="p-3 border-t border-slate-700 text-xs text-slate-400">
          <div className="truncate">{user?.email}</div>
          <div className="text-slate-500 mb-2 capitalize">{user?.role}</div>
          <button onClick={() => { logout(); nav('/login') }}
                  className="text-sky-400 hover:underline">Sign out</button>
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 overflow-auto bg-slate-100">
        <Outlet />
      </main>
    </div>
  )
}
