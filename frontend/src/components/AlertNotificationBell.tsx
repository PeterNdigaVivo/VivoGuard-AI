// Floating alert-notification control, bottom-right on every authenticated
// page. Main button toggles mute (🔊/🔇) with an unread-urgent badge; the
// gear opens the settings panel.
import { useState } from 'react'
import { useAlertNotifications } from '@/contexts/AlertNotificationContext'

export default function AlertNotificationBell() {
  const { muted, toggleMute, settings, setSetting, unreadUrgent, requestPermission, testSound } =
    useAlertNotifications()
  const [open, setOpen] = useState(false)

  return (
    <div className="fixed bottom-6 right-6 z-[60] flex flex-col items-end gap-2">
      {open && (
        <div className="w-64 rounded-xl bg-white dark:bg-slate-800 shadow-xl
                        border border-slate-200 dark:border-slate-700 p-3
                        text-sm text-slate-700 dark:text-slate-200">
          <div className="font-semibold mb-2">🔔 Alert notifications</div>
          <Toggle label="Urgent alerts only" checked={settings.urgentOnly}
                  onChange={v => setSetting('urgentOnly', v)} />
          <Toggle label="All alerts" checked={settings.allAlerts}
                  onChange={v => setSetting('allAlerts', v)} />
          <Toggle label="Sound" checked={settings.sound}
                  onChange={v => setSetting('sound', v)} />
          <Toggle label="Browser notifications" checked={settings.browserNotif}
                  onChange={v => { setSetting('browserNotif', v); if (v) requestPermission() }} />
          <div className="mt-2 flex items-center justify-between">
            <button onClick={testSound} className="text-xs text-sky-600 hover:underline">Test sound</button>
            <button onClick={() => setOpen(false)} className="text-xs text-slate-400 hover:text-slate-600">Close</button>
          </div>
        </div>
      )}

      <div className="flex items-center gap-2">
        <button onClick={() => setOpen(o => !o)} title="Notification settings"
                aria-label="Notification settings"
                className="w-10 h-10 rounded-full bg-slate-800 text-white shadow-lg
                           hover:bg-slate-700 flex items-center justify-center">⚙️</button>
        <button onClick={toggleMute}
                title={muted ? 'Unmute alerts' : 'Mute alerts'}
                aria-label={muted ? 'Unmute alerts' : 'Mute alerts'}
                className="relative w-12 h-12 rounded-full bg-purple-600 text-white shadow-lg
                           hover:bg-purple-500 flex items-center justify-center text-xl">
          {muted ? '🔇' : '🔊'}
          {unreadUrgent > 0 && (
            <span className="absolute -top-1 -right-1 min-w-[20px] h-5 px-1 rounded-full
                             bg-red-600 text-white text-[11px] font-bold flex items-center justify-center">
              {unreadUrgent > 99 ? '99+' : unreadUrgent}
            </span>
          )}
        </button>
      </div>
    </div>
  )
}

function Toggle({ label, checked, onChange }: {
  label: string; checked: boolean; onChange: (v: boolean) => void
}) {
  return (
    <label className="flex items-center justify-between py-1 cursor-pointer">
      <span>{label}</span>
      <button type="button" role="switch" aria-checked={checked} aria-label={label}
              onClick={() => onChange(!checked)}
              className={'w-9 h-5 rounded-full transition-colors flex items-center ' +
                         (checked ? 'bg-emerald-500' : 'bg-slate-300 dark:bg-slate-600')}>
        <span className={'block w-4 h-4 bg-white rounded-full shadow transition-transform ' +
                         (checked ? 'translate-x-4' : 'translate-x-0.5')} />
      </button>
    </label>
  )
}
