// Shared shell — sidebar nav + top bar. Used by every authenticated page.

import { useEffect, useMemo, useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useAuth } from '@/auth/AuthContext'
import { useTheme } from '@/contexts/ThemeContext'
import AlertNotificationBell from '@/components/AlertNotificationBell'
import { alerts as alertsApi, type Alert } from '@/api/alerts'
import { isSystemAdmin } from '@/lib/systemAdmins'

const NAV: { to: string; label: string; systemAdminOnly?: boolean }[] = [
  { to: '/chain',    label: 'Chain' },
  { to: '/compare',  label: 'Compare' },
  { to: '/stores',   label: 'Stores' },
  { to: '/search',   label: 'Search' },
  { to: '/cameras',  label: 'Cameras' },
  { to: '/live',     label: 'Live View' },
  { to: '/alerts',   label: 'Alerts' },
  { to: '/reports',  label: 'Reports' },
  { to: '/analytics/roi', label: 'Value Report' },
  // Hidden from the sidebar (unused, zero records). Routes,
  // endpoints, and page components are intentionally left in place
  // so this is fully reversible — just uncomment when needed.
  // { to: '/stockroom', label: 'Stockroom Log' },
  // { to: '/campaigns', label: 'Campaigns' },
  { to: '/training', label: 'AI Training' },
  // Visible ONLY to the platform operators in lib/systemAdmins.ts —
  // hidden from everyone else, including regular admins.
  { to: '/system-health', label: '💓 System Health', systemAdminOnly: true },
  { to: '/odoo-assurance', label: '🔗 Odoo Assurance', systemAdminOnly: true },
  { to: '/ai-progress', label: '📈 AI Progress' },
  { to: '/ai-learning', label: 'AI Learning' },
  { to: '/sprint',      label: 'Sprint' },
  { to: '/detectors',label: 'Detectors' },
  { to: '/models',   label: 'Models' },
  { to: '/users',    label: 'Users' },
  { to: '/system',   label: 'System' },
  { to: '/agents',   label: 'Agents' },
]

