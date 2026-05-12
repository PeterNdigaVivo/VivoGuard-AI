// Shared shell — sidebar nav + top bar. Used by every authenticated page.

import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useAuth } from '@/auth/AuthContext'

const NAV = [
  { to: '/chain',    label: 'Chain' },
  { to: '/stores',   label: 'Stores' },
  { to: '/cameras',  label: 'Cameras' },
  { to: '/live',     label: 'Live View' },
  { to: '/alerts',   label: 'Alerts' },
  { to: '/stockroom', label: 'Stockroom Log' },
  { to: '/training', label: 'AI Training' },
  { to: '/models',   label: 'Models' },
  { to: '/system',   label: 'System' },
]

export default function Layout() {
  const { user, logout } = useAuth()
  const nav = useNavigate()

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
                'block rounded px-3 py-2 text-sm ' +
                (isActive ? 'bg-sky-700 text-white' : 'hover:bg-slate-800')}>
              {item.label}
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
