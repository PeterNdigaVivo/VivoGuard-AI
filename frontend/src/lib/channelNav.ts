// "Previous / next channel" on the camera setup page (pure, unit tested).
//
// Grouping:
//   • a camera on an NVR steps through the channels of the SAME NVR, in
//     channel_number order (gaps are fine; channels without a number last,
//     by name);
//   • a camera with no NVR steps through the other NVR-less cameras of the
//     same store, by name — so an admin never jumps between an NVR's
//     channels and stand-alone cameras;
//   • no NVR and no store, or nobody else in the group → no navigation.

import type { Camera } from '@/api/cameras'

/** The fields the navigation needs (Camera satisfies this). */
export type NavCamera = Pick<Camera, 'id' | 'name' | 'nvr_id' | 'channel_number'
  | 'store_id' | 'status' | 'ai_enabled'>

export interface ChannelNav {
  scope: 'nvr' | 'store'
  prev: NavCamera | null
  next: NavCamera | null
  /** 1-based position of the current camera in its group. */
  position: number
  total: number
  current: NavCamera
}

const byName = (a: NavCamera, b: NavCamera) =>
  a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }) || a.id - b.id

const byChannel = (a: NavCamera, b: NavCamera) => {
  const ca = a.channel_number ?? Number.POSITIVE_INFINITY
  const cb = b.channel_number ?? Number.POSITIVE_INFINITY
  return ca !== cb ? ca - cb : byName(a, b)
}

export function channelNeighbours(cameras: NavCamera[], currentId: number): ChannelNav | null {
  const current = cameras.find(c => c.id === currentId)
  if (!current) return null
  let scope: ChannelNav['scope']
  let group: NavCamera[]
  if (current.nvr_id != null) {
    scope = 'nvr'
    group = cameras.filter(c => c.nvr_id === current.nvr_id).sort(byChannel)
  } else if (current.store_id != null) {
    scope = 'store'
    group = cameras.filter(c => c.nvr_id == null && c.store_id === current.store_id).sort(byName)
  } else {
    return null
  }
  if (group.length < 2) return null
  const i = group.findIndex(c => c.id === current.id)
  return {
    scope,
    prev: i > 0 ? group[i - 1] : null,
    next: i < group.length - 1 ? group[i + 1] : null,
    position: i + 1,
    total: group.length,
    current,
  }
}

/** "MNS Staff only area (Channel 2) · offline · AI off" */
export function cameraLabel(c: NavCamera): string {
  const marks: string[] = []
  if (c.status && c.status !== 'online') marks.push(c.status)
  if (c.ai_enabled === false) marks.push('AI off')
  const channel = c.channel_number != null ? ` (Channel ${c.channel_number})` : ''
  return `${c.name}${channel}${marks.map(m => ` · ${m}`).join('')}`
}

/** Tooltip for an arrow: the target, or why there is none. */
export function arrowTitle(nav: ChannelNav, dir: 'prev' | 'next'): string {
  const target = dir === 'prev' ? nav.prev : nav.next
  if (target) return `${dir === 'prev' ? 'Previous' : 'Next'}: ${cameraLabel(target)}`
  const where = nav.scope === 'nvr' ? 'channel on this NVR' : 'camera in this store'
  return `${dir === 'prev' ? 'First' : 'Last'} ${where}`
}

/** "Channel 3 · 3 of 8 on Moi Avenue's NVR" / "Camera 2 of 5 in Moi Avenue". */
export function positionLabel(nav: ChannelNav, opts: { storeName?: string | null;
                                                       nvrCountInStore?: number }): string {
  const store = opts.storeName || null
  if (nav.scope === 'store') {
    return `Camera ${nav.position} of ${nav.total}${store ? ` in ${store}` : ''}`
  }
  const nvr = !store ? `NVR #${nav.current.nvr_id}`
    : (opts.nvrCountInStore ?? 1) > 1 ? `NVR #${nav.current.nvr_id} at ${store}`
    : `${store}'s NVR`
  const channel = nav.current.channel_number != null ? `Channel ${nav.current.channel_number} · ` : ''
  return `${channel}${nav.position} of ${nav.total} on ${nvr}`
}

/** How many different NVRs this store's cameras use (for the label). */
export function nvrCountInStore(cameras: NavCamera[], storeId: number | null): number {
  if (storeId == null) return 0
  return new Set(cameras.filter(c => c.store_id === storeId && c.nvr_id != null)
    .map(c => c.nvr_id)).size
}
