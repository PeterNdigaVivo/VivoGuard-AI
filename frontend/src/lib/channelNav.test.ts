import { describe, expect, it } from 'vitest'

import {
  arrowTitle, cameraLabel, channelNeighbours, nvrCountInStore, positionLabel, type NavCamera,
} from './channelNav'

function cam(id: number, extra: Partial<NavCamera> = {}): NavCamera {
  return { id, name: `Cam ${id}`, nvr_id: null, channel_number: null, store_id: 1,
           status: 'online', ai_enabled: true, ...extra }
}

// NVR 10 with channels 1, 2, 4, 7 (gaps), listed out of order, plus another NVR.
const fleet: NavCamera[] = [
  cam(4, { nvr_id: 10, channel_number: 7, name: 'Back door' }),
  cam(1, { nvr_id: 10, channel_number: 1, name: 'Entrance' }),
  cam(3, { nvr_id: 10, channel_number: 4, name: 'Till', status: 'offline' }),
  cam(2, { nvr_id: 10, channel_number: 2, name: 'MNS Staff only area', ai_enabled: false }),
  cam(9, { nvr_id: 20, channel_number: 3, name: 'Other NVR', store_id: 2 }),
  cam(5, { name: 'Stockroom' }),                  // store 1, no NVR
  cam(6, { name: 'Office' }),                     // store 1, no NVR
  cam(7, { name: 'Alone', store_id: 3 }),          // only camera of store 3
  cam(8, { name: 'Floating', store_id: null }),   // no NVR, no store
]

describe('channelNeighbours', () => {
  it('orders an NVR by channel number, with gaps, ignoring other NVRs', () => {
    const nav = channelNeighbours(fleet, 2)!
    expect(nav.scope).toBe('nvr')
    expect([nav.prev?.id, nav.next?.id]).toEqual([1, 3])
    expect([nav.position, nav.total]).toEqual([2, 4])
  })

  it('has no previous at the first channel and no next at the last', () => {
    const first = channelNeighbours(fleet, 1)!
    expect(first.prev).toBeNull()
    expect(first.next?.id).toBe(2)
    const last = channelNeighbours(fleet, 4)!
    expect(last.next).toBeNull()
    expect(last.prev?.id).toBe(3)
    expect(last.position).toBe(4)
  })

  it('puts channels without a number last, by name', () => {
    const cams = [cam(1, { nvr_id: 5, channel_number: 2 }),
                  cam(2, { nvr_id: 5, channel_number: null, name: 'B' }),
                  cam(3, { nvr_id: 5, channel_number: null, name: 'A' })]
    const nav = channelNeighbours(cams, 3)!
    expect([nav.prev?.id, nav.next?.id, nav.position]).toEqual([1, 2, 2])
  })

  it('falls back to the store\'s other NVR-less cameras, by name', () => {
    const nav = channelNeighbours(fleet, 5)!     // Stockroom
    expect(nav.scope).toBe('store')
    expect(nav.prev?.id).toBe(6)                 // Office comes first by name
    expect(nav.next).toBeNull()
    expect(nav.total).toBe(2)                    // the NVR channels of store 1 are not mixed in
  })

  it('returns nothing for a lone camera, a camera without NVR or store, or an unknown id', () => {
    expect(channelNeighbours(fleet, 7)).toBeNull()
    expect(channelNeighbours(fleet, 8)).toBeNull()
    expect(channelNeighbours(fleet, 9)).toBeNull()   // the only channel on NVR 20
    expect(channelNeighbours(fleet, 999)).toBeNull()
    expect(channelNeighbours([], 1)).toBeNull()
  })
})

describe('labels', () => {
  it('marks offline and AI-off cameras', () => {
    expect(cameraLabel(fleet.find(c => c.id === 2)!)).toBe('MNS Staff only area (Channel 2) · AI off')
    expect(cameraLabel(fleet.find(c => c.id === 3)!)).toBe('Till (Channel 4) · offline')
    expect(cameraLabel(cam(5, { name: 'Stockroom' }))).toBe('Stockroom')
  })

  it('arrow tooltips name the target, or say why there is none', () => {
    const first = channelNeighbours(fleet, 1)!
    expect(arrowTitle(first, 'next')).toBe('Next: MNS Staff only area (Channel 2) · AI off')
    expect(arrowTitle(first, 'prev')).toBe('First channel on this NVR')
    expect(arrowTitle(channelNeighbours(fleet, 4)!, 'next')).toBe('Last channel on this NVR')
    expect(arrowTitle(channelNeighbours(fleet, 5)!, 'next')).toBe('Last camera in this store')
  })

  it('position label for an NVR and for a store', () => {
    const nav = channelNeighbours(fleet, 3)!
    expect(positionLabel(nav, { storeName: 'Moi Avenue', nvrCountInStore: 1 }))
      .toBe("Channel 4 · 3 of 4 on Moi Avenue's NVR")
    expect(positionLabel(nav, { storeName: 'Moi Avenue', nvrCountInStore: 2 }))
      .toBe('Channel 4 · 3 of 4 on NVR #10 at Moi Avenue')
    expect(positionLabel(nav, { storeName: null })).toBe('Channel 4 · 3 of 4 on NVR #10')
    expect(positionLabel(channelNeighbours(fleet, 6)!, { storeName: 'Moi Avenue' }))
      .toBe('Camera 1 of 2 in Moi Avenue')
  })

  it('counts NVRs per store', () => {
    expect(nvrCountInStore(fleet, 1)).toBe(1)
    expect(nvrCountInStore([...fleet, cam(30, { nvr_id: 11 })], 1)).toBe(2)
    expect(nvrCountInStore(fleet, null)).toBe(0)
  })
})
