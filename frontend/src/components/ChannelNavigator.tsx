// ← Previous channel · "Channel 3 · 3 of 8 on Moi Avenue's NVR" · Next channel →
// Used next to the title on the camera setup page. The end arrows use
// aria-disabled (not `disabled`) so their "Last channel on this NVR"
// tooltip still shows; clicking them does nothing.

import { Button } from '@/components/ui/Primitives'
import { arrowTitle, cameraLabel, type ChannelNav } from '@/lib/channelNav'

export function ChannelNavigator({ nav, label, onGo }: {
  nav: ChannelNav
  label: string
  onGo: (cameraId: number) => void
}) {
  const arrow = (dir: 'prev' | 'next') => {
    const target = dir === 'prev' ? nav.prev : nav.next
    const text = dir === 'prev' ? '← Previous channel' : 'Next channel →'
    return (
      <Button type="button" variant="ghost"
              aria-disabled={!target}
              title={arrowTitle(nav, dir)}
              aria-label={arrowTitle(nav, dir)}
              onClick={() => { if (target) onGo(target.id) }}
              className={target ? '' : 'opacity-50 cursor-not-allowed'}>
        {text}
      </Button>
    )
  }
  return (
    <nav className="flex items-center gap-2" aria-label="Channels">
      {arrow('prev')}
      <span className="text-sm leading-tight text-center px-1" aria-live="polite">
        <span className="block font-semibold text-slate-800 dark:text-slate-100">
          {cameraLabel({ ...nav.current, channel_number: null })}
        </span>
        <span className="block text-xs text-slate-600 dark:text-slate-300 tabular-nums">{label}</span>
      </span>
      {arrow('next')}
    </nav>
  )
}