export default function Layout() {
  const { user, logout } = useAuth()
  const { theme, toggle } = useTheme()
  const nav = useNavigate()

  // Sidebar badge + the chain-wide UrgentRibbon both read from
  // /alerts/summary. Single poller, single source of truth.
  const [urgentBadge, setUrgentBadge] = useState(0)
  const [urgentCount, setUrgentCount] = useState(0)
  const [criticalCount, setCriticalCount] = useState(0)
  useEffect(() => {
    let alive = true
    const refresh = () => alertsApi.summary()
      .then(s => {
        if (!alive) return
        setUrgentBadge(s.unread_urgent)
        setUrgentCount(s.urgent ?? 0)
        setCriticalCount(s.critical_today ?? 0)
      })
      .catch(() => {})
    refresh()
    const t = setInterval(refresh, 30_000)
    const unsub = alertsApi.subscribe(() => refresh())
    const onResolved = (e: Event) => {
      const detail = (e as CustomEvent).detail
      const bulk = typeof detail?.bulk === 'number' ? detail.bulk : 1
      setUrgentBadge(b => Math.max(0, b - bulk))
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
      {/* Sidebar — always dark (bg-slate-900, no dark: variant) in both themes. */}
      <aside className="w-56 shrink-0 bg-slate-900 text-slate-200 flex flex-col">
        <div className="px-4 py-5 text-lg font-semibold border-b border-slate-700 shrink-0">
          🎥 VivoGuard
        </div>
        {/* min-h-0 + overflow-y-auto so a long nav scrolls instead of pushing
            the bottom items (Users / System / Agents) under the footer. */}
        <nav className="flex-1 min-h-0 overflow-y-auto p-2 space-y-1">
          {NAV.filter(item =>
            !item.systemAdminOnly || isSystemAdmin(user?.email),
          ).map(item => (
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
        <div className="shrink-0 p-3 border-t border-slate-700 text-xs text-slate-400">
          {/* Light / dark toggle: ☀️ in dark → go light, 🌙 in light → go dark. */}
          <button onClick={toggle}
                  title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
                  aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
                  className="mb-3 flex items-center gap-2 text-slate-300 hover:text-white transition-colors">
            <span className="text-base leading-none">{theme === 'dark' ? '☀️' : '🌙'}</span>
            <span>{theme === 'dark' ? 'Light mode' : 'Dark mode'}</span>
          </button>
          <div className="truncate">{user?.email}</div>
          <div className="text-slate-500 mb-2 capitalize">{user?.role}</div>
          <button onClick={() => { logout(); nav('/login') }}
                  className="text-sky-400 hover:underline">Sign out</button>
        </div>
      </aside>

      {/* Floating alert-notification bell — shows on every page. */}
      <AlertNotificationBell />

      {/* Main */}
      <main className="flex-1 overflow-auto bg-slate-100 dark:bg-slate-950 transition-colors duration-200">
        <UrgentRibbon urgent={urgentCount} critical={criticalCount} />
        <Outlet />
      </main>
    </div>
  )
}


// ---------------------------------------------------------------
// UrgentRibbon — chain-wide red banner at the top of the main
// panel when one or more URGENT / CRITICAL alerts are open.
// ---------------------------------------------------------------

const RIBBON_DISMISS_KEY = 'vg_ribbon_dismissed_until'
const RIBBON_DISMISS_MS  = 10 * 60 * 1000

function UrgentRibbon({ urgent, critical }: { urgent: number; critical: number }) {
  const nav = useNavigate()
  const total = Math.max(urgent, critical)
  // Pull a few alert titles so the "2–5 alerts" banner can name them.
  // Cheap — limit=5, only re-runs when `total` changes.
  const [titles, setTitles] = useState<string[]>([])
  useEffect(() => {
    if (total < 2 || total > 5) { setTitles([]); return }
    let alive = true
    alertsApi.list({ limit: 5 })
      .then((rows: Alert[]) => {
        if (!alive) return
        const urgents = rows
          .filter(a => a.severity_label === 'URGENT' && a.status === 'new')
          .map(a => a.plain_title ?? a.title ?? a.detection_type ?? 'Alert')
          .filter(Boolean)
          .slice(0, 3)
        setTitles(urgents)
      })
      .catch(() => setTitles([]))
    return () => { alive = false }
  }, [total])

  // Dismiss state — read from localStorage, plus a 1Hz tick so the
  // ribbon re-appears automatically once the 10-minute window ends.
  const [dismissUntil, setDismissUntil] = useState<number>(() => {
    try {
      const raw = localStorage.getItem(RIBBON_DISMISS_KEY)
      const n = raw ? Number(raw) : 0
      return Number.isFinite(n) ? n : 0
    } catch { return 0 }
  })
  // Auto-revive when a NEW urgent arrives after the user dismissed.
  // We compare the latest urgent count against the snapshot taken at
  // dismiss time — if the count grew, the ribbon ignores the dismiss.
  const [dismissBaseline, setDismissBaseline] = useState<number>(0)
  const [, forceTick] = useState(0)
  useEffect(() => {
    const t = setInterval(() => forceTick(n => n + 1), 1000)
    return () => clearInterval(t)
  }, [])

  const now = Date.now()
  const stillDismissed = now < dismissUntil && total <= dismissBaseline
  const visible = total > 0 && !stillDismissed
  const message = useMemo(() => {
    if (total === 1) return '1 URGENT ALERT NEEDS ATTENTION'
    if (total > 5)   return `${total} URGENT ALERTS NEED IMMEDIATE ATTENTION`
    if (titles.length)
      return `${total} URGENT ALERTS — ${titles.join(' · ')}`
    return `${total} URGENT ALERTS NEED ATTENTION`
  }, [total, titles])

  if (!visible) return null

  const shouldPulse = total > 3

  return (
    <div
      role="alert"
      style={{ backgroundColor: '#dc2626' }}
      className={
        'sticky top-0 z-40 text-white px-4 py-2 flex items-center gap-3 ' +
        'shadow-md animate-vg-slide-down ' +
        (shouldPulse ? 'animate-pulse' : '')
      }
    >
      <span className="text-base">🚨</span>
      <span className="text-sm font-semibold flex-1 truncate">{message}</span>
      <button
        onClick={() => nav('/alerts')}
        className="px-3 py-1 rounded bg-white text-red-700 text-xs font-semibold hover:bg-red-50">
        View Alerts
      </button>
      <button
        onClick={() => {
          const until = Date.now() + RIBBON_DISMISS_MS
          setDismissUntil(until)
          setDismissBaseline(total)
          try { localStorage.setItem(RIBBON_DISMISS_KEY, String(until)) } catch {}
        }}
        title="Hide for 10 minutes — re-appears if a new urgent alert fires."
        className="text-white/80 hover:text-white text-xl leading-none px-2">
        ×
      </button>

      {/* Slide-down keyframes injected inline so we don't need to
          touch tailwind.config.js or the global stylesheet. */}
      <style>{`
        @keyframes vg-ribbon-slide {
          from { transform: translateY(-100%); opacity: 0 }
          to   { transform: translateY(0);     opacity: 1 }
        }
        .animate-vg-slide-down { animation: vg-ribbon-slide 240ms ease-out both; }
      `}</style>
    </div>
  )
}
