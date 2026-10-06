// @vitest-environment jsdom
//
// A True/False click on a grouped card is saved on the alert the operator
// was looking at, and its result stays on the card even when the group
// data changes underneath it.
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Alert } from '@/api/alerts'

const api = vi.hoisted(() => ({
  confirm: vi.fn(async (id: number) => ({ id, status: 'confirmed' })),
  dismiss: vi.fn(async (id: number) => ({ id, status: 'dismissed' })),
  resolve: vi.fn(async (id: number) => ({ id, status: 'confirmed' })),
  acknowledge: vi.fn(async () => ({})),
  addNote: vi.fn(async () => ({})),
  get: vi.fn(async () => { throw new Error('not needed in this test') }),
}))
vi.mock('@/api/alerts', () => ({ alerts: api }))

import { AlertCard } from './AlertCard'

function alert(id: number, hhmm: string, extra: Partial<Alert> = {}): Alert {
  return {
    id, event_id: id, status: 'new', severity: 'warning', severity_label: 'ATTENTION',
    title: `Alert ${id}`, plain_title: `Person seen (${id})`, body: null,
    detection_type: 'person', camera_id: 7, camera_name: 'Camera 7',
    created_at: `2026-10-06T${hhmm}:00+03:00`,
    acknowledged_at: null, resolved_at: null, snapshot_url: null,
    what_to_do: [], review_only: false, notification_suppressed: false,
    ...extra,
  } as Alert
}

function card(head: Alert, siblings: Alert[], onChanged?: () => void) {
  return (
    <MemoryRouter>
      <AlertCard alert={head} groupCount={1 + siblings.length}
                 groupLast={head.created_at} groupSiblings={siblings}
                 onChanged={onChanged} />
    </MemoryRouter>
  )
}

const shownAlertId = () => screen.getByTitle('Alert reference for investigation notes').textContent

beforeEach(() => {
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  vi.spyOn(console, 'warn').mockImplementation(() => {})
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.useRealTimers()
})

describe('grouped AlertCard verdicts', () => {
  // Newer alert (#2) already marked True; the older one (#1) is open and
  // is what the card shows.
  const newerTrue = alert(2, '10:05', { status: 'confirmed', acknowledged_at: '2026-10-06T10:06:00+03:00' })
  const olderOpen = alert(1, '10:00')

  it('saves False on the alert being shown, never its neighbour', async () => {
    render(card(olderOpen, [newerTrue]))
    expect(shownAlertId()).toBe('Alert #1')
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /False Alert/ })) })
    expect(api.dismiss).toHaveBeenCalledTimes(1)
    expect(api.dismiss).toHaveBeenCalledWith(1)
    expect(api.confirm).not.toHaveBeenCalled()
    expect(screen.getByText('✓ Marked False')).toBeTruthy()
    expect(screen.getByTestId('group-summary').textContent)
      .toBe('2 alerts: 1 marked True, 1 marked False')
  })

  it('keeps showing the marked alert when the group head switches, then moves on after 10 s', async () => {
    vi.useFakeTimers()
    const { rerender } = render(card(olderOpen, [newerTrue]))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /False Alert/ })) })

    // The page refreshes and (under the old rule) hands the card the OTHER
    // alert as head. The card must not swap to "Marked True" under the operator.
    const olderFalse = { ...olderOpen, status: 'dismissed', acknowledged_at: '2026-10-06T10:20:00+03:00' }
    rerender(card(newerTrue, [olderFalse]))
    expect(shownAlertId()).toBe('Alert #1')
    expect(screen.getByText('✓ Marked False')).toBeTruthy()
    expect(screen.getByTestId('group-summary').textContent)
      .toBe('2 alerts: 1 marked True, 1 marked False')

    await act(async () => { vi.advanceTimersByTime(10_000) })
    expect(shownAlertId()).toBe('Alert #2')
  })

  it('shows "Next in this group" when another alert is still open', async () => {
    const newerOpen = alert(3, '10:12')
    const { rerender } = render(card(newerOpen, [olderOpen]))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /True Alert/ })) })
    expect(api.confirm).toHaveBeenCalledWith(3)
    // Refresh: #3 is now True, #1 is the open head.
    rerender(card(olderOpen, [{ ...newerOpen, status: 'confirmed' }]))
    expect(shownAlertId()).toBe('Alert #3')
    expect(screen.getByText('✓ Marked True')).toBeTruthy()
    expect(screen.getByText(/Next in this group:/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Show now/ }))
    expect(shownAlertId()).toBe('Alert #1')
  })

  it('rolls back to the same alert if saving fails', async () => {
    api.dismiss.mockRejectedValueOnce(new Error('offline'))
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {})
    render(card(olderOpen, [newerTrue]))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /False Alert/ })) })
    expect(alertSpy).toHaveBeenCalled()
    expect(shownAlertId()).toBe('Alert #1')
    expect(screen.getByRole('button', { name: /False Alert/ })).toBeTruthy()
  })

  it('lists each other alert with its own result when expanded', () => {
    render(card(olderOpen, [newerTrue]))
    fireEvent.click(screen.getByRole('button', { name: /×2 today/ }))
    expect(screen.getByText('✓ True')).toBeTruthy()
  })
})

describe('verdict responsiveness', () => {
  it('shows the result at once with "saving…", and a second click sends nothing', async () => {
    let finish: (v: { id: number; status: string }) => void = () => {}
    api.dismiss.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    render(card(alert(1, '10:00'), []))
    const falseBtn = screen.getByRole('button', { name: /False Alert/ })
    await act(async () => { fireEvent.click(falseBtn); fireEvent.click(falseBtn) })
    expect(screen.getByText('✓ Marked False')).toBeTruthy()
    expect(screen.getByText(/saving…/)).toBeTruthy()
    expect(api.dismiss).toHaveBeenCalledTimes(1)
    await act(async () => { finish({ id: 1, status: 'dismissed' }) })
    expect(screen.queryByText(/saving…/)).toBeNull()
  })

  it('does not reload the list; it announces the saved change instead', async () => {
    const onChanged = vi.fn()
    const events: unknown[] = []
    const listener = (e: Event) => events.push((e as CustomEvent).detail)
    window.addEventListener('vg:alert-resolved', listener)
    render(card(alert(1, '10:00'), [], onChanged))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /True Alert/ })) })
    window.removeEventListener('vg:alert-resolved', listener)
    expect(onChanged).not.toHaveBeenCalled()
    expect(events).toHaveLength(1)
    expect(events[0]).toMatchObject({ id: 1, action: 'resolve',
                                      patch: { status: 'confirmed', resolved_at: null } })
  })

  it('cancelling the "Are you sure?" popup saves nothing', async () => {
    vi.mocked(window.confirm).mockReturnValueOnce(false)
    render(card(alert(1, '10:00'), []))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /False Alert/ })) })
    expect(api.dismiss).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /False Alert/ })).toBeTruthy()
  })
})
