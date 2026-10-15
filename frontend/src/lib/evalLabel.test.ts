import { describe, expect, it } from 'vitest'

import { clickToPoint, progressText } from './evalLabel'

describe('clickToPoint', () => {
  const rect = { left: 100, top: 50, width: 400, height: 200 }
  it('maps a click to image-relative coordinates', () => {
    expect(clickToPoint(300, 150, rect)).toEqual([0.5, 0.5])
    expect(clickToPoint(100, 50, rect)).toEqual([0, 0])
    expect(clickToPoint(500, 250, rect)).toEqual([1, 1])
  })
  it('ignores clicks outside the image or on an empty rect', () => {
    expect(clickToPoint(99, 150, rect)).toBeNull()
    expect(clickToPoint(300, 251, rect)).toBeNull()
    expect(clickToPoint(1, 1, { left: 0, top: 0, width: 0, height: 0 })).toBeNull()
  })
})

describe('progressText', () => {
  it('summarises labelling progress', () => {
    expect(progressText({ labelled: 12, skipped: 3, pending: 5 }, 400))
      .toBe('12 of 400 labelled (3 skipped) · 5 waiting')
    expect(progressText({}, 100)).toBe('0 of 100 labelled')
  })
})
