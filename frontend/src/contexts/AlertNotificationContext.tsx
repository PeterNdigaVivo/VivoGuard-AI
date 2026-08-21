// Global real-time alert notifications: polls for new alerts, plays a
// synthesized horn + shows a browser notification for new URGENT alerts,
// and exposes mute + per-type settings (all persisted to localStorage).
//
// Mounted inside <Protected> (authenticated pages only). Reuses the existing
// /ws/alerts push for immediacy AND a 15s poll as a fallback.
import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
  type ReactNode,
} from 'react'
import { alerts as alertsApi, type Alert } from '@/api/alerts'
import { useAlertSound } from '@/hooks/useAlertSound'

export interface NotifSettings {
  urgentOnly: boolean       // notify only on URGENT (default)
  allAlerts: boolean        // notify on every new alert
  sound: boolean            // play the horn
  browserNotif: boolean     // show a browser Notification
}

const DEFAULTS: NotifSettings = { urgentOnly: true, allAlerts: false, sound: true, browserNotif: true }
const SETTINGS_KEY = 'vg_notif_settings'
const MUTE_KEY = 'vg_notif_muted'
const NOTIFIED_KEY = 'vg_notif_ids'
const POLL_MS = 15_000
const MAX_TRACKED = 300

// Inline "logo" so the browser notification has an icon with no file dep.
const NOTIF_ICON = 'data:image/svg+xml,' + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">' +
  '<rect width="64" height="64" rx="12" fill="#0f172a"/>' +
  '<text x="32" y="46" font-size="34" text-anchor="middle">🎥</text></svg>')

interface Ctx {
  muted: boolean
  toggleMute: () => void
  settings: NotifSettings
  setSetting: (k: keyof NotifSettings, v: boolean) => void
  unreadUrgent: number
  requestPermission: () => void
  testSound: () => void
}
const AlertNotifCtx = createContext<Ctx | null>(null)

function loadSettings(): NotifSettings {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY)
    if (raw) return { ...DEFAULTS, ...(JSON.parse(raw) as Partial<NotifSettings>) }
  } catch { /* ignore */ }
  return DEFAULTS
}
function loadNotified(): Set<number> {
  try {
    const raw = localStorage.getItem(NOTIFIED_KEY)
    if (raw) return new Set(JSON.parse(raw) as number[])
  } catch { /* ignore */ }
  return new Set<number>()
}

export function AlertNotificationProvider({ children }: { children: ReactNode }) {
  const [muted, setMuted] = useState<boolean>(() => {
    try { return localStorage.getItem(MUTE_KEY) === '1' } catch { return false }
  })
  const [settings, setSettings] = useState<NotifSettings>(loadSettings)
  const [unreadUrgent, setUnreadUrgent] = useState(0)

  const playSound = useAlertSound()
  const notifiedRef = useRef<Set<number>>(loadNotified())
  const seededRef = useRef(false)
  // Keep the latest mute/settings readable by the async poller without
  // re-subscribing the interval/WS every change.
  const mutedRef = useRef(muted); mutedRef.current = muted
  const settingsRef = useRef(settings); settingsRef.current = settings

  const toggleMute = useCallback(() => {
    setMuted(m => {
      const n = !m
      try { localStorage.setItem(MUTE_KEY, n ? '1' : '0') } catch { /* ignore */ }
      return n
    })
  }, [])

  const setSetting = useCallback((k: keyof NotifSettings, v: boolean) => {
    setSettings(s => {
      const next: NotifSettings = { ...s, [k]: v }
      // urgentOnly / allAlerts are mutually-exclusive scopes.
      if (k === 'allAlerts' && v) next.urgentOnly = false
      if (k === 'urgentOnly' && v) next.allAlerts = false
      try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(next)) } catch { /* ignore */ }
      return next
    })
  }, [])

  const requestPermission = useCallback(() => {
    try {
      if ('Notification' in window && Notification.permission === 'default') {
        void Notification.requestPermission()
      }
    } catch { /* ignore */ }
  }, [])

  const testSound = useCallback(() => { if (!mutedRef.current) playSound() }, [playSound])

  const persistNotified = useCallback(() => {
    const arr = Array.from(notifiedRef.current)
    const trimmed = arr.slice(Math.max(0, arr.length - MAX_TRACKED))
    notifiedRef.current = new Set(trimmed)
    try { localStorage.setItem(NOTIFIED_KEY, JSON.stringify(trimmed)) } catch { /* ignore */ }
  }, [])

  const showBrowserNotif = useCallback((a: Alert) => {
    try {
      if (!('Notification' in window) || Notification.permission !== 'granted') return
      const title = a.plain_title || a.title || a.detection_type || 'New alert'
      const n = new Notification(title, {
        body: a.body || '',
        icon: NOTIF_ICON,
        tag: `vg-alert-${a.id}`,
      })
      n.onclick = () => {
        try { window.focus() } catch { /* ignore */ }
        window.location.assign('/alerts')
      }
    } catch { /* ignore */ }
  }, [])

  const check = useCallback(async () => {
    let rows: Alert[]
    try {
      // order=recent → the backend returns the newest "new" alerts of ANY
      // severity. Without it the endpoint orders severity-first then LIMITs,
      // so a new low-severity alert is buried behind the backlog of urgent
      // ones and never reaches the notifier — the "no sound in All alerts
      // mode" bug. Client-side scoping (below) still narrows to urgent when
      // urgentOnly is set.
      rows = await alertsApi.list({ status: 'new', order: 'recent', limit: 100 })
    } catch { return }   // poll error — retry next tick
    const s = settingsRef.current
    const urgentNew = rows.filter(a => a.severity_label === 'URGENT')
    setUnreadUrgent(urgentNew.length)
    const scoped = s.allAlerts ? rows : urgentNew

    // First pass after (re)mount: treat the current backlog as already seen
    // so we don't alarm on load — only genuinely new alerts trigger.
    if (!seededRef.current) {
      scoped.forEach(a => notifiedRef.current.add(a.id))
      seededRef.current = true
      persistNotified()
      return
    }

    // Never turn a backfilled/stale database row into a "live" alarm. The
    // card remains in the feed (with its delivery-delay badge), but audible
    // and browser notifications are reserved for alerts created recently.
    const now = Date.now()
    const fresh = scoped.filter(a =>
      !notifiedRef.current.has(a.id)
      && now - new Date(a.created_at).getTime() <= 2 * 60 * 1000
    )
    if (fresh.length > 0) {
      if (s.sound && !mutedRef.current) playSound()            // one horn per batch
      if (s.browserNotif) fresh.slice(0, 3).forEach(showBrowserNotif)  // cap the burst
    }
    scoped.forEach(a => notifiedRef.current.add(a.id))
    persistNotified()
  }, [playSound, showBrowserNotif, persistNotified])

  useEffect(() => {
    requestPermission()
    void check()                                   // seed the notified set
    const id = window.setInterval(() => { void check() }, POLL_MS)
    const unsub = alertsApi.subscribe(() => { void check() })   // WS immediacy
    return () => { window.clearInterval(id); unsub() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const value = useMemo<Ctx>(() => ({
    muted, toggleMute, settings, setSetting, unreadUrgent, requestPermission, testSound,
  }), [muted, toggleMute, settings, setSetting, unreadUrgent, requestPermission, testSound])

  return <AlertNotifCtx.Provider value={value}>{children}</AlertNotifCtx.Provider>
}

export function useAlertNotifications(): Ctx {
  const c = useContext(AlertNotifCtx)
  if (!c) throw new Error('useAlertNotifications must be used within AlertNotificationProvider')
  return c
}
